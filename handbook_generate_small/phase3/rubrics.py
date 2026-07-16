# -*- coding: utf-8 -*-
"""Per-tier rubrics + scoring model for the Phase 3 actor-critic-reflexion loop.

Each tier has a DIFFERENT purpose, so each has its own rubric — criteria, which
ones are hard gates, weights, and a pass threshold. A uniform rubric would be
wrong: simplification is a virtue in Tier 1 and a sin in Tier 3, so "accuracy"
and "completeness" mean different things per tier.

Purposes (the rubrics are derived from these):
  - Tier 1: let a complete novice understand the Harness's PURPOSE and WHAT it
            does. Simplification/omission are virtues here.
  - Tier 2: convey each stage's ROLE + RUNNING LOGIC (control flow) + DATA FLOW.
  - Tier 3: a PRECISE per-function analysis — inputs/outputs, parameters, code
            details. Literal accuracy and completeness are virtues; omission of
            non-obvious behavior is a sin.

A criterion is scored 1-5 (1=fails, 3=acceptable, 5=excellent). A GATE criterion
carries a `floor`: if its score dips below the floor the whole unit fails, no
matter how high the weighted average is (a beautiful-but-wrong output must not
pass). SOFT criteria only feed the weighted average.

pass == overall >= threshold AND no gate dipped below its floor.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ─── Rubric definition ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Criterion:
    name: str
    kind: str            # "gate" | "soft"
    weight: float        # contribution to the weighted average
    check: str           # what the critic must look for (goes into the prompt)
    floor: int = 3       # gate only: min acceptable score (ignored for soft)


@dataclass(frozen=True)
class Rubric:
    tier: str
    purpose: str         # one-line purpose, embedded in the critic prompt
    critic_mindset: str  # how the critic should think for THIS tier
    criteria: tuple[Criterion, ...]
    threshold: float = 4.0   # per-tier pass bar on the weighted average

    def gates(self) -> list[Criterion]:
        return [c for c in self.criteria if c.kind == "gate"]

    def to_prompt_block(self) -> str:
        """Render WHAT this tier is judged on: purpose, mindset, criteria, gates.
        HOW to score (the 1-5 scale + calibration) lives in the critic prompt
        (build_critic_prompt), so it isn't duplicated or coupled to a threshold
        here."""
        lines = [
            f"Tier purpose: {self.purpose}",
            f"Your mindset for THIS tier: {self.critic_mindset}",
            "",
            "Criteria:",
        ]
        for c in self.criteria:
            tag = f"GATE (floor {c.floor})" if c.kind == "gate" else "soft"
            lines.append(f"  - `{c.name}` [{tag}] — {c.check}")
        lines += [
            "",
            "GATE criteria are non-negotiable: scoring one below its floor means "
            "the whole output fails regardless of the others. Do not reward a "
            "well-written but inaccurate output.",
        ]
        return "\n".join(lines)

    def verdict_schema_hint(self) -> str:
        names = ", ".join(f'"{c.name}"' for c in self.criteria)
        return (
            "Return ONLY a JSON object inside a ```json fence:\n"
            "{\n"
            '  "scores": {\n'
            f"    // one entry per criterion: {names}\n"
            '    "<criterion>": {"score": <1-5>, "evidence": "<why>", '
            '"findings": ["<fixable point>", "..."]}\n'
            "  },\n"
            '  "actionable_findings": ["<the single most important fixes, '
            'phrased as instructions to the writer>"]\n'
            "}"
        )


# ─── Scored verdict ──────────────────────────────────────────────────────────


@dataclass
class CriterionScore:
    name: str
    score: int
    evidence: str = ""
    findings: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    tier: str
    scores: dict[str, CriterionScore]
    overall: float
    gate_failures: list[str]
    passed: bool
    actionable_findings: list[str] = field(default_factory=list)


def compute_verdict(
    rubric: Rubric,
    raw_scores: dict,
    actionable_findings: list[str] | None = None,
) -> Verdict:
    """Turn the critic's raw per-criterion scores into a Verdict.

    Missing / malformed criterion scores are treated as 1 (worst) so a critic
    that silently drops a criterion can't accidentally let a unit pass.
    """
    scores: dict[str, CriterionScore] = {}
    for c in rubric.criteria:
        raw = raw_scores.get(c.name) if isinstance(raw_scores, dict) else None
        if isinstance(raw, dict):
            try:
                s = int(raw.get("score", 1))
            except (TypeError, ValueError):
                s = 1
            s = max(1, min(5, s))
            scores[c.name] = CriterionScore(
                name=c.name,
                score=s,
                evidence=str(raw.get("evidence", "")),
                findings=[str(x) for x in (raw.get("findings") or [])],
            )
        else:
            scores[c.name] = CriterionScore(name=c.name, score=1)

    total_w = sum(c.weight for c in rubric.criteria) or 1.0
    overall = sum(scores[c.name].score * c.weight for c in rubric.criteria) / total_w

    gate_failures = [
        c.name for c in rubric.gates() if scores[c.name].score < c.floor
    ]
    passed = overall >= rubric.threshold and not gate_failures

    return Verdict(
        tier=rubric.tier,
        scores=scores,
        overall=round(overall, 3),
        gate_failures=gate_failures,
        passed=passed,
        actionable_findings=[str(x) for x in (actionable_findings or [])],
    )


# ─── The three rubrics ───────────────────────────────────────────────────────


TIER1_RUBRIC = Rubric(
    tier="tier1",
    purpose=(
        "Let a COMPLETE NOVICE understand the codebase's purpose and what it "
        "does. Simplification and omission are virtues here, not flaws."
    ),
    critic_mindset=(
        "You are reviewing for a reader with zero context. Ask 'would a "
        "newcomer walk away knowing what this thing is for and what it does?' "
        "Do NOT demand code-level accuracy or completeness — that belongs to "
        "deeper tiers. Penalizing healthy simplification is a category error."
    ),
    criteria=(
        Criterion("purpose_clarity", "gate", 0.25,
                  "Clearly answers what the codebase is for, why it exists, what "
                  "problem it solves. The reader learns why it exists."),
        Criterion("what_it_does", "gate", 0.20,
                  "Conveys, at a high level, what the system actually does — its "
                  "core components and how control/data flows through them."),
        Criterion("novice_accessibility", "gate", 0.20,
                  "Jargon is inline-defined on first use; a zero-context reader "
                  "is never stranded; analogies used where they help."),
        Criterion("shape_fidelity", "soft", 0.20,
                  "The mental model is faithful to the real shape of the system "
                  "as described by the skeleton (its top-level stages and the "
                  "primary flow — a loop, a one-shot pipeline, etc.)."),
        Criterion("scannability_style", "soft", 0.15,
                  "Short, structured, scannable; no throat-clearing; any "
                  "diagrams aid the gestalt rather than bury it in detail."),
    ),
    threshold=4.0,
)


TIER2_RUBRIC = Rubric(
    tier="tier2",
    purpose=(
        "Convey what each stage DOES, its RUNNING LOGIC (control flow), and its "
        "DATA FLOW (which state registers move)."
    ),
    critic_mindset=(
        "You are an engineer explaining how the system runs. The core question "
        "is whether the control flow and the data flow are correct. Bounded "
        "simplification is fine, but the state-flow must be exact. Sliding into "
        "a function-by-function recital (Tier 3's job) is wrong."
    ),
    criteria=(
        Criterion("stage_role", "gate", 0.18,
                  "Clearly states what THIS stage does and its role in a run."),
        Criterion("control_flow", "gate", 0.22,
                  "The running logic — decision points, branches, step order — "
                  "is accurate and matches the members + skeleton description."),
        Criterion("data_flow_validity", "gate", 0.25,
                  "The State Flow block is present and every register read / "
                  "written / cleared is real (from the input register list) and "
                  "in the right direction."),
        Criterion("explains_why", "soft", 0.15,
                  "Explains the design rationale, not just the what."),
        Criterion("adjacency", "soft", 0.10,
                  "Hands off correctly to the previous / next stage."),
        Criterion("right_altitude_style", "soft", 0.10,
                  "Stays at stage altitude — neither drowning in line-level "
                  "detail nor merely repeating the Tier 1 overview."),
    ),
    threshold=4.0,
)


TIER3_RUBRIC = Rubric(
    tier="tier3",
    purpose=(
        "A PRECISE per-function analysis: inputs/outputs, parameters, and code "
        "details, every claim anchored to the source."
    ),
    critic_mindset=(
        "You are a senior engineer checking the analysis against the actual "
        "source, line by line. Be strict about parameters, I/O, and state "
        "mutations. Vagueness and omission of non-obvious behavior are sins "
        "here (the opposite of Tier 1). Mechanical shape checks are already "
        "done elsewhere — judge semantic correctness."
    ),
    criteria=(
        Criterion("accuracy", "gate", 0.25,
                  "Every claim (synopsis, execution_flow/gloss, "
                  "design_decisions) matches the source. No hallucinated "
                  "behavior, no speculation about unseen/future code."),
        Criterion("io_params_precision", "gate", 0.25,
                  "The interface is precise and correct: parameters, inputs "
                  "(args + self._ read), outputs (return + self._ written + "
                  "side effects). Nothing important missing or wrong."),
        Criterion("register_accuracy", "gate", 0.15,
                  "register_interactions faithfully reflect the source's state "
                  "mutations (self._pending_*, .append, += ...); actions and "
                  "register ids are correct. (N/A → full score if the function "
                  "genuinely touches no register.)"),
        Criterion("code_detail_fidelity", "soft", 0.15,
                  "execution_flow / region glosses capture the real code steps "
                  "and branches at an appropriate granularity."),
        Criterion("non_obvious_surfaced", "soft", 0.12,
                  "design_decisions surface the genuinely non-obvious choices "
                  "(swallowed exceptions, explicit None checks, fallbacks)."),
        Criterion("section_purity_style", "soft", 0.08,
                  "What stays in synopsis, Why stays in design_decisions; no "
                  "bleed. Active voice, identifiers in backticks."),
    ),
    threshold=4.0,
)


RUBRICS: dict[str, Rubric] = {
    "tier1": TIER1_RUBRIC,
    "tier2": TIER2_RUBRIC,
    "tier3": TIER3_RUBRIC,
}
