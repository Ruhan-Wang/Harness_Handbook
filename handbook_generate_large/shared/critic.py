# -*- coding: utf-8 -*-
"""critic.py — Actor-Critic framework + role-play prompts.

The framework implements the loop:
    proposal_v1  = Actor(input)
    verdict      = Critic(proposal_v1, role)
    if APPROVE → return v1
    if REJECT  → return None  (discarded)
    if REVISE  → proposal_v2 = Actor(input, critique)
                  verdict2   = Critic(proposal_v2, role)
                  if APPROVE → return v2
                  else        → return None

For Pass C (skeleton changes), use multi_review: 3 critics with different roles
each review the same proposal; all three must APPROVE for application.

Decisions about *what to do* live in `pass_*.py` modules. This module just
provides the orchestration + role prompts.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_client import Api  # noqa: E402

logger = logging.getLogger(__name__)


# ─── Role prompts ────────────────────────────────────────────────────────────


ROLE_PROMPTS: dict[str, str] = {
    "engineer": """You are a SENIOR ENGINEER reviewing a proposed change to how a function is classified or organized in a handbook for an AI agent harness. Your job is to be skeptical and find real concerns rooted in code behavior.

Focus on:
  - Does the proposed classification reflect what the function ACTUALLY does at the code level?
  - Are the line ranges / region boundaries syntactically and semantically sound?
  - Does the proposal misuse cross-cutting categories (e.g. labeling a function "crosscut-X3 logging" just because it calls self.logger)?
  - Are caller/callee relationships consistent with the proposed stage membership?

You may APPROVE, REVISE (with concrete concerns), or REJECT.""",

    "architect": """You are a SYSTEM ARCHITECT reviewing a proposed change to the structural mapping of an AI agent harness. Your job is to find structural problems.

Focus on:
  - Do stage boundaries stay clean?
  - Does this change cause one stage to bloat (>20 members) or starve (<2 members)?
  - Are subsystem boundaries respected (subsys-tmux internals should not invade stage-2's main narrative)?
  - Does the proposed multi-stage assignment reflect genuine multi-identity, or is the function just being shoehorned?

You may APPROVE, REVISE, or REJECT.""",

    "reader": """You are a TECHNICAL WRITER / HANDBOOK EDITOR reviewing a proposed change from the perspective of a future reader who doesn't know the codebase deeply. Your job is to ensure the proposed change makes the handbook MORE READABLE, not less.

Focus on:
  - Would a reader land on a stage page and find members that fit together?
  - Are stage titles and ID hierarchies intuitive?
  - Do regions read like distinct narrative steps, or do they feel arbitrary?
  - Would a reader of stage X be surprised to find a particular function there?

You may APPROVE, REVISE, or REJECT.""",

    "editor": """You are a NARRATIVE EDITOR reviewing a proposed ORDERING of members within one stage of a handbook. Your job is to ensure the resulting reading order matches the narrative the stage tells.

Focus on:
  - Does the chosen 'structure' (linear / branched / unordered) match the actual content?
  - For linear: do earlier members come before later ones?
  - For branched: are the branches semantically distinct (e.g. happy path vs fallback)?
  - For unordered: are these truly independent items?

You may APPROVE, REVISE, or REJECT.""",
}


# ─── Data types ──────────────────────────────────────────────────────────────


@dataclass
class Verdict:
    """A Critic's response to a proposal."""

    decision: str  # "APPROVE" | "REVISE" | "REJECT"
    concerns: list[str] = field(default_factory=list)
    suggested_revision: dict | None = None  # only for REVISE
    rationale: str = ""

    @property
    def is_approve(self) -> bool:
        return self.decision == "APPROVE"

    @property
    def is_revise(self) -> bool:
        return self.decision == "REVISE"

    @property
    def is_reject(self) -> bool:
        return self.decision == "REJECT"


@dataclass
class ActorCriticResult:
    """Final outcome of one Actor-Critic exchange."""

    final_proposal: dict | None  # None when discarded
    rounds: int
    actor_proposals: list[dict]  # history (v1, v2, ...)
    critic_verdicts: list[Verdict]  # history
    accepted: bool

    @property
    def discarded(self) -> bool:
        return not self.accepted


def _normalize_vacuous_revise(verdict: Verdict, role_label: str) -> Verdict:
    """Convert a vacuous REVISE (decision REVISE with no concerns) into APPROVE.

    A REVISE verdict is supposed to come with actionable feedback the Actor
    can address in its next attempt. When the Critic returns REVISE with an
    empty `concerns` list, the revise prompt that `build_revise_prompt`
    constructs has nothing for the Actor to react to — the second-round
    attempt typically lands within rounding distance of the first, and we
    burn a whole round of LLM cost producing the same proposal back.

    Treating vacuous REVISE as APPROVE avoids that round while keeping a
    WARN-level log entry so a real LLM regression (e.g., the Critic prompt
    starts producing REVISE verdicts without populating concerns) is still
    visible in telemetry.
    """
    if verdict.decision == "REVISE" and not verdict.concerns:
        logger.warning(
            "Critic(%s) returned REVISE with empty concerns — treating as "
            "APPROVE (no actionable feedback for the Actor to revise against)",
            role_label,
        )
        return Verdict(
            decision="APPROVE",
            concerns=[],
            suggested_revision=verdict.suggested_revision,
            rationale=f"[normalized from vacuous REVISE] {verdict.rationale}",
        )
    return verdict


# ─── Prompt construction ──────────────────────────────────────────────────────


_CRITIC_OUTPUT_RULES = """DECISION GUIDELINES
- APPROVE: the proposal is reasonable, even if minor wording could be tweaked. A correct-enough proposal is APPROVE, not REVISE. Be GENEROUS with APPROVE.
- REVISE: only when there is a SPECIFIC, ACTIONABLE flaw that materially affects correctness (wrong stage assignment, wrong region boundary, factual error about the code). Do NOT REVISE for style, phrasing, or "could be more thorough" reasons.
- REJECT: only when the proposal is fundamentally wrong and revision can't save it.

By default lean APPROVE. The pipeline must make progress.

OUTPUT FORMAT
Return ONLY a single JSON object wrapped in a ```json fenced block:

{
  "decision": "APPROVE" | "REVISE" | "REJECT",
  "concerns": ["<concern 1>", ...],
  "suggested_revision": { ... } OR null,
  "rationale": "<one sentence explanation>"
}

When decision == "APPROVE", concerns and suggested_revision may be empty.
When decision == "REVISE", concerns must be non-empty AND each concern must point to a concrete, fixable flaw.
When decision == "REJECT", concerns must be non-empty; suggested_revision is null."""


def build_critic_prompt(
    role: str,
    task_context: str,
    proposal: dict,
    proposal_schema_hint: str = "",
    review_evidence: str = "",
) -> str:
    """Build a Critic prompt.

    ``review_evidence`` is the ground-truth material the Critic needs to *judge
    semantic correctness* — source code, caller/callee list, current mapping
    excerpt, etc. Without this, the Critic can only do schema checks.
    """
    role_block = ROLE_PROMPTS.get(role) or ROLE_PROMPTS["engineer"]
    parts = [
        role_block,
        "",
        "## Task context",
        task_context,
    ]
    if review_evidence:
        parts += ["", "## Review evidence (ground truth for judgement)",
                  review_evidence]
    parts += [
        "",
        "## Proposal under review",
        "```json",
        json.dumps(proposal, ensure_ascii=False, indent=2),
        "```",
    ]
    if proposal_schema_hint:
        parts += ["", "## Proposal schema reminder", proposal_schema_hint]
    parts += ["", _CRITIC_OUTPUT_RULES]
    return "\n".join(parts)


def build_revise_prompt(
    actor_original_prompt: str,
    original_proposal: dict,
    verdict: Verdict,
) -> str:
    """Ask the Actor to revise its proposal given the Critic's concerns."""
    parts = [
        actor_original_prompt,
        "",
        "── PREVIOUS PROPOSAL (under review) ──",
        "```json",
        json.dumps(original_proposal, ensure_ascii=False, indent=2),
        "```",
        "",
        "── REVIEWER'S CONCERNS ──",
    ]
    for c in verdict.concerns:
        parts.append(f"  • {c}")
    if verdict.suggested_revision:
        parts += [
            "",
            "── REVIEWER'S SUGGESTED REVISION ──",
            "```json",
            json.dumps(verdict.suggested_revision, ensure_ascii=False, indent=2),
            "```",
        ]
    parts += [
        "",
        "Produce a revised proposal that addresses these concerns. "
        "You may adopt the suggested revision verbatim, modify it, or deviate "
        "with justification — but address every concern.",
        "",
        "Return the same JSON schema as before.",
    ]
    return "\n".join(parts)


def parse_verdict(parsed_json: dict | None) -> tuple[Verdict | None, str | None]:
    """Parse a critic's JSON response into a Verdict.

    Returns ``(verdict, error_reason)``:
      - ``(Verdict, None)`` on success.
      - ``(None, <short_reason>)`` on failure — the reason names the specific
        violation (e.g. ``"top-level not a dict"``, ``"decision='approved_with_caveats' not in canonical set"``)
        so callers can log a meaningful diagnostic when an LLM regression starts
        producing malformed verdicts at scale.
    """
    if not isinstance(parsed_json, dict):
        return None, f"top-level not a dict (got {type(parsed_json).__name__})"
    raw_decision = parsed_json.get("decision")
    if not isinstance(raw_decision, str):
        return None, f"decision missing or not a string (got {type(raw_decision).__name__})"
    decision = raw_decision.strip().upper()
    if decision not in ("APPROVE", "REVISE", "REJECT"):
        return None, f"decision={raw_decision!r} not in canonical set"
    concerns = parsed_json.get("concerns") or []
    if not isinstance(concerns, list):
        concerns = []
    # The schema for `suggested_revision` is "either null or a dict shaped
    # like a proposal-revision". `json.dumps` will happily serialize any
    # value the LLM puts here (string, list, number), but `build_revise_prompt`
    # then hands that nonsense to the Actor as the recommended revision and
    # the Actor — already disoriented by being asked to revise — tends to
    # echo it back verbatim. Rejecting non-conforming shapes here is the
    # earliest point at which we can fail loudly with a parse error.
    sugg = parsed_json.get("suggested_revision")
    if sugg is not None and not isinstance(sugg, dict):
        return None, f"suggested_revision is {type(sugg).__name__}, expected dict or null"
    return Verdict(
        decision=decision,
        concerns=[str(c) for c in concerns],
        suggested_revision=sugg,
        rationale=str(parsed_json.get("rationale", "")),
    ), None


# ─── Actor / Critic single calls ──────────────────────────────────────────────


def call_actor(api: Api, prompt: str) -> dict | None:
    """Run the Actor — returns parsed JSON proposal, or None on failure."""
    try:
        result = api.call(prompt)
    except Exception as e:  # noqa: BLE001
        logger.warning("Actor call failed: %s", e)
        return None
    return result.parsed_json


def call_critic(
    api: Api,
    role: str,
    task_context: str,
    proposal: dict,
    proposal_schema_hint: str = "",
    review_evidence: str = "",
) -> Verdict | None:
    """Run a Critic — returns Verdict, or None on failure."""
    prompt = build_critic_prompt(
        role, task_context, proposal, proposal_schema_hint, review_evidence
    )
    try:
        result = api.call(prompt)
    except Exception as e:  # noqa: BLE001
        logger.warning("Critic(%s) call failed: %s", role, e)
        return None
    verdict, err = parse_verdict(result.parsed_json)
    if verdict is None:
        # Log both the specific parse reason (e.g., "decision='approved' not
        # in canonical set") AND a truncated preview of the raw response.
        # Without the preview, every malformed-verdict surface as a generic
        # "verdict was None" downstream and the only way to figure out what
        # the LLM actually returned is to re-run with extra instrumentation.
        # The 300-char cap keeps logs readable while still showing enough
        # context to spot e.g. a new model returning a different decision
        # vocabulary.
        raw_preview = repr(result.parsed_json)[:300]
        logger.warning(
            "Critic(%s) verdict parse failed: %s; raw=%s",
            role, err, raw_preview,
        )
    return verdict


# ─── Orchestration: Actor + 1 Critic (≤2 rounds) ──────────────────────────────


def actor_critic_loop(
    api: Api,
    actor_prompt: str,
    critic_role: str,
    task_context: str,
    proposal_schema_hint: str = "",
    max_revise_rounds: int = 1,
    review_evidence: str = "",
) -> ActorCriticResult:
    """One Actor proposes, one Critic reviews, optionally one revise round.

    Returns ActorCriticResult capturing the full history.
    """
    proposals: list[dict] = []
    verdicts: list[Verdict] = []

    # Round 1
    p1 = call_actor(api, actor_prompt)
    if p1 is None:
        return ActorCriticResult(
            final_proposal=None,
            rounds=0,
            actor_proposals=[],
            critic_verdicts=[],
            accepted=False,
        )
    proposals.append(p1)

    v1 = call_critic(api, critic_role, task_context, p1,
                     proposal_schema_hint, review_evidence)
    if v1 is None:
        # Critic broke — conservative: discard.
        return ActorCriticResult(
            final_proposal=None,
            rounds=1,
            actor_proposals=proposals,
            critic_verdicts=[],
            accepted=False,
        )
    v1 = _normalize_vacuous_revise(v1, critic_role)
    verdicts.append(v1)

    if v1.is_approve:
        return ActorCriticResult(
            final_proposal=p1,
            rounds=1,
            actor_proposals=proposals,
            critic_verdicts=verdicts,
            accepted=True,
        )
    if v1.is_reject:
        return ActorCriticResult(
            final_proposal=None,
            rounds=1,
            actor_proposals=proposals,
            critic_verdicts=verdicts,
            accepted=False,
        )

    # v1.is_revise — try one revision round
    if max_revise_rounds < 1:
        return ActorCriticResult(
            final_proposal=None,
            rounds=1,
            actor_proposals=proposals,
            critic_verdicts=verdicts,
            accepted=False,
        )

    revise_prompt = build_revise_prompt(actor_prompt, p1, v1)
    p2 = call_actor(api, revise_prompt)
    if p2 is None:
        return ActorCriticResult(
            final_proposal=None,
            rounds=2,
            actor_proposals=proposals,
            critic_verdicts=verdicts,
            accepted=False,
        )
    proposals.append(p2)

    # Round 2: tell the Critic this is a revision review.
    round2_context = task_context + (
        f"\n\nNote: this is round 2. In round 1 you returned decision={v1.decision} "
        f"with concerns={v1.concerns!r}. The Actor revised; judge whether the "
        f"revision addresses these concerns."
    )
    v2 = call_critic(api, critic_role, round2_context, p2,
                     proposal_schema_hint, review_evidence)
    if v2 is None:
        return ActorCriticResult(
            final_proposal=None,
            rounds=2,
            actor_proposals=proposals,
            critic_verdicts=verdicts,
            accepted=False,
        )
    verdicts.append(v2)

    # After 2 rounds: REJECT discards, anything else accepts the revised v2.
    # Rationale: if Actor took one revise round, that's our best effort.
    # REVISE-after-REVISE means the Critic is being picky; ship v2.
    accepted = not v2.is_reject
    return ActorCriticResult(
        final_proposal=p2 if accepted else None,
        rounds=2,
        actor_proposals=proposals,
        critic_verdicts=verdicts,
        accepted=accepted,
    )


# ─── Orchestration: Actor + N Critics (all must approve) ─────────────────────


def actor_multi_critic_loop(
    api: Api,
    actor_prompt: str,
    critic_roles: list[str],
    task_context: str,
    proposal_schema_hint: str = "",
    max_revise_rounds: int = 1,
    review_evidence: str = "",
) -> ActorCriticResult:
    """Used by Pass C (skeleton changes). All critics must APPROVE.

    Strategy:
      1. Actor produces proposal_v1.
      2. All critics review v1 in parallel.
      3. If all approve → apply.
         If any rejects (or any revises) → collect all concerns, ask Actor to revise.
      4. Repeat once.
    """
    proposals: list[dict] = []
    verdicts: list[Verdict] = []

    # Round 1
    p1 = call_actor(api, actor_prompt)
    if p1 is None:
        return ActorCriticResult(
            final_proposal=None, rounds=0,
            actor_proposals=[], critic_verdicts=[], accepted=False,
        )
    proposals.append(p1)

    round1_verdicts: list[Verdict] = []
    for role in critic_roles:
        v = call_critic(api, role, task_context, p1,
                        proposal_schema_hint, review_evidence)
        if v is None:
            # Treat broken critic as REJECT (conservative).
            v = Verdict(
                decision="REJECT",
                concerns=[f"Critic with role={role} failed to respond"],
                suggested_revision=None,
                rationale="critic_call_failed",
            )
        v = _normalize_vacuous_revise(v, role)
        round1_verdicts.append(v)
    verdicts.extend(round1_verdicts)

    if all(v.is_approve for v in round1_verdicts):
        return ActorCriticResult(
            final_proposal=p1, rounds=1,
            actor_proposals=proposals, critic_verdicts=verdicts,
            accepted=True,
        )
    if any(v.is_reject for v in round1_verdicts) and max_revise_rounds < 1:
        return ActorCriticResult(
            final_proposal=None, rounds=1,
            actor_proposals=proposals, critic_verdicts=verdicts,
            accepted=False,
        )

    # Aggregate concerns into a single combined "verdict" for the revise prompt.
    aggregated = Verdict(
        decision="REVISE",
        concerns=[
            f"[{role}] {c}"
            for role, v in zip(critic_roles, round1_verdicts)
            for c in v.concerns
        ],
        suggested_revision=None,
        rationale="aggregated concerns from multiple critics",
    )

    revise_prompt = build_revise_prompt(actor_prompt, p1, aggregated)
    p2 = call_actor(api, revise_prompt)
    if p2 is None:
        return ActorCriticResult(
            final_proposal=None, rounds=2,
            actor_proposals=proposals, critic_verdicts=verdicts,
            accepted=False,
        )
    proposals.append(p2)

    round2_verdicts: list[Verdict] = []
    for role, prev_verdict in zip(critic_roles, round1_verdicts):
        # Give the round-2 Critic context about ITS OWN round-1 verdict so it
        # can check whether the revision addresses the concerns it raised.
        round2_context = task_context + (
            f"\n\nNote: this is round 2 of review. In round 1 you (role={role}) "
            f"returned: decision={prev_verdict.decision}; concerns="
            f"{prev_verdict.concerns!r}. The Actor revised in response. Now "
            f"judge whether the revised proposal addresses these concerns."
        )
        v = call_critic(api, role, round2_context, p2,
                        proposal_schema_hint, review_evidence)
        if v is None:
            v = Verdict(decision="REJECT", concerns=[f"Critic {role} failed"],
                       suggested_revision=None, rationale="critic_call_failed")
        v = _normalize_vacuous_revise(v, role)
        round2_verdicts.append(v)
    verdicts.extend(round2_verdicts)

    # After 2 rounds, multi-Critic still requires no REJECT.
    # Lingering REVISE is acceptable (Actor did its best).
    accepted = not any(v.is_reject for v in round2_verdicts)
    return ActorCriticResult(
        final_proposal=p2 if accepted else None,
        rounds=2,
        actor_proposals=proposals,
        critic_verdicts=verdicts,
        accepted=accepted,
    )


# ─── Logging helpers ──────────────────────────────────────────────────────────


def summarize_result(result: ActorCriticResult, label: str) -> str:
    """One-line summary suitable for changes.md or progress logging."""
    if result.accepted:
        n_critics = len(result.critic_verdicts) // max(result.rounds, 1)
        return (
            f"{label}: ACCEPTED after {result.rounds} round(s)"
            + (f" ({n_critics} critics)" if n_critics > 1 else "")
        )
    if not result.critic_verdicts:
        return f"{label}: actor_failed"
    decisions = [v.decision for v in result.critic_verdicts]
    return f"{label}: DISCARDED ({', '.join(decisions)})"
