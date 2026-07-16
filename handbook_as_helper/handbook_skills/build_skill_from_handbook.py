#!/usr/bin/env python3
"""build_skill_from_handbook.py — assemble a planner-ready handbook skill from a
rendered handbook directory, for ANY target.

The handbook arm gives the planner a `handbook_skill_<target>/` directory containing:

    SKILL.md              — a short navigation guide (how to use the references)
    references/
        overview.md       — the system overview
        index.md          — the stage index (the routing backbone)
        registers.md      — state-flow registers (if the handbook has them)
        stages/<id>.md    — one page per stage

Different handbook generators emit slightly different filenames (e.g. `register.md`
vs `registers.md`). This script copies whatever exists into the canonical layout the
planner prompt names, so the same prompt works across targets.

Usage:
    python build_skill_from_handbook.py                  # active EVAL_TARGET
    python build_skill_from_handbook.py --target codex
    EVAL_TARGET=codex python build_skill_from_handbook.py --src /path/to/handbook
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from targets import get_target, target_names  # noqa: E402

_SKILL_MD = """---
name: {name}-handbook
description: A navigation index for the {project} codebase. Use it to locate every
  file/function/site a change must touch before reading the real source.
---

# {project} Handbook — how to use it

This handbook is a **location index**, not a description of the code. Use it to ROUTE
to the right files and functions, then read the REAL source for the exact text.

1. Read `references/overview.md` for the system shape.
2. Read `references/index.md` — the stage index. It maps every subsystem to its
   files. Follow the link for the stage(s) your change touches.
3. Open the relevant `references/stages/<id>.md` page(s): each lists the files in
   that stage and, where available, per-function detail (purpose, signature, line
   ranges, call relations).
4. If `references/registers.md` exists, it lists cross-cutting state and where it is
   read/written — invaluable for fan-out changes.
5. Then `read_file` the actual source at the addresses you found, and plan the edits.

Do NOT treat the handbook as ground truth for code text — always confirm against the
real source before emitting a verbatim edit.
"""

# canonical reference filename -> candidate source names in the rendered handbook
_REF_MAP = {
    "overview.md": ["overview.md"],
    "index.md": ["index.md"],
    "registers.md": ["registers.md", "register.md"],
}


def build(target_name: str, src: Path | None, dest: Path | None) -> Path:
    t = get_target(target_name)
    src = src or t.handbook_rendered
    if not src or not Path(src).exists():
        raise FileNotFoundError(
            f"no rendered handbook for target '{t.name}'. Looked at: {src}. "
            "Pass --src /path/to/handbook (a dir with index.md + stages/)."
        )
    src = Path(src)
    dest = dest or t.handbook_skill
    refs = dest / "references"
    if dest.exists():
        shutil.rmtree(dest)
    (refs / "stages").mkdir(parents=True, exist_ok=True)

    # SKILL.md
    (dest / "SKILL.md").write_text(
        _SKILL_MD.format(name=t.name, project=t.prompt_vars.get("PROJECT", t.name))
    )

    # canonical reference files (first candidate that exists wins)
    copied = []
    for canon, candidates in _REF_MAP.items():
        for cand in candidates:
            p = src / cand
            if p.exists():
                shutil.copy2(p, refs / canon)
                copied.append(canon)
                break

    # stage pages. Two source layouts are supported: a `stages/` subdir (the
    # canonical layout), or a FLAT handbook where `stage-*.md` sit directly in
    # `src/` (what handbook_generate_large emits). Either way they land in
    # references/stages/. The flat scan excludes the top-level reference files
    # (index/overview/register) since those are not stage-*.md.
    n_stages = 0
    stages_src = src / "stages"
    if stages_src.is_dir():
        stage_files = sorted(stages_src.glob("*.md"))
    else:
        stage_files = sorted(src.glob("stage-*.md"))
    for f in stage_files:
        shutil.copy2(f, refs / "stages" / f.name)
        n_stages += 1

    print(f"built {dest}  (refs: {', '.join(copied) or 'none'} | {n_stages} stage pages)")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=os.environ.get("EVAL_TARGET", "terminus2"),
                    choices=target_names(), help="target project (default: terminus2)")
    ap.add_argument("--src", type=Path, help="rendered handbook dir (default: the target's)")
    ap.add_argument("--dest", type=Path, help="output skill dir (default: the target's)")
    args = ap.parse_args()
    build(args.target, args.src, args.dest)


if __name__ == "__main__":
    main()
