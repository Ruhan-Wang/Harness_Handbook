# -*- coding: utf-8 -*-
"""Phase 3 critic + reflexion step.

`score_tier`  — run the per-tier rubric over one generated output (against its
                ground truth when one exists), return a scored Verdict.
`reflect`     — distill a failing Verdict into the SINGLE highest-leverage fix
                (one sentence) to focus THIS unit's next attempt.

Both are thin LLM wrappers; the scoring math lives in rubrics.compute_verdict so
gate logic is deterministic and not at the model's mercy.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Reach phase2's api_client (same path trick the other phase3 tools use).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
_PHASE2_TOOLS = _HERE.parent.parent / "phase2/tools"
if str(_PHASE2_TOOLS) not in sys.path:
    sys.path.insert(0, str(_PHASE2_TOOLS))

from api_client import Api  # noqa: E402
from rubrics import Rubric, Verdict, compute_verdict  # noqa: E402

logger = logging.getLogger(__name__)


# ─── Scoring ─────────────────────────────────────────────────────────────────


def build_critic_prompt(
    rubric: Rubric,
    output_text: str,
    ground_truth: str = "",
) -> str:
    """Compose the critic prompt.

    The rubric supplies WHAT to judge (purpose, mindset, criteria, gates); this
    function supplies HOW to score (the 1-5 scale + strictness calibration) and
    handles the optional ground truth.

    `ground_truth` is OPTIONAL supplementary evidence — only Tier 3 supplies it
    (the verified source, needed to check accuracy / params / registers against
    real code). Tier 1 / Tier 2 pass nothing: there is no gold answer to match,
    so the critic scores against the RUBRIC and proposes its own revision
    suggestions. The rubric is always the standard; ground truth just lets the
    critic verify factual claims when it exists.
    """
    if ground_truth and ground_truth.strip():
        source_rule = (
            "- Verify every factual claim against the ground truth below; a claim "
            "it does not support is an accuracy failure, not a style nit.\n"
        )
        gt_block = (
            "\n## Ground truth (the real source — verify claims against this)\n"
            f"{ground_truth}\n"
        )
    else:
        source_rule = (
            "- There is no external ground truth. Judge only what's on the page "
            "against the rubric; do NOT invent factual errors you cannot verify.\n"
        )
        gt_block = ""

    return f"""You are a strict, experienced reviewer scoring ONE generated handbook unit for this tier.

{rubric.to_prompt_block()}

## How to score (read carefully)
- Score each criterion 1-5 INDEPENDENTLY — don't let one strong aspect lift the others.
  5 = genuinely excellent (rare). 4 = solid. 3 = acceptable. 2 = weak. 1 = fails.
- Be calibrated and strict. A first draft is rarely all 5s; if you're about to give
  mostly 5s, look harder for what's weak.
- Quote the specific text you are judging as evidence for every score.
- For every criterion you do NOT give full marks, give one concrete, actionable fix.
{source_rule}{gt_block}
## Output under review
{output_text}

{rubric.verdict_schema_hint()}
"""


def score_tier(
    api: Api,
    rubric: Rubric,
    output_text: str,
    ground_truth: str,
) -> Verdict:
    """Score one output. A broken/unparseable critic response yields an
    all-1 verdict (fails everything) so the loop retries rather than passing
    junk through on a silent critic failure."""
    prompt = build_critic_prompt(rubric, output_text, ground_truth)
    try:
        result = api.call(prompt)
    except Exception as e:  # noqa: BLE001
        logger.warning("critic call failed (%s): %s", rubric.tier, e)
        return compute_verdict(rubric, {}, ["critic call failed; retry"])

    parsed = result.parsed_json
    if not isinstance(parsed, dict):
        logger.warning("critic returned no JSON (%s)", rubric.tier)
        return compute_verdict(rubric, {}, ["critic returned no JSON; retry"])

    raw_scores = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
    findings = parsed.get("actionable_findings") or []
    if not isinstance(findings, list):
        findings = []
    return compute_verdict(rubric, raw_scores, findings)


# ─── Reflexion ───────────────────────────────────────────────────────────────


_REFLECT_PROMPT = """A handbook unit just failed review. From the critic's findings,
name the SINGLE most important change for the next attempt — the root cause of the
failure, not a laundry list. It must be:
  - the highest-leverage fix (what, if fixed, moves the score the most);
  - concrete and actionable (a direct instruction to the writer);
  - one sentence.

Tier: {tier}
Failed gates: {gates}
Critic's findings:
{findings}

Output ONLY the single instruction, no preamble, no quotes."""


def reflect(api: Api, verdict: Verdict) -> str:
    """Turn a failing verdict into one verbal lesson (the reflexion step).

    Returns "" on failure — the loop still has the raw critic findings to
    revise with, so a missing lesson just means no extra guidance this round."""
    findings = verdict.actionable_findings or [
        f for cs in verdict.scores.values() for f in cs.findings
    ]
    if not findings:
        return ""
    prompt = _REFLECT_PROMPT.format(
        tier=verdict.tier,
        gates=", ".join(verdict.gate_failures) or "(none — soft shortfall)",
        findings="\n".join(f"- {f}" for f in findings[:8]),
    )
    try:
        result = api.call(prompt)
    except Exception as e:  # noqa: BLE001
        logger.warning("reflect call failed (%s): %s", verdict.tier, e)
        return ""
    lesson = (result.raw_text or "").strip().strip('"').splitlines()
    return lesson[0].strip() if lesson else ""


def revise_findings_block(verdict: Verdict) -> str:
    """The targeted feedback fed back into the actor's next attempt (drives the
    within-run correction — same idea as phase2's build_revise_prompt)."""
    lines = ["## Reviewer findings — fix every one in your next attempt"]
    if verdict.gate_failures:
        lines.append(f"BLOCKING (must fix): {', '.join(verdict.gate_failures)}")
    for f in verdict.actionable_findings:
        lines.append(f"- {f}")
    for cs in verdict.scores.values():
        for f in cs.findings:
            lines.append(f"- [{cs.name}] {f}")
    return "\n".join(lines)
