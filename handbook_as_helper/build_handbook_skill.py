#!/usr/bin/env python3
"""build_handbook_skill.py — generate the handbook navigation Skill's reference files
from the generated handbook (handbook/phase3/output/handbook_en.json).

Output (under handbook_skill/references/):
  overview.md      <- overview.content_md (system orientation)
  index.md         <- TOC: every stage (id + title + function names) and every register
  registers.md     <- registers_md (state-variable read/write registry)
  stages/<id>.md   <- per stage: logical_md + each function's full NL translation

Deliberately OMITTED: stages[].findings / overview.findings / score — those are the
handbook's self-QA notes ("rewrite this block..."), not knowledge about terminus_2.

Re-run this whenever the handbook is regenerated. No NexAU needed; pure stdlib.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
HANDBOOK_JSON = Path(
    __import__("os").environ.get(
        "HANDBOOK_JSON",
        HERE.parent / "handbook" / "phase3" / "output" / "handbook_en.json",
    )
)
REFS = HERE / "handbook_skill" / "references"

# translation keys to skip (metadata / self-QA, not content)
_SKIP = {"schema_version", "type"}


def _md(value, depth: int = 0) -> str:
    """Render an arbitrary JSON value (str / list / dict) as readable markdown."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict):
                out.append(_md(item, depth))
            else:
                out.append(f"- {_md(item, depth)}")
        return "\n".join(out)
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            if k in _SKIP:
                continue
            rv = _md(v, depth + 1)
            if not rv:
                continue
            label = k.replace("_", " ")
            if "\n" in rv:
                out.append(f"**{label}:**\n{rv}")
            else:
                out.append(f"**{label}:** {rv}")
        return "\n\n".join(out)
    return str(value)


def _render_function(fn: dict) -> str:
    qual = fn.get("qualname", "?")
    tr = fn.get("translation", {})
    body = _md(tr) if isinstance(tr, dict) else _md(tr)
    return f"### `{qual}`\n\n{body}\n"


def _render_stage(sid: str, s: dict) -> str:
    title = s.get("title", sid)
    chapter = s.get("chapter", "")
    head = f"# {chapter + ' · ' if chapter else ''}{title}  ({sid})\n"
    parts = [head]
    lm = (s.get("logical_md") or "").strip()
    if lm:
        parts.append(lm)
    fns = s.get("functions") or []
    if fns:
        parts.append("\n---\n\n## Functions in this stage\n")
        for fn in fns:
            parts.append(_render_function(fn))
    return "\n".join(parts).strip() + "\n"


def _registers_index(registers_md: str) -> list[tuple[str, str]]:
    """Extract (reg-name, purpose) pairs from registers_md for the TOC."""
    out = []
    blocks = re.split(r"^###\s+", registers_md, flags=re.M)
    for b in blocks:
        first_line = b.split("\n", 1)[0]
        m_name = re.search(r"`([\w\-]+)`", first_line)  # name in backticks (after optional emoji)
        m_purpose = re.search(r"\*\*Purpose\*\*:\s*(.+)", b)
        if m_name and m_purpose:
            out.append((m_name.group(1), m_purpose.group(1).strip()))
    return out


def _stage_blurb(logical_md: str, max_chars: int = 900) -> str:
    """The stage's full Opening Explanation (what it does and why) — enough to ROUTE a
    query to this stage. Function-level detail stays in stages/<id>.md."""
    text = logical_md or ""
    # grab the Opening Explanation section: text until the next "#### ..." header
    m = re.search(r"Opening Explanation\s*\n+(.+?)(?:\n#{1,6}\s|\Z)", text, flags=re.S)
    body = m.group(1) if m else text
    para = re.sub(r"\s+", " ", body.strip())
    if len(para) > max_chars:
        cut = para[:max_chars]
        if ". " in cut:
            cut = cut.rsplit(". ", 1)[0] + "."
        para = cut
    return para


def main() -> None:
    if not HANDBOOK_JSON.exists():
        raise SystemExit(f"handbook json not found: {HANDBOOK_JSON} (set HANDBOOK_JSON)")
    d = json.loads(HANDBOOK_JSON.read_text(encoding="utf-8"))

    # fresh references/
    if REFS.exists():
        shutil.rmtree(REFS)
    (REFS / "stages").mkdir(parents=True)

    # 1) overview.md
    overview_md = (d.get("overview") or {}).get("content_md", "").strip()
    (REFS / "overview.md").write_text(overview_md + "\n", encoding="utf-8")

    # 2) registers.md
    registers_md = d.get("registers_md") or ""
    (REFS / "registers.md").write_text(registers_md.strip() + "\n", encoding="utf-8")

    # 3) per-stage files (in handbook order)
    stages = d.get("stages") or {}
    order = d.get("order") or list(stages.keys())
    stage_index = []
    for sid in order:
        s = stages.get(sid)
        if not s:
            continue
        (REFS / "stages" / f"{sid}.md").write_text(_render_stage(sid, s), encoding="utf-8")
        fn_names = [f.get("qualname", "?") for f in (s.get("functions") or [])]
        blurb = _stage_blurb(s.get("logical_md", ""))
        stage_index.append((sid, s.get("title", sid), fn_names, blurb))

    # 4) index.md (TOC)
    lines = ["# Terminus-2 Handbook — Index\n",
             "Use this to decide which reference file(s) to open.\n",
             "## Stages (each detailed in `stages/<id>.md`)\n"]
    for sid, title, fns, blurb in stage_index:
        lines.append(f"- **{sid}** — {title}")
        if blurb:
            lines.append(f"    - {blurb}")
        if fns:
            lines.append(f"    - functions: {', '.join('`'+f+'`' for f in fns)}")
    lines.append("\n## State registers (each detailed in `registers.md`)\n")
    for name, purpose in _registers_index(registers_md):
        lines.append(f"- **`{name}`** — {purpose}")
    (REFS / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # summary
    n_fn = sum(len(fns) for _, _, fns, _ in stage_index)
    n_reg = len(_registers_index(registers_md))
    total = sum(p.stat().st_size for p in REFS.rglob("*.md"))
    print(f"wrote {REFS}")
    print(f"  overview.md, index.md, registers.md, {len(stage_index)} stage files")
    print(f"  stages={len(stage_index)}  functions={n_fn}  registers={n_reg}  total={total//1024} KB")


if __name__ == "__main__":
    main()
