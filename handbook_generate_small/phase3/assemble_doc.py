# -*- coding: utf-8 -*-
"""Phase 3 orchestrator (v2) — build the handbook DOCUMENT TREE via the
actor-critic-reflexion loop, then render.

For every node it runs `tier_loop.produce`: generate → critic scores → (on
fail) reflect → revise → stop on pass/plateau/max_rounds → keep the
best-scoring attempt. Reflexion is per-unit and ephemeral (no cross-unit/run
memory) — each node is fixed in isolation.

Pipeline:
    Tier 1 overview → each stage's Tier 2 → each function's Tier 3
        → handbook.json (tree)  → render_md / render_html

Reuses the existing hand-authored prompts (via tier_actors) and the existing
source extraction / Tier-3 schema. Does NOT touch the legacy assemble.py.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import logging
import sys
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
_PHASE2_TOOLS = _HERE.parent / "phase2"   # new layout: phase2 lives beside phase3
for _p in (_PHASE2_TOOLS, _HERE.parent, _HERE.parent / "adapters"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from api_client import Api  # noqa: E402
from config import OUTPUT_ROOT, PHASE2_FINAL, SOURCE_ROOT  # noqa: E402
from document import FunctionNode, HandbookDoc, StageNode  # noqa: E402
from narrative import gen_register_appendix  # noqa: E402
from skeleton_view import stage_chapter_numbers, stage_render_order  # noqa: E402
from rubrics import RUBRICS  # noqa: E402
from tier_actors import (  # noqa: E402
    ground_truth_tier3,
    make_tier1_gen,
    make_tier2_gen,
    make_tier3_gen,
    parse_tier3_output,
)
from tier_loop import produce  # noqa: E402
from translate_member import collect_units  # noqa: E402

logger = logging.getLogger(__name__)


def _verdict_dict(v) -> dict | None:
    if v is None:
        return None
    return {
        "overall": v.overall,
        "passed": v.passed,
        "gate_failures": v.gate_failures,
        "scores": {k: cs.score for k, cs in v.scores.items()},
    }


def build(
    *,
    lang: str = "zh",
    stage_filter: str | None = None,
    limit_units: int | None = None,
    max_rounds: int = 3,
    max_stage_workers: int = 4,
) -> HandbookDoc:
    mapping = yaml.safe_load((PHASE2_FINAL / "mapping.yaml").read_text(encoding="utf-8"))
    skeleton = yaml.safe_load((PHASE2_FINAL / "skeleton.yaml").read_text(encoding="utf-8"))
    api = Api()

    order = stage_render_order(skeleton)
    chapters = stage_chapter_numbers(skeleton)
    stages_by_id = {s["id"]: s for s in skeleton["stages"]}
    if stage_filter:
        order = [s for s in order if s == stage_filter]

    doc = HandbookDoc(meta={"lang": lang, "max_rounds": max_rounds})

    # ── Tier 1 ───────────────────────────────────────────────────────────────
    if not stage_filter:
        logger.info("Tier 1 overview")
        # Tier 1/2 have no external ground truth — the critic scores against
        # the rubric and proposes its own fixes (see build_critic_prompt).
        r1 = produce(
            api, RUBRICS["tier1"], "",
            make_tier1_gen(api, skeleton, lang), max_rounds=max_rounds,
        )
        doc.overview_md = r1.output
        doc.overview_score = _verdict_dict(r1.verdict)
        doc.overview_findings = r1.verdict.actionable_findings if r1.verdict else []

    # ── per-stage Tier 2 + Tier 3 ────────────────────────────────────────────
    # Stages are independent, so they run concurrently (each stage is one
    # worker). WITHIN a stage, Tier 3 units stay SEQUENTIAL so each unit can
    # cross-reference its already-translated siblings (sibling_synopses). This
    # is the "stage-level parallel, unit-level serial" trade-off: it captures
    # the bulk of the speed-up (there are far more stages×units of latency than
    # a single stage's units) while preserving the cross-reference narrative.
    def _build_stage(idx: int, sid: str) -> StageNode:
        stage = stages_by_id.get(sid, {"id": sid})
        members = mapping.get("stages", {}).get(sid, {}).get("members", [])

        adj = []
        if idx > 0:
            adj.append(f"prev: {order[idx-1]} {stages_by_id.get(order[idx-1],{}).get('title','')}")
        if idx + 1 < len(order):
            adj.append(f"next: {order[idx+1]} {stages_by_id.get(order[idx+1],{}).get('title','')}")
        adjacent_brief = " | ".join(adj)

        node = StageNode(
            id=sid, chapter=chapters.get(sid, sid),
            title=stage.get("title", ""), parent=stage.get("parent"),
            children=list(stage.get("children") or []),
            members_count=len(members),
        )

        logger.info("Tier 2: %s", sid)
        r2 = produce(
            api, RUBRICS["tier2"], "",
            make_tier2_gen(api, stage, members, skeleton, adjacent_brief, lang),
            max_rounds=max_rounds,
        )
        node.logical_md = r2.output
        node.score = _verdict_dict(r2.verdict)
        node.findings = r2.verdict.actionable_findings if r2.verdict else []

        # Tier 3 — one unit per qualname, sequential so siblings can cross-ref.
        units = collect_units(sid, members, SOURCE_ROOT)
        sibling_synopses: list = []
        for u_i, unit in enumerate(units):
            if limit_units is not None and u_i >= limit_units:
                break
            logger.info("Tier 3: %s / %s", sid, unit.qualname)
            r3 = produce(
                api, RUBRICS["tier3"],
                ground_truth_tier3(unit, skeleton),
                make_tier3_gen(api, unit, skeleton, sibling_synopses, lang),
                max_rounds=max_rounds,
            )
            translation = parse_tier3_output(r3.output) or {}
            node.functions.append(FunctionNode(
                qualname=unit.qualname,
                type_kind=unit.type_kind,
                translation=translation,
                score=_verdict_dict(r3.verdict),
                findings=r3.verdict.actionable_findings if r3.verdict else [],
            ))
            sibling_synopses.append((unit.qualname, translation.get("synopsis", "")))

        return node

    if max_stage_workers <= 1 or len(order) <= 1:
        stage_nodes = {sid: _build_stage(idx, sid) for idx, sid in enumerate(order)}
    else:
        stage_nodes = {}
        with cf.ThreadPoolExecutor(max_workers=max_stage_workers) as pool:
            fut_to_sid = {
                pool.submit(_build_stage, idx, sid): sid
                for idx, sid in enumerate(order)
            }
            for fut in cf.as_completed(fut_to_sid):
                sid = fut_to_sid[fut]
                try:
                    stage_nodes[sid] = fut.result()
                except Exception as e:  # noqa: BLE001
                    logger.exception("stage %s failed: %s", sid, e)

    # Assign in skeleton render order (concurrency doesn't affect final order).
    for sid in order:
        if sid in stage_nodes:
            doc.stages[sid] = stage_nodes[sid]
            doc.order.append(sid)

    # ── register appendix (reused as-is; no critic in v1) ────────────────────
    if not stage_filter:
        try:
            doc.registers_md = gen_register_appendix(api, skeleton, False, lang)
        except Exception as e:  # noqa: BLE001
            logger.warning("register appendix failed: %s", e)

    return doc


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3 doc-tree assembler (actor-critic-reflexion)")
    ap.add_argument("--lang", default="zh", choices=["zh", "en"])
    ap.add_argument("--stage", default=None, help="only this stage id")
    ap.add_argument("--limit-units", type=int, default=None, help="cap functions per stage (smoke test)")
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--max-stage-workers", type=int, default=4,
                    help="how many stages to generate concurrently (1 = serial)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    doc = build(lang=args.lang, stage_filter=args.stage,
                limit_units=args.limit_units, max_rounds=args.max_rounds,
                max_stage_workers=args.max_stage_workers)

    pieces = ["handbook"]
    if args.stage:
        pieces.append(args.stage)
    if args.lang != "zh":
        pieces.append(args.lang)
    base = "_".join(pieces)
    json_path = OUTPUT_ROOT / f"{base}.json"
    doc.write(json_path)

    # Render markdown from the tree.
    from render_doc import render_md
    md_path = OUTPUT_ROOT / f"{base}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_md(doc, lang=args.lang), encoding="utf-8")

    print(f"tree  → {json_path}")
    print(f"md    → {md_path}")


if __name__ == "__main__":
    main()
