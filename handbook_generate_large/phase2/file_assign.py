# -*- coding: utf-8 -*-
"""file_assign.py — coarse file→stage assignment (batched, cheap).

This is the *partition* for the large-scale pipeline. It is NOT the directory
tree from earlier designs — the bucket is a **stage**, so the work-partition is
aligned with the narrative (no orthogonality leak), and every function gets a
strong stage prior before the expensive per-function classification.

Three jobs in one cheap O(files) pass (batched, parallel):
  1. Partition: assign each file to exactly one primary stage → buckets.
  2. Prior: that assignment seeds Phase 2 (function classification only refines
     within / around the file's stage instead of choosing from all stages).
  3. Coverage gate (file altitude): every file lands in a stage or `unassigned`
     — a mechanical, computable coverage check, unlike the skeleton altitude.

A file is the atomic unit (its functions stay together as a prior), but this is
a PRIOR not a constraint: Phase 2 may still split a file's functions across
stages. Genuinely cross-cutting files may also list a secondary crosscut stage.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "shared"))

from api_client import Api  # noqa: E402
from skeleton_yaml import stage_short_descriptions  # noqa: E402

import nav_pack as navmod  # noqa: E402

logger = logging.getLogger(__name__)


_RULES = """You are assigning whole SOURCE FILES to stages of a system handbook.

For each file you see its path, its classes, and a sample of its functions
(name + signature). Pick the ONE stage whose description best matches the file's
primary responsibility. If the file is a small cross-cutting utility (logging,
protocol/types, config plumbing, generic helpers) used across the system, set
"stage" to the best-fit crosscut stage if one exists, else the closest stage.

RULES
- "stage" MUST be one of the stage IDs in the menu. Never invent an ID.
- Assign by the file's PRIMARY identity, not by incidental imports.
- Optionally add "also" with 0-2 additional stage IDs only when the file truly
  spans them (rare). Do not pad this.
- If a file genuinely fits nowhere (generated code, vendored, dead), set
  "stage" to "unassigned".

OUTPUT — ONLY a JSON object in a ```json block:
{
  "assignments": [
    {"file": "<exact path>", "stage": "<stage-id|unassigned>", "also": []},
    ...
  ]
}
Return one entry per file given. Use the exact file paths provided."""


def _file_descriptor(f: dict, purposes: dict[str, dict] | None = None) -> str:
    classes = f.get("classes") or []
    cls = f"  classes={classes}" if classes else ""
    fns = "; ".join(
        f"{s['qualname']}{('  ' + s['signature']) if s.get('signature') else ''}"
        for s in (f.get("sample_functions") or [])[:8]
    )
    # When a per-file purpose is available (bottom-up path), it's a far stronger
    # signal than raw signatures, so lead with it.
    p = (purposes or {}).get(f["file"])
    purpose_line = ""
    if p and p.get("purpose"):
        purpose_line = (f"\n    purpose: {p['purpose']}"
                        f"  [role={p.get('role', '?')}, lifecycle={p.get('lifecycle', '?')}]")
    return (f"- {f['file']}  ({f['n_functions']} fn){cls}{purpose_line}\n"
            f"    fns: {fns or '(none sampled)'}")


def _build_batch_prompt(stage_menu: str, batch: list[dict],
                        purposes: dict[str, dict] | None = None) -> str:
    files_block = "\n".join(_file_descriptor(f, purposes) for f in batch)
    return "\n".join([
        _RULES,
        "",
        "## Stage menu (valid IDs)",
        stage_menu,
        "",
        f"## Files to assign ({len(batch)})",
        files_block,
        "",
        "Return the JSON block only, one assignment per file above.",
    ])


def _assign_batch(api: Api, stage_menu: str, valid_ids: set[str],
                  batch: list[dict], purposes: dict[str, dict] | None = None
                  ) -> dict[str, dict]:
    """Assign one batch of files. Returns {file: {stage, also}}. Files the LLM
    drops or mis-IDs are left out (the caller backfills as 'unassigned')."""
    prompt = _build_batch_prompt(stage_menu, batch, purposes)
    try:
        result = api.call(prompt, params={"temperature": 0.0})
    except Exception as e:  # noqa: BLE001
        logger.warning("file_assign batch crashed: %s", e)
        return {}
    parsed = result.parsed_json
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, dict] = {}
    batch_files = {f["file"] for f in batch}
    for a in parsed.get("assignments", []) or []:
        if not isinstance(a, dict):
            continue
        fpath = a.get("file")
        stage = a.get("stage")
        if fpath not in batch_files:
            continue
        if stage != "unassigned" and stage not in valid_ids:
            stage = "unassigned"
        also = [s for s in (a.get("also") or []) if s in valid_ids]
        out[fpath] = {"stage": stage, "also": also}
    return out


def assign_files(
    api: Api,
    graph: dict,
    skeleton_doc: dict,
    *,
    batch_size: int = 25,
    max_workers: int = 6,
    purposes: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Assign every file to a stage. Returns:
        {
          "file_stage": {file: {"stage": id, "also": [...]}},
          "buckets":    {stage_id: [file, ...]},
          "coverage":   {"n_files", "n_assigned", "unassigned": [...]},
        }

    purposes (optional) is the file_purposes map from read_files.py; when given,
    each file's one-line purpose is added to its descriptor — a much stronger
    assignment signal than signatures alone (the bottom-up path passes it).
    """
    nav = navmod.build_nav_pack(graph)
    # ALL scanned files (incl. function-less ones), so every source file is
    # assigned to a stage — matches read_files' 1:1 card coverage.
    files = navmod.all_file_descriptors(graph, nav)
    valid_ids = {s["id"] for s in skeleton_doc.get("stages", [])}
    stage_menu = "\n".join(
        f"  - {sid}: {desc}"
        for sid, desc in stage_short_descriptions(skeleton_doc).items()
    )

    batches = [files[i:i + batch_size] for i in range(0, len(files), batch_size)]
    logger.info("file_assign: %d files in %d batch(es)", len(files), len(batches))

    from progress import Progress
    prog = Progress(logger, "2b file_assign", len(batches))

    file_stage: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_assign_batch, api, stage_menu, valid_ids, b, purposes): i
                for i, b in enumerate(batches)}
        for fut in cf.as_completed(futs):
            i = futs[fut]
            try:
                file_stage.update(fut.result())
            except Exception as e:  # noqa: BLE001
                logger.warning("file_assign batch %d failed: %s", i, e)
            prog.tick(note=f"{len(file_stage)} files assigned")

    # Backfill files the LLM dropped → unassigned (coverage gate is honest).
    unassigned: list[str] = []
    buckets: dict[str, list[str]] = {sid: [] for sid in valid_ids}
    for f in files:
        fpath = f["file"]
        entry = file_stage.get(fpath)
        if entry is None or entry["stage"] == "unassigned":
            file_stage.setdefault(fpath, {"stage": "unassigned", "also": []})
            unassigned.append(fpath)
            continue
        buckets[entry["stage"]].append(fpath)

    coverage = {
        "n_files": len(files),
        "n_assigned": len(files) - len(unassigned),
        "unassigned": sorted(unassigned),
    }
    logger.info("file_assign: %d/%d assigned, %d unassigned",
                coverage["n_assigned"], coverage["n_files"], len(unassigned))
    return {"file_stage": file_stage, "buckets": buckets, "coverage": coverage}


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse
    import skeleton_yaml

    logging.basicConfig(format="[%(levelname)5s] %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(description="Assign files to stages (file→stage)")
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--skeleton", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="file_stage.json output")
    ap.add_argument("--batch-size", type=int, default=25)
    args = ap.parse_args(argv)

    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    skeleton_doc = skeleton_yaml.load_yaml(args.skeleton)
    api = Api()
    res = assign_files(api, graph, skeleton_doc, batch_size=args.batch_size)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    bucket_sizes = {k: len(v) for k, v in res["buckets"].items() if v}
    logger.info("wrote %s", args.out)
    logger.info("bucket sizes: %s", dict(sorted(bucket_sizes.items(),
                                                key=lambda kv: -kv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
