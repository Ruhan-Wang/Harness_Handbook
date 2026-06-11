# -*- coding: utf-8 -*-
"""The Phase 3 actor-critic-reflexion loop — within-unit, ephemeral.

For one unit (a Tier 1 overview, a Tier 2 stage, a Tier 3 function):

    output  = actor(lessons-so-far + last findings)   # LLM generates
    verdict = critic(output, ground_truth, rubric)     # LLM scores
    if pass: done
    else:
        lesson = reflect(verdict)        # LLM reflects → one guidance line
        keep it for THIS unit's next attempt
        feed the critic findings into the next attempt

Stops on: pass (overall >= T and no gate failure) OR plateau (score stopped
improving) OR max_rounds. Always returns the HIGHEST-scoring attempt.

Per-unit only — nothing persists across units or runs. The base prompt is never
modified; the accumulated lessons + findings are just appended context for the
next attempt within this unit.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable

from rubrics import Rubric, Verdict
from tier_critic import reflect, revise_findings_block, score_tier

logger = logging.getLogger(__name__)

MAX_ROUNDS = 3
PLATEAU_EPS = 0.05   # improvement smaller than this counts as "not improving"


@dataclass
class LoopResult:
    output: str
    verdict: Verdict
    rounds: int
    history: list = field(default_factory=list)   # overall score per round
    lessons: list = field(default_factory=list)    # reflexion lessons this unit


# gen(extra_context) -> generated text. `extra_context` is the lessons-so-far
# plus the last round's findings, appended to the tier's base prompt.
GenFn = Callable[[str], str]


def _lessons_block(lessons: list) -> str:
    if not lessons:
        return ""
    body = "\n".join(f"- {l}" for l in lessons)
    return "\n\n## Lessons from this review so far (address them)\n" + body + "\n"


def produce(
    api,
    rubric: Rubric,
    ground_truth: str,
    gen: GenFn,
    *,
    max_rounds: int = MAX_ROUNDS,
) -> LoopResult:
    # Cost knob: HANDBOOK_TIER_MAX_ROUNDS caps the per-unit gen+critic rounds.
    env_mr = os.environ.get("HANDBOOK_TIER_MAX_ROUNDS")
    if env_mr:
        try:
            max_rounds = max(1, int(env_mr))
        except ValueError:
            pass
    best_output: str | None = None
    best_verdict: Verdict | None = None
    history: list = []
    lessons: list = []          # within-unit only; discarded when this unit ends
    revise_block = ""

    for rnd in range(1, max_rounds + 1):
        output = gen(_lessons_block(lessons) + revise_block)
        verdict = score_tier(api, rubric, output, ground_truth)
        history.append(verdict.overall)

        # Always keep the best-scoring attempt (the last one may be worse).
        if best_verdict is None or verdict.overall > best_verdict.overall:
            best_output, best_verdict = output, verdict

        logger.info(
            "[%s] round %d/%d overall=%.2f pass=%s gates=%s",
            rubric.tier, rnd, max_rounds, verdict.overall,
            verdict.passed, verdict.gate_failures,
        )

        if verdict.passed:
            break

        # Reflexion: LLM distills one guidance line for the next attempt.
        lesson = reflect(api, verdict)
        if lesson:
            lessons.append(lesson)

        # Plateau: if this round didn't improve on the previous, stop.
        if rnd >= 2 and (history[-1] - history[-2]) < PLATEAU_EPS:
            logger.info("[%s] plateau at %.2f; stopping", rubric.tier, history[-1])
            break

        revise_block = revise_findings_block(verdict)

    return LoopResult(
        output=best_output or "",
        verdict=best_verdict,  # type: ignore[arg-type]
        rounds=len(history),
        history=history,
        lessons=lessons,
    )
