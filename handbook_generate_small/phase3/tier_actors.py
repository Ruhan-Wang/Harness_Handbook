# -*- coding: utf-8 -*-
"""Tier actors + ground-truth builders — the seam between the existing prompts
and the actor-critic-reflexion loop.

Each `make_tierN_gen(...)` returns a `GenFn` of shape `gen(mem_block, revise_block)
-> text`, which `tier_loop.produce` calls each round. We REUSE the existing
hand-authored Tier 1/2/3 prompts (from assemble.py / translate_member.py) and
just append the reflexion-memory block (A+B) and the previous round's revise
findings. The base prompt is never modified.

`ground_truth_tierN(...)` returns the evidence the critic judges against:
  - Tier 1: skeleton brief (stages + registers) — is the shape faithful?
  - Tier 2: stage members + that stage's registers + neighbors.
  - Tier 3: the verified source snippets + register list.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


# Tier 1 / Tier 2 prompts describe their output sections with markdown headings
# (## (a) … (70-150 字)), and the LLM tends to copy them verbatim — leaking the
# word-count hints and emitting H2s that collide with the stage's own heading.
# This is a prompt-agnostic safety net: strip the word-count annotations and
# demote any heading so the narrative nests cleanly UNDER the stage heading.
_WORDCOUNT_RE = re.compile(
    r"\s*[（(]\s*(?:约|~|approx\.?\s*)?\s*\d+(?:\s*[-–—]\s*\d+)?\s*(?:字|words?|词)\s*[)）]"
)


def _clean_narrative(text: str) -> str:
    out = []
    for line in text.splitlines():
        line = _WORDCOUNT_RE.sub("", line)
        m = re.match(r"^(#{1,6})(\s)", line)
        if m:  # demote: push tier headings below the stage's ## / ###
            line = "#" * min(len(m.group(1)) + 2, 6) + line[len(m.group(1)):]
        out.append(line)
    return "\n".join(out).strip()

# Reuse the existing, hand-tuned prompts + brief helpers.
from project_context import get_project_context  # noqa: E402
from prompts import _PROMPTS_BY_LANG  # noqa: E402
from skeleton_view import (  # noqa: E402
    _members_brief,
    _registers_brief,
    _side_brief,
    _stage_registers_brief,
    _stages_brief,
)
from translate_member import (  # noqa: E402
    TranslationUnit,
    build_prompt,
    validate_translation,
)

logger = logging.getLogger(__name__)


# ─── Tier 1 ──────────────────────────────────────────────────────────────────


def make_tier1_gen(api, skeleton: dict, lang: str):
    ctx = get_project_context()
    base = _PROMPTS_BY_LANG[lang]["tier1"].format(
        project_name=ctx.name,
        project_block=ctx.block(lang),
        stages_brief=_stages_brief(skeleton),
        side_brief=_side_brief(skeleton),
        registers_brief=_registers_brief(skeleton),
    )

    def gen(extra: str) -> str:
        return _clean_narrative(api.call(base + extra).raw_text)

    return gen


# Tier 1 has no ground truth: there's no gold answer for "is this a good novice
# overview". The critic scores against the rubric and proposes its own fixes.


# ─── Tier 2 ──────────────────────────────────────────────────────────────────


def make_tier2_gen(api, stage: dict, members: list, skeleton: dict,
                   adjacent_brief: str, lang: str):
    none_marker = "(none)" if lang == "en" else "(无)"
    ctx = get_project_context()
    base = _PROMPTS_BY_LANG[lang]["tier2"].format(
        project_name=ctx.name,
        project_block=ctx.block(lang),
        stage_id=stage.get("id", ""),
        stage_title=stage.get("title", ""),
        stage_description=stage.get("description", ""),
        members_brief=_members_brief(members),
        stage_registers=_stage_registers_brief(skeleton, stage.get("id", "")),
        adjacent_brief=adjacent_brief or none_marker,
    )

    def gen(extra: str) -> str:
        return _clean_narrative(api.call(base + extra).raw_text)

    return gen


# Tier 2 has no ground truth either: the critic scores against the rubric and
# proposes its own fixes. (Consequence: data_flow_validity can no longer verify
# register ids against a list — it becomes a coherence check. The stage's
# register list is still fed to the ACTOR via make_tier2_gen, so the writer
# still knows the real registers.)


# ─── Tier 3 ──────────────────────────────────────────────────────────────────


def make_tier3_gen(api, unit: TranslationUnit, skeleton: dict,
                   sibling_synopses: list, lang: str):
    """The Tier-3 actor returns parseable, schema-valid JSON (pretty-printed
    text). Mechanical shape (validate_translation) is enforced HERE with a
    couple of internal retries, so the critic above only ever judges semantic
    quality on well-formed output — mirroring the existing translate_unit."""
    base = build_prompt(unit, skeleton, sibling_synopses, lang=lang)

    def gen(extra: str) -> str:
        prompt = base + extra
        last_raw = ""
        for _ in range(2):
            res = api.call(prompt)
            last_raw = res.raw_text
            t = res.parsed_json
            if t is not None and validate_translation(unit, t) is None:
                return json.dumps(t, ensure_ascii=False, indent=2)
        return last_raw  # malformed → critic will score it low and the loop retries

    return gen


def ground_truth_tier3(unit: TranslationUnit, skeleton: dict) -> str:
    parts = [f"## function: {unit.qualname}  (type={unit.type_kind})"]
    for i, (entry, snip) in enumerate(zip(unit.entries, unit.snippets), 1):
        parts.append(
            f"### entry {i} · line_range={entry.get('line_range')} "
            f"· sha1={snip.sha1[:8]}\n```python\n{snip.text}\n```"
        )
    regs = [f"  - {r.get('id','')}: {(r.get('semantics') or '')[:160]}"
            for r in (skeleton.get("state_registers") or [])]
    parts.append("## state_registers (for register_interactions check)\n"
                 + ("\n".join(regs) or "  (none)"))
    return "\n\n".join(parts)


def parse_tier3_output(text: str) -> dict | None:
    """Recover the structured translation dict from the loop's best output."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # best output may be a fenced block; let translate_member's extractor try
        from api_client import _extract_json_block
        return _extract_json_block(text)
