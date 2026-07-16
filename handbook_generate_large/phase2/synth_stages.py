# -*- coding: utf-8 -*-
"""synth_stages.py — divide the system into stages from per-file purposes.

Phase 2b. Consumes the per-file purposes from read_files.py (Phase 2a) and
*synthesizes* the stage skeleton from them bottom-up, then assigns every file to
a stage. Because every file already has a purpose, coverage is complete by
construction — there is no `unaccounted_dirs` tail.

Two steps:
  A. synthesize_skeleton: roll the file purposes up to the directory level
     (bounded), hand the rollup + the call-graph entry points to the LLM, and
     ask for an ORDERED stage list (startup -> ... -> teardown) plus crosscuts.
     The order comes from the entry points / lifecycle hints, not from blind
     clustering, so the skeleton stays a narrative spine.
  B. assign files: reuse file_assign.assign_files, but feed it the purposes so
     each file is placed by what it actually does (not just its signatures).

Output: a canonical skeleton doc (skeleton_yaml shape) + the file_stage result
(same shape file_assign produces), so Phase 2c is unchanged.
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "shared"))

from api_client import Api  # noqa: E402

import file_assign  # noqa: E402
import nav_pack as navmod  # noqa: E402

logger = logging.getLogger(__name__)


# ─── Step A: synthesize the ordered skeleton ─────────────────────────────────


def _dir_rollups(file_purposes: dict[str, dict], files: list[dict],
                 *, examples_per_dir: int = 4) -> list[dict]:
    """Aggregate per-file purposes to the directory level. Directory is a strong
    structural prior and keeps the synthesis prompt bounded (n_dirs, not
    n_files). Each rollup: path, n_files, role histogram, lifecycle hints, a few
    representative purposes. `files` is the full descriptor list (incl.
    function-less files) so the rollup covers the whole source tree."""
    by_dir: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for f in files:
        rel = f["file"]
        d = f["dir"]
        by_dir[d].append((rel, file_purposes.get(rel, {})))

    rollups: list[dict] = []
    for d, items in sorted(by_dir.items()):
        roles = Counter(p.get("role", "other") for _, p in items)
        lifecycles = Counter(p.get("lifecycle", "") for _, p in items if p.get("lifecycle"))
        # Representative purposes: prefer non-empty, longer (more specific) ones.
        examples = sorted(
            (p.get("purpose", "") for _, p in items if p.get("purpose")),
            key=len, reverse=True,
        )[:examples_per_dir]
        rollups.append({
            "dir": d,
            "n_files": len(items),
            "roles": dict(roles.most_common()),
            "lifecycles": [lc for lc, _ in lifecycles.most_common(3)],
            "examples": examples,
        })
    return rollups


def _render_rollups(rollups: list[dict]) -> str:
    lines: list[str] = []
    for r in rollups:
        roles = ", ".join(f"{k}×{v}" for k, v in r["roles"].items())
        lc = ("; lifecycle=" + "/".join(r["lifecycles"])) if r["lifecycles"] else ""
        lines.append(f"- {r['dir']}  ({r['n_files']}f) roles=[{roles}]{lc}")
        for ex in r["examples"]:
            lines.append(f"    · {ex}")
    return "\n".join(lines)


_SYNTH_RULES = """You are dividing a large codebase into the STAGES of a system handbook, using
a per-directory rollup of file purposes plus the call-graph entry points.

Produce the high-altitude NARRATIVE SPINE: the ordered phases the system goes
through, from process startup through its main work loop to teardown, followed by
cross-cutting concerns (shared infrastructure that spans phases).

RULES
- Order the main stages by EXECUTION/LIFECYCLE, not alphabetically: start from
  the entry points, follow setup -> dispatch -> main loop / request handling ->
  per-unit work -> teardown. Use the lifecycle hints to order.
- Aim for 12-25 top-level stages. Use substages (set "parent" to a stage id,
  give ids like "stage-3.1") for depth instead of widening the top level.
- Put genuinely cross-cutting infrastructure (logging/telemetry, config,
  protocol/types, generic utils, persistence) into stages marked as crosscuts
  ("crosscut": true), placed after the main flow.
- Every directory in the rollup must be coverable by some stage's scope. Write
  descriptions concretely enough that a later step can assign files confidently.

OUTPUT — ONLY a JSON object in a ```json block:
{
  "metadata": {"archetype": "<one phrase describing the system shape>"},
  "stages": [
    {"id": "stage-1", "title": "...", "description": "...",
     "parent": null, "crosscut": false},
    ...
  ]
}
ids must be unique. Use "stage-N" for top-level and "stage-N.M" for substages."""


_SYNTH_RULES_ZH = """你在把一个大型代码库划分成系统手册的**阶段**（STAGE），依据是按目录聚合的文件用途
加上调用图的入口点。

产出高层的**叙事主线**：系统所经历的有序阶段——从进程启动，经主工作循环，到收尾，
最后是横切关注点（跨阶段的共享基础设施）。

规则
- 主阶段按**执行/生命周期**排序，不要按字母：从入口点开始，依次 setup -> 分发 ->
  主循环/请求处理 -> 单元工作 -> teardown。用 lifecycle 提示来排序。
- 顶层阶段目标 12-25 个。用子阶段（把 "parent" 设为某 stage id，id 形如 "stage-3.1"）
  来增加深度，而不是把顶层拉宽。
- 把真正横切的基础设施（日志/遥测、配置、协议/类型、通用工具、持久化）放进标记为
  crosscut（"crosscut": true）的阶段，置于主流程之后。
- rollup 里的每个目录都必须能被某个阶段的范围覆盖。**title 和 description 用中文写**，
  且写得足够具体，让后续步骤能据此自信地分配文件。

输出——只输出一个 ```json 块中的 JSON 对象（**JSON 的 key 用英文，title/description/archetype 的值用中文**）：
{
  "metadata": {"archetype": "<一句话描述系统形态>"},
  "stages": [
    {"id": "stage-1", "title": "...", "description": "...",
     "parent": null, "crosscut": false},
    ...
  ]
}
id 必须唯一。顶层用 "stage-N"，子阶段用 "stage-N.M"。"""


def synthesize_skeleton(api: Api, graph: dict, file_purposes: dict[str, dict],
                        lang: str = "en") -> dict:
    """Step A — synthesize the ordered, canonical skeleton doc from purposes."""
    nav = navmod.build_nav_pack(graph)
    rollups = _dir_rollups(file_purposes, navmod.all_file_descriptors(graph, nav))
    orientation_entries = "\n".join(
        f"  [{'root' if e['is_root'] else 'hint'}] {e['qualname']}  "
        f"{e['file']}:{e['line_start']}  →{e['n_callees']} callees"
        for e in nav["entry_points"][:25]
    )
    prompt = "\n".join([
        _SYNTH_RULES_ZH if lang == "zh" else _SYNTH_RULES,
        "",
        f"## System: language={nav['language']}  "
        f"files={nav['totals']['n_files']}  dirs={nav['totals']['n_dirs']}",
        "",
        "## Entry-point candidates (where execution starts)",
        orientation_entries,
        "",
        f"## Directory rollup ({len(rollups)} dirs — file purposes aggregated)",
        _render_rollups(rollups),
        "",
        "Return the JSON skeleton block only.",
    ])
    logger.info("synth_stages: synthesizing skeleton from %d dirs", len(rollups))
    result = api.call(prompt, params={"temperature": 0.0})
    parsed = result.parsed_json
    if not isinstance(parsed, dict) or not parsed.get("stages"):
        raise RuntimeError("synth_stages: LLM returned no usable stages "
                           f"(error={result.error})")
    return _normalize(parsed)


def _normalize(raw: dict) -> dict:
    """Coerce the LLM's stage list into the canonical skeleton_yaml shape:
    every stage has id/title/description/parent/children, ids are unique."""
    stages_in = raw.get("stages", []) or []
    seen_ids: set[str] = set()
    stages: list[dict] = []
    for i, s in enumerate(stages_in, 1):
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or (f"crosscut-{i}" if s.get("crosscut") else f"stage-{i}")
        while sid in seen_ids:  # guarantee uniqueness
            sid = f"{sid}-{i}"
        seen_ids.add(sid)
        title = s.get("title") or sid
        stages.append({
            "id": sid,
            "title": title,
            "description": (s.get("description") or title).strip(),
            "parent": s.get("parent"),
            "children": [],
            "crosscut": bool(s.get("crosscut")),
        })
    # Wire children from declared parents (drop dangling parent refs).
    by_id = {s["id"] for s in stages}
    for s in stages:
        if s["parent"] not in by_id:
            s["parent"] = None
    for s in stages:
        if s["parent"]:
            parent = next(p for p in stages if p["id"] == s["parent"])
            parent["children"].append(s["id"])
    return {
        "metadata": {**raw.get("metadata", {}), "version": 1,
                     "drafted_by": "synth_stages"},
        "stages": stages,
        "unread_regions": [],
        "state_registers": [],
        "subsystems": [],
    }


# ─── Step B: assign files to the synthesized stages ──────────────────────────


def assign_with_purposes(api: Api, graph: dict, skeleton_doc: dict,
                         file_purposes: dict[str, dict], **kw) -> dict:
    """Step B — place every file in a stage, using its purpose as the signal.
    Thin wrapper over file_assign.assign_files (purposes-aware)."""
    return file_assign.assign_files(api, graph, skeleton_doc,
                                    purposes=file_purposes, **kw)


def synth(api: Api, graph: dict, file_purposes: dict[str, dict],
          *, assign_workers: int = 6, assign_batch_size: int = 25,
          synth_mode: str = "oneshot", max_rounds: int = 6,
          doctor_workers: int = 1, doctor_llm_workers: int | None = None,
          lang: str = "en"
          ) -> tuple[dict, dict]:
    """Full bottom-up step: purposes -> (skeleton_doc, file_stage_result).

    synth_mode:
      - "oneshot" (default): one LLM call drafts the skeleton, then assign once.
        Original behaviour, unchanged.
      - "agent": a NexAU agent drafts the skeleton, then an actor-critic loop
        enriches it (split/merge/add stages) and reassigns affected files until
        every file is placed or `max_rounds` is hit. See synth_agent.py.
        `doctor_workers` > 1 fans the per-round diagnosis out concurrently;
        `doctor_llm_workers` caps total concurrent doctor LLM calls.
      - "doctor": like "agent" but SKIP the NexAU drafting agent — the one-shot
        synth drafts the skeleton (api_client `Api`, no NexAU / LLM_* needed) and
        the SAME doctor convergence loop enriches it. This is the "one-shot draft
        + doctor enrich" path; in practice it converges to the richest skeletons
        and needs no standard endpoint.

    `lang` ("en"/"zh") drives the language of stage title/description prose.
    """
    if synth_mode in ("agent", "doctor"):
        import synth_agent
        return synth_agent.synth_agent_loop(
            api, graph, file_purposes, max_rounds=max_rounds,
            assign_workers=assign_workers, assign_batch_size=assign_batch_size,
            doctor_workers=doctor_workers, doctor_llm_workers=doctor_llm_workers,
            use_agent_draft=(synth_mode == "agent"), lang=lang)

    skeleton_doc = synthesize_skeleton(api, graph, file_purposes, lang=lang)
    logger.info("synth_stages: %d stages (%d crosscut)", len(skeleton_doc["stages"]),
                sum(1 for s in skeleton_doc["stages"] if s.get("crosscut")))
    assign = assign_with_purposes(api, graph, skeleton_doc, file_purposes,
                                  max_workers=assign_workers,
                                  batch_size=assign_batch_size)
    return skeleton_doc, assign


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse
    import read_files
    import skeleton_yaml

    logging.basicConfig(format="[%(asctime)s][%(levelname)5s] %(message)s",
                        level=logging.INFO)
    ap = argparse.ArgumentParser(description="Synthesize stages from file purposes")
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--cards-dir", type=Path, required=True,
                    help="cards/ dir from read_files.py (2a)")
    ap.add_argument("--skeleton-out", type=Path, required=True)
    ap.add_argument("--file-stage-out", type=Path, required=True)
    args = ap.parse_args(argv)

    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    purposes = read_files.load_cards(args.cards_dir)
    api = Api()
    skeleton_doc, assign = synth(api, graph, purposes)
    skeleton_yaml.save_yaml(skeleton_doc, args.skeleton_out)
    args.file_stage_out.parent.mkdir(parents=True, exist_ok=True)
    args.file_stage_out.write_text(json.dumps(assign, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    logger.info("wrote %s (%d stages) and %s (%d/%d files assigned)",
                args.skeleton_out, len(skeleton_doc["stages"]),
                args.file_stage_out, assign["coverage"]["n_assigned"],
                assign["coverage"]["n_files"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
