# -*- coding: utf-8 -*-
"""nav_pack.py — build a *navigation pack* from graph.json.

The large-scale pipeline never reads the whole codebase. Instead, an agent
reads only the *execution spine* (main → dispatch → loop → teardown), and the
navigation pack is what tells it **where to look**:

  - dir_map            directory → (n_files, n_functions)  — the lay of the land
  - files              per-file descriptor (path, n_functions, classes, sample
                       function names/signatures) — what to read & a cheap hint
  - entry_points       internal functions with no internal caller, plus
                       name-heuristic entries (main/run/handler/...) — where the
                       spine starts
  - fan_out_top        files ranked by out-degree — orchestration suspects
  - external_subsystems boundary nodes grouped by module — the system's external
                       dependencies (nearly free from the graph)

Everything here is pure derivation from graph.json — no LLM, no source reads.
The stage-synthesis step (synth_stages.py) and the file reader (read_files.py)
both consume this.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

# Heuristic names that often mark an execution entry point across languages.
_ENTRY_NAME_HINTS = (
    "main", "run", "serve", "start", "execute", "exec", "dispatch",
    "handle", "handler", "cmd", "command", "app", "loop", "bootstrap",
)


def _dirname(file_path: str) -> str:
    d = os.path.dirname(file_path)
    return d or "."


def _internal_nodes(graph: dict) -> dict[str, dict]:
    return {
        nid: n
        for nid, n in graph["nodes"].items()
        if n.get("kind") == "internal" and not n.get("synthetic")
    }


def build_nav_pack(graph: dict, *, fan_out_top_k: int = 40,
                   sample_fns_per_file: int = 8) -> dict[str, Any]:
    """Derive the navigation pack from an in-memory graph.json dict."""
    nodes = _internal_nodes(graph)

    # ── per-file aggregation ────────────────────────────────────────────────
    by_file: dict[str, list[dict]] = defaultdict(list)
    for n in nodes.values():
        by_file[n["file"]].append(n)

    files: list[dict] = []
    for fpath, fnodes in sorted(by_file.items()):
        fnodes_sorted = sorted(fnodes, key=lambda n: n.get("line_start") or 0)
        classes = sorted({n["class_name"] for n in fnodes if n.get("class_name")})
        sample = [
            {
                "qualname": n["qualname"],
                "signature": (n.get("signature") or "")[:120],
                "line_start": n.get("line_start"),
            }
            for n in fnodes_sorted[:sample_fns_per_file]
        ]
        files.append({
            "file": fpath,
            "dir": _dirname(fpath),
            "n_functions": len(fnodes),
            "classes": classes,
            "sample_functions": sample,
        })

    # ── directory map ───────────────────────────────────────────────────────
    dir_map: dict[str, dict] = defaultdict(lambda: {"n_files": 0, "n_functions": 0})
    for f in files:
        d = dir_map[f["dir"]]
        d["n_files"] += 1
        d["n_functions"] += f["n_functions"]
    dir_map = {k: v for k, v in sorted(dir_map.items())}

    # ── entry points ────────────────────────────────────────────────────────
    # An internal node with no *internal* caller is a root (CLI entry, public
    # API, event handler). n_callers counts internal callers only.
    roots = [
        n for n in nodes.values()
        if (n.get("n_callers") or 0) == 0 and n.get("line_start") is not None
    ]
    name_hits = [
        n for n in nodes.values()
        if any(h == (n.get("name") or "").lower() or
               (n.get("name") or "").lower().startswith(h + "_")
               for h in _ENTRY_NAME_HINTS)
    ]
    seen: set[str] = set()
    entry_points: list[dict] = []
    for n in sorted(roots + name_hits,
                    key=lambda n: (-(n.get("n_callees") or 0), n["qualname"])):
        if n["qualname"] in seen:
            continue
        seen.add(n["qualname"])
        entry_points.append({
            "qualname": n["qualname"],
            "file": n["file"],
            "line_start": n.get("line_start"),
            "n_callees": n.get("n_callees") or 0,
            "is_root": (n.get("n_callers") or 0) == 0,
        })

    # ── fan-out (orchestration suspects) ────────────────────────────────────
    file_out: dict[str, int] = defaultdict(int)
    for n in nodes.values():
        file_out[n["file"]] += (n.get("n_callees") or 0)
    fan_out_top = [
        {"file": f, "out_degree": deg}
        for f, deg in sorted(file_out.items(), key=lambda kv: -kv[1])[:fan_out_top_k]
    ]

    # ── external subsystems (boundary nodes grouped by module) ──────────────
    ext: dict[str, list[str]] = defaultdict(list)
    for n in graph["nodes"].values():
        if n.get("kind") == "boundary":
            module = n.get("module") or n.get("qualname", "").rsplit(".", 1)[0]
            ext[module].append(n.get("qualname", ""))
    external_subsystems = [
        {"module": m, "n_calls_into": len(qns), "sample": sorted(set(qns))[:5]}
        for m, qns in sorted(ext.items(), key=lambda kv: -len(kv[1]))
    ]

    meta = graph.get("metadata", {})
    return {
        "language": meta.get("language"),
        "source_root": meta.get("harness_dir"),
        "totals": {
            "n_files": len(files),
            "n_functions": sum(f["n_functions"] for f in files),
            "n_dirs": len(dir_map),
            "n_external_subsystems": len(external_subsystems),
        },
        "dir_map": dir_map,
        "files": files,
        "entry_points": entry_points,
        "fan_out_top": fan_out_top,
        "external_subsystems": external_subsystems,
    }


def all_file_descriptors(graph: dict, nav: dict | None = None) -> list[dict]:
    """Every scanned source file as a descriptor, INCLUDING function-less files
    (pure type/schema/mod/re-export files) that have no call-graph nodes.

    nav["files"] only covers files that have internal functions; this widens it
    to `graph.metadata.scanned_files` so the pipeline (cards, file→stage) is
    1:1 with the actual source tree. Function-less files get an empty descriptor
    (n_functions=0, no classes/samples)."""
    if nav is None:
        nav = build_nav_pack(graph)
    by_path = {f["file"]: f for f in nav["files"]}
    out: list[dict] = list(nav["files"])
    seen = set(by_path)
    for rel in graph.get("metadata", {}).get("scanned_files", []) or []:
        if rel not in seen:
            seen.add(rel)
            out.append({"file": rel, "dir": _dirname(rel), "n_functions": 0,
                        "classes": [], "sample_functions": []})
    return out


def render_orientation(nav: dict, *, max_dirs: int = 120,
                       max_entries: int = 25, max_ext: int = 30) -> str:
    """Compact text orientation block (dir map + entry points + externals).

    Bounded by construction (dir-level, not file-level), so it fits even for a
    huge codebase. Used by this module's debug CLI; synth_stages builds its own
    entry-point block for the stage-synthesis prompt.
    """
    t = nav["totals"]
    lines = [
        f"SYSTEM: language={nav['language']}  "
        f"files={t['n_files']}  functions={t['n_functions']}  dirs={t['n_dirs']}",
        "",
        "## Directory map (dir : files / functions)",
    ]
    for d, info in list(nav["dir_map"].items())[:max_dirs]:
        lines.append(f"  {d:<50} {info['n_files']:>4}f / {info['n_functions']:>5}fn")
    if len(nav["dir_map"]) > max_dirs:
        lines.append(f"  ... ({len(nav['dir_map']) - max_dirs} more dirs)")

    lines += ["", "## Entry-point candidates (no internal caller / name hint)"]
    for e in nav["entry_points"][:max_entries]:
        tag = "root" if e["is_root"] else "hint"
        lines.append(
            f"  [{tag}] {e['qualname']:<48} {e['file']}:{e['line_start']}  "
            f"→{e['n_callees']} callees"
        )

    lines += ["", "## Highest fan-out files (orchestration suspects)"]
    for f in nav["fan_out_top"][:max_entries]:
        lines.append(f"  {f['file']:<55} out={f['out_degree']}")

    lines += ["", "## External subsystems (boundary calls grouped by module)"]
    for s in nav["external_subsystems"][:max_ext]:
        lines.append(f"  {s['module']:<45} x{s['n_calls_into']}  e.g. {s['sample'][:2]}")

    return "\n".join(lines)


# ─── CLI (standalone: dump nav pack) ─────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Build navigation pack from graph.json")
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="write nav_pack.json here")
    ap.add_argument("--orient", action="store_true", help="print orientation block")
    args = ap.parse_args(argv)

    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    nav = build_nav_pack(graph)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(nav, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}  ({nav['totals']})")
    if args.orient or not args.out:
        print(render_orientation(nav))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
