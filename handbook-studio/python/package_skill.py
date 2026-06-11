#!/usr/bin/env python3
"""package_skill.py — write the handbook's reference files + SKILL.md into the repo.

Reuses the rendering helpers from handbook_as_helper/build_handbook_skill.py but
targets <repo>/.handbook/references/ (env-driven) instead of the fixed in-repo
location, and also drops a SKILL.md so other agents (and the in-app chat) can
consume the handbook as a navigation skill.

Env:
  HANDBOOK_PHASE3_ROOT — contains output/handbook_en.json (or handbook.json)
  HANDBOOK_OUT         — repo .handbook dir (references/ + SKILL.md go here)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_HANDBOOK = Path(
    os.environ.get("HANDBOOK_GENERATE_DIR", str(_HERE.parent.parent / "Harness_Handbook" / "handbook_generate"))
).parent  # Harness_Handbook/
sys.path.insert(0, str(_HANDBOOK / "handbook_as_helper"))

# Reuse the proven rendering helpers.
import build_handbook_skill as bhs  # noqa: E402


SKILL_TEMPLATE = """---
name: {slug}-handbook
description: >-
  Structural map of {name}, derived from its code. Use it when planning a change
  to find EVERY code site the change must touch — it maps the code stage-by-stage
  (with a per-function description) and lists every state variable together with
  all of its read/write locations.
---

# {name} Handbook — navigation guide

This is a structural map of {name}. Use it to locate where a requested change must
take effect — especially sites that are not adjacent to the obvious one.

## Reference files (in this folder)
- `references/overview.md`    — whole-system orientation. Read this first.
- `references/index.md`       — the map: every stage and every state register.
- `references/registers.md`   — for each state variable: every write and read site.
- `references/stages/<id>.md` — one stage's prose plus each function in it.

## How to use it
1. Read `references/overview.md` to understand the system.
2. Read `references/index.md` to find the stages, functions, and registers your change involves.
3. For every state variable involved, read `references/registers.md` and note EVERY read/write site.
4. Open the relevant `references/stages/<id>.md` for detail.
5. Verify each site against the real code before finalizing your plan.
"""


def main() -> int:
    phase3_root = Path(os.environ.get("HANDBOOK_PHASE3_ROOT", str(_HANDBOOK / "handbook_generate" / "phase3")))
    out_root = Path(os.environ.get("HANDBOOK_OUT", str(phase3_root.parent)))

    output_dir = phase3_root / "output"
    candidates = [output_dir / "handbook_en.json", output_dir / "handbook.json"]
    handbook_json = next((c for c in candidates if c.exists()), None)
    if handbook_json is None:
        print(f"[skill] no handbook json under {output_dir}", file=sys.stderr)
        return 2

    d = json.loads(handbook_json.read_text(encoding="utf-8"))
    refs = out_root / "references"
    if refs.exists():
        import shutil

        shutil.rmtree(refs)
    (refs / "stages").mkdir(parents=True)

    overview_md = (d.get("overview") or {}).get("content_md", "").strip()
    (refs / "overview.md").write_text(overview_md + "\n", encoding="utf-8")

    registers_md = d.get("registers_md") or ""
    (refs / "registers.md").write_text(registers_md.strip() + "\n", encoding="utf-8")

    stages = d.get("stages") or {}
    order = d.get("order") or list(stages.keys())
    stage_index = []
    for sid in order:
        s = stages.get(sid)
        if not s:
            continue
        (refs / "stages" / f"{sid}.md").write_text(bhs._render_stage(sid, s), encoding="utf-8")
        fn_names = [f.get("qualname", "?") for f in (s.get("functions") or [])]
        blurb = bhs._stage_blurb(s.get("logical_md", ""))
        stage_index.append((sid, s.get("title", sid), fn_names, blurb))

    name = os.environ.get("HANDBOOK_PROJECT_NAME", out_root.parent.name or "this repository")
    lines = [f"# {name} Handbook — Index\n",
             "Use this to decide which reference file(s) to open.\n",
             "## Stages (each detailed in `stages/<id>.md`)\n"]
    for sid, title, fns, blurb in stage_index:
        lines.append(f"- **{sid}** — {title}")
        if blurb:
            lines.append(f"    - {blurb}")
        if fns:
            lines.append(f"    - functions: {', '.join('`' + f + '`' for f in fns)}")
    lines.append("\n## State registers (each detailed in `registers.md`)\n")
    for reg_name, purpose in bhs._registers_index(registers_md):
        lines.append(f"- **`{reg_name}`** — {purpose}")
    (refs / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-") or "repo"
    (out_root / "SKILL.md").write_text(SKILL_TEMPLATE.format(slug=slug, name=name), encoding="utf-8")

    n_fn = sum(len(fns) for _, _, fns, _ in stage_index)
    print(f"[skill] wrote {refs} ({len(stage_index)} stages, {n_fn} functions) + SKILL.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
