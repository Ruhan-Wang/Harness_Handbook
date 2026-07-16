# -*- coding: utf-8 -*-
"""organize_stages.py — intra-stage organization (file-level 2c, cheap).

A scalable alternative to function-level classification. On a
large codebase, classifying every function is ~16× more LLM work than the file
count buys, and most of that detail is never read. This pass instead organizes
each stage *internally* at the FILE level:

  1. Order the stage's files by execution/dependency flow — the call graph gives
     a prior (a file whose functions call into another file's functions comes
     first), then the LLM refines into a readable narrative order.
  2. Split the stage into 2-N sub-groups (sub-themes) so a big stage (e.g. 200
     files) reads as a handful of coherent sections instead of a flat list.

Cost: O(stages) LLM calls (one per stage; a huge stage still fits because each
file contributes a single purpose line), versus O(functions) for the per-function
path. What it keeps: stage structure, file order, sub-grouping — what a
high-altitude handbook reader needs. What it drops vs function classification:
per-function purpose, splitting a file's functions across stages, region splits.

Output (stage_organization.yaml shape):
    {
      "metadata": {...},
      "stages": {
        "stage-1": {
          "title": "...",
          "groups": [
            {"title": "...", "summary": "...",
             "files": [{"file": ..., "purpose": ..., "role": ..., "n_functions": N}, ...]},
            ...
          ],
          "ordered_files": [<path>, ...],   # flat order across groups
        },
        ...
      },
      "coverage": {"n_files": int, "n_organized": int},
    }
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "shared"))

from api_client import Api  # noqa: E402

logger = logging.getLogger(__name__)


# ─── Call-graph priors (file ordering) ───────────────────────────────────────


def file_call_adjacency(graph: dict) -> dict[str, set[str]]:
    """Global file→{files it calls into} adjacency from internal call edges.
    `A in adj[B]` means a function in B calls a function in A — i.e. B → A."""
    id_to_file = {nid: n.get("file") for nid, n in graph["nodes"].items()
                  if n.get("kind") == "internal"}
    adj: dict[str, set[str]] = defaultdict(set)
    for e in graph.get("edges", []):
        cf_, ce = e.get("caller_id"), e.get("callee_id")
        fa, fb = id_to_file.get(cf_), id_to_file.get(ce)
        if fa and fb and fa != fb:
            adj[fa].add(fb)
    return adj


def suggest_order(files: list[str], adj: dict[str, set[str]]) -> list[str]:
    """Order files so callers come before callees (entry → leaf), restricted to
    `files`. Kahn's algorithm on the in-stage subgraph; ties and cycles broken
    by out-degree (orchestrators first) then path."""
    fileset = set(files)
    out_edges = {f: (adj.get(f, set()) & fileset) - {f} for f in files}
    indeg = {f: 0 for f in files}
    for f in files:
        for g in out_edges[f]:
            indeg[g] += 1

    def tiebreak(f: str) -> tuple:
        return (-len(out_edges[f]), f)

    ready = sorted([f for f in files if indeg[f] == 0], key=tiebreak)
    order: list[str] = []
    seen: set[str] = set()
    while ready:
        f = ready.pop(0)
        if f in seen:
            continue
        seen.add(f)
        order.append(f)
        newly = []
        for g in out_edges[f]:
            indeg[g] -= 1
            if indeg[g] == 0 and g not in seen:
                newly.append(g)
        if newly:
            ready = sorted(set(ready) | set(newly), key=tiebreak)
    # Leftovers (cycles) appended deterministically.
    for f in sorted(files, key=tiebreak):
        if f not in seen:
            order.append(f)
    return order


# ─── Per-stage LLM organization ──────────────────────────────────────────────


_RULES = """You are organizing the files of ONE stage of a system handbook into a readable
structure. You are given the stage's files, each with a one-line purpose, in a
suggested execution order (derived from the call graph: callers before callees).

Your job:
1. Group the files into 2-8 coherent SUB-GROUPS (sub-themes within the stage).
   A small stage may be a single group; a large one needs several.
2. Within each group, order the files so they read as a narrative
   (entry/setup → core work → finalization), respecting the suggested order and
   the "calls into" hints where they make sense.
3. Order the groups themselves the same way.

RULES
- EVERY file given must appear in EXACTLY ONE group. Do not drop or duplicate.
- Use the EXACT file paths provided.
- Group titles are short noun phrases; the summary is one sentence.

OUTPUT — ONLY a JSON object in a ```json block:
{
  "groups": [
    {"title": "...", "summary": "...", "files": ["<exact path>", ...]},
    ...
  ]
}"""


_RULES_ZH = """你在把系统手册中**某一个阶段**的文件组织成可读的结构。给你的是该阶段的文件，
每个带一句用途，并按建议的执行顺序排列（来自调用图：调用者在被调用者之前）。

你的任务：
1. 把文件分成 2-8 个连贯的**子组**（阶段内的子主题）。小阶段可以是单个组；大阶段需要多个。
2. 每个组内，按叙事顺序排列文件（入口/setup → 核心工作 → 收尾），尊重建议顺序和
   "calls into" 提示。
3. 组与组之间也按同样的顺序排列。

规则
- 给定的**每个**文件必须恰好出现在**一个**组里。不要丢失或重复。
- 使用提供的确切文件路径。
- 组标题用简短的中文名词短语；summary 用一句中文。

输出——只输出一个 ```json 块中的 JSON 对象（**JSON 的 key 用英文，title/summary 的值用中文**）：
{
  "groups": [
    {"title": "...", "summary": "...", "files": ["<exact path>", ...]},
    ...
  ]
}"""


def _build_stage_prompt(stage: dict, ordered_files: list[str],
                        file_info: dict[str, dict],
                        adj: dict[str, set[str]], lang: str = "en") -> str:
    fileset = set(ordered_files)
    lines: list[str] = []
    for f in ordered_files:
        info = file_info.get(f, {})
        calls = sorted((adj.get(f, set()) & fileset) - {f})
        calls_s = f"  calls→ {calls[:4]}" if calls else ""
        lines.append(
            f"- {f}  [{info.get('role', '?')}, {info.get('n_functions', 0)} fn]"
            f"\n    {info.get('purpose') or '(no purpose)'}{calls_s}")
    return "\n".join([
        _RULES_ZH if lang == "zh" else _RULES,
        "",
        f"## Stage: {stage['id']} — {stage.get('title', '')}",
        stage.get("description", ""),
        "",
        f"## Files in this stage ({len(ordered_files)}, suggested execution order)",
        "\n".join(lines),
        "",
        "Return the JSON groups block only; cover every file exactly once.",
    ])


def _organize_one_stage(api: Api, stage: dict, files: list[str],
                        file_info: dict[str, dict],
                        adj: dict[str, set[str]], lang: str = "en") -> dict:
    """Return the organized stage entry: {title, groups, ordered_files}."""
    ordered = suggest_order(files, adj)
    sid = stage["id"]
    if len(files) == 1:
        # Trivial — no LLM call needed.
        groups = [{"title": stage.get("title", sid), "summary": "",
                   "files": list(files)}]
        return _finalize_stage(stage, groups, files, file_info)

    prompt = _build_stage_prompt(stage, ordered, file_info, adj, lang)
    try:
        result = api.call(prompt, params={"temperature": 0.0})
        parsed = result.parsed_json
    except Exception as e:  # noqa: BLE001
        logger.warning("[organize %s] LLM crashed: %s — falling back to flat order", sid, e)
        parsed = None

    groups_in = parsed.get("groups") if isinstance(parsed, dict) else None
    if not groups_in:
        groups = [{"title": stage.get("title", sid), "summary": "(ungrouped)",
                   "files": ordered}]
        return _finalize_stage(stage, groups, files, file_info)

    # Validate: keep only known files, dedup across AND within groups, backfill
    # missing. The dedup must update `seen` as it walks each file (not once per
    # group): the LLM sometimes lists the same path twice inside ONE group, and
    # a per-group `seen.update()` after building the list would let that intra-
    # group duplicate through (the path isn't in `seen` yet while the list is
    # built), inflating ordered_files and the coverage count.
    seen: set[str] = set()
    groups: list[dict] = []
    valid = set(files)
    for g in groups_in:
        if not isinstance(g, dict):
            continue
        gf: list[str] = []
        for f in (g.get("files") or []):
            if f in valid and f not in seen:
                seen.add(f)
                gf.append(f)
        if gf:
            groups.append({"title": (g.get("title") or "").strip() or "Group",
                           "summary": (g.get("summary") or "").strip(),
                           "files": gf})
    missing = [f for f in ordered if f not in seen]
    if missing:
        logger.info("[organize %s] %d file(s) not placed by LLM → 'Other'",
                    sid, len(missing))
        groups.append({"title": "Other", "summary": "(not placed by the model)",
                       "files": missing})
    return _finalize_stage(stage, groups, files, file_info)


def _finalize_stage(stage: dict, groups: list[dict], files: list[str],
                    file_info: dict[str, dict]) -> dict:
    """Attach per-file info inline and compute the flat ordered_files list."""
    ordered_files: list[str] = []
    for g in groups:
        enriched = []
        for f in g["files"]:
            info = file_info.get(f, {})
            enriched.append({"file": f, "purpose": info.get("purpose", ""),
                             "role": info.get("role", ""),
                             "n_functions": info.get("n_functions", 0)})
            ordered_files.append(f)
        g["files"] = enriched
    return {"title": stage.get("title", stage["id"]),
            "groups": groups, "ordered_files": ordered_files}


# ─── Driver ──────────────────────────────────────────────────────────────────


def _file_info_map(graph: dict, file_purposes: dict[str, dict]) -> dict[str, dict]:
    n_functions: dict[str, int] = defaultdict(int)
    for n in graph["nodes"].values():
        if n.get("kind") == "internal" and not n.get("synthetic"):
            n_functions[n.get("file")] += 1
    info: dict[str, dict] = {}
    for f, p in file_purposes.items():
        info[f] = {"purpose": p.get("purpose", ""), "role": p.get("role", ""),
                   "n_functions": n_functions.get(f, 0)}
    return info


def organize(api: Api, graph: dict, skeleton_doc: dict, assign: dict,
             file_purposes: dict[str, dict], *, workers: int = 8,
             lang: str = "en") -> dict:
    """Organize every stage's files into ordered sub-groups. Returns the
    stage_organization doc (see module docstring)."""
    adj = file_call_adjacency(graph)
    file_info = _file_info_map(graph, file_purposes)
    buckets = assign["buckets"]
    stages_by_id = {s["id"]: s for s in skeleton_doc.get("stages", [])}

    # Only stages that actually have files.
    work = [(sid, files) for sid, files in buckets.items() if files and sid in stages_by_id]
    files_by_sid = dict(work)   # for the flat fallback on a per-stage failure
    logger.info("organize_stages: %d non-empty stage(s)", len(work))

    from progress import Progress
    prog = Progress(logger, "2c organize", len(work))

    out_stages: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_organize_one_stage, api, stages_by_id[sid], files,
                            file_info, adj, lang): sid for sid, files in work}
        for fut in cf.as_completed(futs):
            sid = futs[fut]
            try:
                out_stages[sid] = fut.result()
            except Exception as e:  # noqa: BLE001
                # A per-stage failure must NOT drop the stage's files from the
                # artifact (2b guaranteed every file a stage; silently losing
                # them here would break that contract and leave only a coverage
                # count gap as evidence). Fall back to a single flat group in
                # deterministic call-graph order — no LLM needed.
                logger.exception("organize stage %s failed: %s — flat fallback", sid, e)
                files = files_by_sid.get(sid, [])
                stage = stages_by_id[sid]
                out_stages[sid] = _finalize_stage(
                    stage,
                    [{"title": stage.get("title", sid),
                      "summary": "(organize failed; flat call-graph order)",
                      "files": suggest_order(files, adj)}],
                    files, file_info)
            prog.tick(note=f"stage {sid}")

    n_org = sum(len(s["ordered_files"]) for s in out_stages.values())
    # Re-key in skeleton order for a readable artifact.
    ordered = {s["id"]: out_stages[s["id"]]
               for s in skeleton_doc["stages"] if s["id"] in out_stages}
    # n_files counts DISTINCT files across buckets: file_assign appends each file
    # only to its primary stage, so buckets are already disjoint, but dedup keeps
    # the coverage ratio honest even if that ever changes (a file double-counted
    # would otherwise make n_files exceed the real total).
    all_bucket_files: set[str] = set()
    for fs in buckets.values():
        all_bucket_files.update(fs)
    return {
        "metadata": {"phase2_organize": True, "n_stages": len(ordered)},
        "stages": ordered,
        "coverage": {"n_files": len(all_bucket_files),
                     "n_organized": n_org},
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse
    import skeleton_yaml
    import yaml

    logging.basicConfig(format="[%(asctime)s][%(levelname)5s] %(message)s",
                        level=logging.INFO)
    ap = argparse.ArgumentParser(description="Organize each stage's files (file-level 2c)")
    import read_files
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--skeleton", type=Path, required=True)
    ap.add_argument("--file-stage", type=Path, required=True)
    ap.add_argument("--cards-dir", type=Path, required=True,
                    help="cards/ dir from read_files.py (2a)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--lang", choices=["en", "zh"], default="en",
                    help="group title/summary language (en default; zh = Chinese)")
    args = ap.parse_args(argv)

    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    skeleton_doc = skeleton_yaml.load_yaml(args.skeleton)
    assign = json.loads(args.file_stage.read_text(encoding="utf-8"))
    purposes = read_files.load_cards(args.cards_dir)

    api = Api()
    doc = organize(api, graph, skeleton_doc, assign, purposes,
                   workers=args.workers, lang=args.lang)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True,
                                       width=10000), encoding="utf-8")
    logger.info("wrote %s (%d stages, %d/%d files organized)", args.out,
                len(doc["stages"]), doc["coverage"]["n_organized"],
                doc["coverage"]["n_files"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
