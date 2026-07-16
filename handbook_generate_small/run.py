#!/usr/bin/env python3
"""End-to-end driver for the multilang handbook pipeline.

Chains the three phases for any supported language (python / rust / typescript
/ go):

  Phase 1  run_phase1.py            source -> graph.json     (no LLM)
  Phase 2  phase2/iterate_phase2.py graph + skeleton -> mapping.yaml  (LLM)
  Phase 3  phase3/assemble_doc.py   mapping -> handbook.md / .json     (LLM)

Phase 1 is pure static analysis. Phases 2 & 3 call the internal LLM endpoint
(see phase2/api_client.py) — they need network access to it. Use --phase to run
a subset (e.g. --phase 1 to only extract the graph).

Example
-------
python3 run.py \
    --lang rust \
    --source-root /path/to/codex \
    --skeleton skeletons/codex.yaml \
    --work-dir work/codex \
    --title "Codex Handbook" \
    --out-lang en
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _run(cmd: list[str], env: dict | None = None) -> None:
    printable = " ".join(str(c) for c in cmd)
    print(f"\n$ {printable}\n", flush=True)
    result = subprocess.run([str(c) for c in cmd], env=env)
    if result.returncode != 0:
        sys.exit(f"[run] step failed (exit {result.returncode}): {printable}")


def _child_env() -> dict:
    """PYTHONPATH so the phase2/phase3 flat imports + adapter imports resolve
    regardless of cwd."""
    env = dict(os.environ)
    extra = [str(_HERE), str(_HERE / "adapters"), str(_HERE / "phase2"), str(_HERE / "phase3")]
    env["PYTHONPATH"] = os.pathsep.join(extra + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Multilang handbook pipeline (phase1->2->3)")
    ap.add_argument("--lang", default="python", help="source language: python | rust | typescript | go")
    ap.add_argument("--source-root", required=True, type=Path)
    ap.add_argument("--files", default="", help="comma-separated files (phase1); empty = auto-discover")
    ap.add_argument("--skeleton", type=Path, help="user-authored skeleton.yaml (required for phase 2+)")
    ap.add_argument("--work-dir", required=True, type=Path, help="dir for intermediates + outputs")
    ap.add_argument("--title", default="Handbook", help="handbook H1 title")
    ap.add_argument("--project-name", default="",
                    help="short display name of the codebase (e.g. 'Redis'); "
                         "injected into every LLM prompt so the handbook is not "
                         "hardcoded to any one project")
    ap.add_argument("--project-brief", default="",
                    help="1-3 sentence description of what the codebase is/does")
    ap.add_argument("--project-brief-file", type=Path, default=None,
                    help="read the project brief from a file (overrides --project-brief)")
    ap.add_argument("--project-kind", default="",
                    help="noun for the codebase, e.g. 'agent harness', 'web "
                         "service', 'compiler' (default: 'codebase')")
    ap.add_argument("--out-lang", default="zh", choices=["zh", "en"], help="handbook output language")
    ap.add_argument("--phase", default="all", help="which phases: all | 1 | 2 | 3 | 1-2 | 2-3")
    ap.add_argument("--max-iters", type=int, default=10, help="phase 2 max iterations")
    ap.add_argument("--max-rounds", type=int, default=3, help="phase 3 critic rounds per unit")
    ap.add_argument("--max-stage-workers", type=int, default=4,
                    help="phase 3: how many stages to generate concurrently (1 = serial)")
    ap.add_argument("--limit", type=int, default=None, help="phase 2: cap functions (smoke test)")
    ap.add_argument("--limit-units", type=int, default=None, help="phase 3: cap functions/stage (smoke test)")
    args = ap.parse_args(argv)

    source_root = args.source_root.resolve()
    work = args.work_dir.resolve()
    phases = _expand_phases(args.phase)

    # Project identity injected into every LLM prompt (Phase 2 + Phase 3), so
    # the handbook is generated for THIS codebase rather than any hardcoded one.
    project_brief = args.project_brief
    if args.project_brief_file:
        project_brief = args.project_brief_file.read_text(encoding="utf-8").strip()
    project_env = {
        # Fall back to the handbook title when no explicit project name is given.
        "HANDBOOK_PROJECT_NAME": args.project_name or args.title,
        "HANDBOOK_PROJECT_BRIEF": project_brief,
        "HANDBOOK_PROJECT_KIND": args.project_kind or "codebase",
    }

    p1_out = work / "phase1"
    graph = p1_out / "graph.json"
    p2_iters = work / "phase2" / "iterations"
    p2_final = p2_iters / "final"
    mapping = work / "phase2" / "mapping.yaml"
    p3_root = work / "phase3"

    env = _child_env()
    env.update(project_env)

    # ---- Phase 1 ----
    if 1 in phases:
        cmd = [sys.executable, _HERE / "run_phase1.py",
               "--lang", args.lang, "--source-root", source_root, "--out", p1_out]
        if args.files.strip():
            cmd += ["--files", args.files]
        _run(cmd, env)

    # ---- Phase 2 ----
    if 2 in phases:
        if not args.skeleton:
            sys.exit("[run] phase 2 needs --skeleton (the user-authored skeleton.yaml)")
        if not graph.exists():
            sys.exit(f"[run] phase 2 needs {graph} — run phase 1 first")
        cmd = [sys.executable, _HERE / "phase2" / "iterate_phase2.py",
               "--graph", graph, "--source-root", source_root,
               "--skeleton-yaml", args.skeleton.resolve(),
               "--mapping", mapping, "--iterations-dir", p2_iters,
               "--max-iters", args.max_iters]
        if args.limit is not None:
            cmd += ["--limit", args.limit]
        _run(cmd, env)

    # ---- Phase 3 ----
    if 3 in phases:
        if not p2_final.exists():
            sys.exit(f"[run] phase 3 needs {p2_final} — run phase 2 first")
        p3_env = dict(env)
        p3_env["HANDBOOK_SOURCE_ROOT"] = str(source_root)
        p3_env["HANDBOOK_PHASE2_FINAL"] = str(p2_final)
        p3_env["HANDBOOK_PHASE3_ROOT"] = str(p3_root)
        p3_env["HANDBOOK_TITLE"] = args.title
        cmd = [sys.executable, _HERE / "phase3" / "assemble_doc.py",
               "--lang", args.out_lang, "--max-rounds", args.max_rounds,
               "--max-stage-workers", args.max_stage_workers]
        if args.limit_units is not None:
            cmd += ["--limit-units", args.limit_units]
        _run(cmd, p3_env)
        print(f"\n[done] handbook in {p3_root / 'output'}/")

    return 0


def _expand_phases(spec: str) -> set[int]:
    spec = spec.strip().lower()
    if spec == "all":
        return {1, 2, 3}
    if "-" in spec:
        a, b = spec.split("-", 1)
        return set(range(int(a), int(b) + 1))
    return {int(spec)}


if __name__ == "__main__":
    raise SystemExit(main())
