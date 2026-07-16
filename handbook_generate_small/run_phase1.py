#!/usr/bin/env python3
"""Phase 1 driver for the multilang handbook pipeline.

Discovers source files for the chosen language, runs the matching adapter to
produce language-agnostic IR, and writes graph.json / functions.csv /
graph.dot / dropped_calls.json — schema-compatible with the legacy phase1.

Examples
--------
# Python project, restricted to specific files:
python3 run_phase1.py --lang python \
    --source-root /path/to/project \
    --files main.py,core.py,util.py \
    --out out/project

# Rust project (auto-discover all .rs under the root):
python3 run_phase1.py --lang rust --source-root /path/to/codex --out out/codex
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# Flat imports (ir, base, python_adapter, ...) — match the legacy layout.
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "adapters"))
sys.path.insert(0, str(_HERE / "phase1"))

import base  # noqa: E402
import build_graph  # noqa: E402
from ir import ModuleAnalysis  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Multilang Phase 1 call-graph extraction")
    ap.add_argument("--lang", default="python",
                    help=f"source language ({', '.join(base.available_languages())})")
    ap.add_argument("--source-root", required=True, type=Path,
                    help="root directory of the source tree")
    ap.add_argument("--files", default="",
                    help="comma-separated source files relative to source-root; "
                         "empty = auto-discover all files of the language")
    ap.add_argument("--out", required=True, type=Path,
                    help="output directory for the four artifacts")
    args = ap.parse_args(argv)

    source_root: Path = args.source_root.resolve()
    if not source_root.is_dir():
        ap.error(f"source-root not a directory: {source_root}")

    # --- auto: mixed-language repo -> one merged graph ---
    if args.lang == "auto":
        groups = base.discover_all(source_root)
        if not groups:
            ap.error(f"no source files of any known language under {source_root}")
        merged_funcs: list = []
        merged_edges: list = []
        scanned_files: list[str] = []
        print(f"[scan] auto root={source_root}")
        for lang, files in groups.items():
            print(f"[scan] {lang}: {len(files)} files")
            a = base.get_adapter(lang).analyze(files, source_root)
            merged_funcs.extend(a.functions)
            merged_edges.extend(a.edges)
            scanned_files.extend(str(p.relative_to(source_root)) for p in files)
        analysis = ModuleAnalysis(functions=merged_funcs, edges=merged_edges)
        build_graph.build(
            analysis, source_root=source_root, scanned_files=scanned_files,
            out_dir=args.out.resolve(), lang="multi", default_ext=".py",
        )
        return 0

    adapter = base.get_adapter(args.lang)

    if args.files.strip():
        rels = [f.strip() for f in args.files.split(",") if f.strip()]
        files = [source_root / r for r in rels]
        missing = [str(p) for p in files if not p.exists()]
        if missing:
            ap.error("missing source files:\n  " + "\n  ".join(missing))
    else:
        files = adapter.discover(source_root)
        if not files:
            ap.error(f"no {args.lang} files found under {source_root}")

    scanned_files = [str(p.relative_to(source_root)) for p in files]
    default_ext = adapter.extensions[0] if adapter.extensions else ""

    print(f"[scan] lang={args.lang} root={source_root}")
    print(f"[scan] {len(files)} files")

    analysis = adapter.analyze(files, source_root)

    build_graph.build(
        analysis,
        source_root=source_root,
        scanned_files=scanned_files,
        out_dir=args.out.resolve(),
        lang=args.lang,
        default_ext=default_ext,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
