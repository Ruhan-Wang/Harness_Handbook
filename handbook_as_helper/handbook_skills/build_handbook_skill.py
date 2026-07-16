#!/usr/bin/env python3
"""build_handbook_skill.py — generate the handbook navigation Skill's reference files
by CARVING the fully-rendered handbook markdown (handbook/phase3/output/handbook_en.md).

NOTE: This is the TERMINUS-2-specific skill builder (function-card `<details>` format +
register enrichment that greps `self._attr` sites out of the Python source). For other
targets (e.g. Codex/Rust, whose handbook is plain rendered markdown) use the generic
`build_skill_from_handbook.py`, which assembles `handbook_skill_<target>/` from any
rendered handbook directory.

The markdown is the complete rendering (same source as the HTML viewer): every function
card carries its `file.py:start-end` line anchor, full parameter list WITH types, the
formatted signature, and a `**Source**` block with the function's real code. The old
path here rendered references from handbook_en.json through a generic JSON→md walk,
which silently lost all of that (worst: a recursive key-skip set meant every `type`
field — i.e. every parameter type — was dropped). The JSON is no longer used.

Output (under handbook_skill/references/):
  overview.md      <- the "🗺️ System Overview" chapter
  index.md         <- TOC built from the carved chapters: every stage (id + title + blurb +
                      function cards with their file:line anchors) and every register
  registers.md     <- the "🔄 State Flow Reference" chapter, ENRICHED with the exact code
                      sites of each register (init / reset / write / read) grepped from the
                      pristine terminus_2 source
  stages/<id>.md   <- one chapter per file, verbatim (stage-N, stage-N.M, side-*, crosscut-*,
                      subsys-*) — function cards, source blocks and all

Chapter → file mapping comes from the md's own headings (`## 1 · …`, `### 4.3 · …`,
`## side-S1 · …`), so the carve always matches the rendered handbook's structure even
when it differs from the JSON's stage decomposition.

The register enrichment fills in the lifecycle the upstream text leaves as "input does
not specify": each register maps to a `self._<attr>` (or `chat._messages`), so we grep
the source for it and inject an authoritative, line-numbered site list. It lists every
site of every register — nothing golden-specific. The same routine, pointed at an EDITED
source tree (root=...), is the post-change resync primitive used by handbook_code_edit.

Re-run this whenever the handbook is regenerated. Pure stdlib; needs the pristine source
at harbor/src/harbor/agents/terminus_2 for the register enrichment (skipped if a source
file is absent — those registers just keep "(none)" site lists).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER_ROOT = HERE.parent
REPO_ROOT = HELPER_ROOT.parent
# The rendered handbook and the pristine terminus_2 source both live in the sibling
# Harness_Translation repo — phase-3 writes there (see handbook_generate/phase3/config.py),
# NOT under Harness_Handbook. Resolve to the first path that exists so this keeps working
# when handbook_as_helper is moved around; env vars override.
_REPO = REPO_ROOT.parent / "Harness_Translation"


def _first_existing(*candidates: Path) -> Path:
    """First candidate that exists, else the first (for a clear not-found error message)."""
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


HANDBOOK_MD = (
    Path(os.environ["HANDBOOK_MD"]) if os.environ.get("HANDBOOK_MD")
    else _first_existing(
        _REPO / "handbook/phase3/output/handbook_en.md",
        REPO_ROOT / "handbook_generate/phase3/output/handbook_en.md",
        REPO_ROOT / "handbook/phase3/output/handbook_en.md",
    )
)
REFS = HERE / "handbook_skill_terminus" / "references"

# --- register enrichment ------------------------------------------------------
# Each register maps to a `self._<attr>` (or `chat._messages`) in the real code; we grep the
# source for it and inject the exact init/reset/write/read sites into registers.md.
PRISTINE = (
    Path(os.environ["PRISTINE_ROOT"]) if os.environ.get("PRISTINE_ROOT")
    else _first_existing(
        _REPO / "harbor/src/harbor/agents/terminus_2",
        REPO_ROOT / "harbor/src/harbor/agents/terminus_2",
    )
)
SOURCES = ["terminus_2.py", "terminus_json_plain_parser.py",
           "terminus_xml_plain_parser.py", "tmux_session.py"]

# register id -> regex matching its attribute access in the code
REG = {
    "reg-pending-completion":     r"self\._pending_completion\b",
    "reg-pending-handoff-prompt": r"self\._pending_handoff_prompt\b",
    "reg-pending-subagent-refs":  r"self\._pending_subagent_refs\b",
    "reg-n-episodes":             r"self\._n_episodes\b",
    "reg-summarization-count":    r"self\._summarization_count\b",
    "reg-trajectory-steps":       r"self\._trajectory_steps\b",
    "reg-chat-messages":          r"chat\.(?:_messages|messages)\b",
    "reg-asciinema-markers":      r"self\._timestamped_markers\b",
    "reg-subagent-metrics":       r"self\._subagent_metrics\b",
    "reg-api-request-times":      r"self\._api_request_times\b",
}
_MUT = re.compile(
    r"\.(?:append|pop|clear|extend|insert|remove|sort|update|add|setdefault|discard)\s*\(")
# write = optional subscript(s) (`[k]`, `[k][j]`) then any (augmented) assignment.
# `(?!=)` keeps comparisons (`==`) out; `<=` / `>=` / `!=` never reach the `=` branch.
_ASSIGN = re.compile(
    r"\s*(?:\[[^][]*\])*\s*(?:[+\-*/%&|^@]|//|\*\*|>>|<<)?=(?!=)")


def _methods(lines: list[str]) -> list[str | None]:
    """Map each line index to the enclosing function: class methods (4-space `def`) AND
    module-level `def`s; a col-0 `class` line resets (so code between classes is never
    attributed to the previous class's last method). Best effort."""
    cur: str | None = None
    out: list[str | None] = []
    for ln in lines:
        m = re.match(r"^    (?:async\s+)?def\s+(\w+)", ln)  # class-method indent
        if m:
            cur = m.group(1)
        elif re.match(r"^(?:async\s+)?def\s+(\w+)", ln):    # module-level function
            cur = re.match(r"^(?:async\s+)?def\s+(\w+)", ln).group(1)
        elif re.match(r"^class\s+\w+", ln):                 # new class: no method yet
            cur = None
        out.append(cur)
    return out


def _sites(pattern: str, root: Path | None = None):
    """Grep the sources under `root` (default: the pristine checkout) for `pattern`,
    classifying each hit as init / reset / other write / read by the enclosing method and
    whether it is an assignment or mutation."""
    rx = re.compile(pattern)
    init, reset, writes, reads = [], [], [], []
    for fname in SOURCES:
        p = (root or PRISTINE) / fname
        if not p.exists():
            continue
        lines = p.read_text().splitlines()
        meth = _methods(lines)
        for i, ln in enumerate(lines):
            m = rx.search(ln)
            if not m:
                continue
            after = ln[m.end():]
            is_write = bool(_ASSIGN.match(after)) or bool(_MUT.match(after))
            fn = meth[i] or "?"
            entry = f"{fname}:{i + 1}  (`{fn}`)"
            if is_write and fn == "__init__":
                init.append(entry)
            elif is_write and fn == "_reset_per_run_state":
                reset.append(entry)
            elif is_write:
                writes.append(entry)
            else:
                reads.append(entry)
    return init, reset, writes, reads


def _register_block(reg_id: str, root: Path | None = None,
                    reg_map: dict[str, str] | None = None) -> str | None:
    """The authoritative code-site block for one register id, or None if unknown."""
    pat = (reg_map or REG).get(reg_id)
    if not pat:
        return None
    init, reset, writes, reads = _sites(pat, root)

    def fmt(label, items):
        if not items:
            return f"- {label}: (none)"
        return f"- {label}:\n" + "\n".join(f"  - `{x}`" for x in items)

    return (
        "**Code sites (authoritative — exact lines grepped from the source):**\n"
        f"{fmt('Init (in __init__)', init)}\n"
        f"{fmt('Reset (in _reset_per_run_state)', reset)}\n"
        f"{fmt('Other writes', writes)}\n"
        f"{fmt('Reads', reads)}"
    )


def _enrich_registers(text: str, root: Path | None = None,
                      reg_map: dict[str, str] | None = None) -> tuple[str, int]:
    """Inject an authoritative, line-numbered code-site list under each register's `### `
    header, grepped from the sources under `root` (default: pristine) using `reg_map`
    (default: REG). Idempotent: any prior injection is stripped first — which also makes
    this the post-edit RESYNC primitive (handbook_code_edit phase ④): pointed at an EDITED
    source tree, it refreshes every block to the post-change line numbers.
    Returns (text, n_enriched)."""
    text = re.sub(r"\n*<!-- code-sites:start -->.*?<!-- code-sites:end -->\n*",
                  "\n\n", text, flags=re.S)
    out: list[str] = []
    n = 0
    for line in text.splitlines():
        out.append(line)
        m = re.search(r"`(reg-[\w-]+)`", line)
        if line.startswith("### ") and m:
            blk = _register_block(m.group(1), root, reg_map)
            if blk:
                out += ["", "<!-- code-sites:start -->", blk, "<!-- code-sites:end -->"]
                n += 1
    return "\n".join(out).rstrip(), n


# --- carving the rendered markdown into reference files ------------------------

# A chapter starts at any h2, or at an h3 that is itself a numbered sub-stage
# ("### 4.3 · LLM Query"). Other h3s (function-card sections like "### Relations")
# stay inside their chapter.
_SUBSTAGE = re.compile(r"^### (\d+\.\d+) · (.+?)\s*$")
_H2 = re.compile(r"^## (.+?)\s*$")
# one function card: <summary><b>Qualname</b> — file.py:144-331 · role</summary>
_CARD = re.compile(r"<summary><b>([^<]+)</b>\s*—\s*([^·<]+?)\s*·\s*([^<]*)</summary>")


def _chapter_id(heading: str) -> tuple[str, str] | None:
    """(file id, display title) for a chapter heading's text, or None to skip."""
    t = heading.strip()
    if "System Overview" in t:
        return "overview", "System Overview"
    if "State Flow Reference" in t:
        return "registers", "State Flow Reference"
    m = re.match(r"(\d+(?:\.\d+)?) · (.+)", t)
    if m:
        return f"stage-{m.group(1)}", m.group(2)
    m = re.match(r"((?:side|crosscut|subsys)-[\w-]+) · (.+)", t)
    if m:
        return m.group(1), m.group(2)
    return None


def _split_chapters(text: str) -> list[tuple[str, str, str]]:
    """Carve the handbook md into [(id, title, content)] in document order. Content keeps
    the chapter's own heading line and everything (cards, Source blocks) verbatim."""
    lines = text.splitlines(keepends=True)
    starts: list[tuple[int, str]] = []          # (line idx, heading text)
    in_fence = False                            # inside ``` … ``` (e.g. a card's Source)
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue        # a col-0 `## comment` INSIDE source code is not a chapter
        m = _H2.match(ln)
        if m:
            starts.append((i, m.group(1)))
            continue
        m = _SUBSTAGE.match(ln)
        if m:
            starts.append((i, f"{m.group(1)} · {m.group(2)}"))
    starts.append((len(lines), ""))

    out: list[tuple[str, str, str]] = []
    for (i, head), (j, _) in zip(starts, starts[1:]):
        ident = _chapter_id(head)
        if not ident:
            continue
        cid, title = ident
        out.append((cid, title, "".join(lines[i:j]).rstrip() + "\n"))
    return out


def _stage_blurb(content: str, max_chars: int = 900) -> str:
    """The stage's Opening Explanation (what it does and why) — enough to ROUTE a query
    to this stage. Function-level detail stays in stages/<id>.md."""
    m = re.search(r"Opening Explanation\s*\n+(.+?)(?:\n#{1,6}\s|\Z)", content, flags=re.S)
    body = m.group(1) if m else ""
    para = re.sub(r"\s+", " ", body.strip())
    if len(para) > max_chars:
        cut = para[:max_chars]
        if ". " in cut:
            cut = cut.rsplit(". ", 1)[0] + "."
        para = cut
    return para


def _registers_index(registers_md: str) -> list[tuple[str, str]]:
    """Extract (reg-name, purpose) pairs from the registers chapter for the TOC."""
    out = []
    blocks = re.split(r"^###\s+", registers_md, flags=re.M)
    for b in blocks:
        first_line = b.split("\n", 1)[0]
        m_name = re.search(r"`([\w\-]+)`", first_line)  # name in backticks (after optional emoji)
        m_purpose = re.search(r"\*\*Purpose\*\*:\s*(.+)", b)
        if m_name and m_purpose:
            out.append((m_name.group(1), m_purpose.group(1).strip()))
    return out


def _render_index(stage_entries: list[tuple[str, str, list[tuple[str, str]], str]],
                  registers_md: str) -> str:
    """Render index.md from per-stage entries (id, title, [(qualname, anchor)], blurb)."""
    lines = ["# Terminus-2 Handbook — Index\n",
             "Use this to decide which reference file(s) to open.\n",
             "## Stages (each detailed in `stages/<id>.md`)\n"]
    for cid, title, cards, blurb in stage_entries:
        lines.append(f"- **{cid}** — {title}")
        if blurb:
            lines.append(f"    - {blurb}")
        if cards:
            lines.append("    - functions: "
                         + ", ".join(f"`{q}` ({a})" for q, a in cards))
    lines.append("\n## State registers (each detailed in `registers.md`)\n")
    for name, purpose in _registers_index(registers_md):
        lines.append(f"- **`{name}`** — {purpose}")
    return "\n".join(lines) + "\n"


def rebuild_index(refs_root: Path, insert_after: dict[str, str | None] | None = None) -> bool:
    """Regenerate an existing references dir's index.md from its CURRENT files — stage
    order/titles are taken from the old index, everything else (blurbs, function lists
    with line anchors, register list) is re-extracted. Stage files NOT in the old index
    (e.g. a stage newly declared by a handbook edit) are slotted in after the id named in
    `insert_after[new_id]` (appended at the end when absent/None). Used by
    handbook_code_edit's phase ⑤ after cards/registers were edited, refreshed or added."""
    idx = refs_root / "index.md"
    reg_p = refs_root / "registers.md"
    if not (idx.exists() and reg_p.exists()):
        return False
    # stage entries look like "- **stage-4.3** — LLM Query"; register entries have the
    # name in backticks ("- **`reg-…`** — …") so this pattern skips them.
    order = re.findall(r"^- \*\*([A-Za-z0-9.\-]+)\*\* — (.+)$", idx.read_text(), flags=re.M)
    known = {cid for cid, _t in order}

    # stage files on disk that the old index does not know yet → title from the file's
    # own chapter heading, position from insert_after
    for f in sorted((refs_root / "stages").glob("*.md")):
        cid = f.stem
        if cid in known:
            continue
        head = next((ln for ln in f.read_text().splitlines() if ln.startswith("#")), "")
        m = re.match(r"#{2,3}\s+(?:[\w.\-]+\s+·\s+)?(.+)", head)
        title = m.group(1).strip() if m else cid
        after = (insert_after or {}).get(cid)
        pos = next((i + 1 for i, (oid, _t) in enumerate(order) if oid == after),
                   len(order))
        order.insert(pos, (cid, title))
        known.add(cid)

    entries = []
    for cid, title in order:
        f = refs_root / "stages" / f"{cid}.md"
        if not f.exists():
            continue
        content = f.read_text()
        cards = [(q, a.strip()) for q, a, _r in _CARD.findall(content)]
        entries.append((cid, title, cards, _stage_blurb(content)))
    if not entries:
        return False
    idx.write_text(_render_index(entries, reg_p.read_text()), encoding="utf-8")
    return True


def main() -> None:
    if not HANDBOOK_MD.exists():
        raise SystemExit(f"handbook md not found: {HANDBOOK_MD} (set HANDBOOK_MD)")
    chapters = _split_chapters(HANDBOOK_MD.read_text(encoding="utf-8"))
    if not chapters:
        raise SystemExit(f"no chapters recognized in {HANDBOOK_MD}")

    # fresh references/
    if REFS.exists():
        shutil.rmtree(REFS)
    (REFS / "stages").mkdir(parents=True)

    stage_index: list[tuple[str, str, list[tuple[str, str]], str]] = []
    registers_md = ""
    for cid, title, content in chapters:
        if cid == "overview":
            (REFS / "overview.md").write_text(content, encoding="utf-8")
        elif cid == "registers":
            registers_md, n_enriched = _enrich_registers(content)
            (REFS / "registers.md").write_text(registers_md + "\n", encoding="utf-8")
        else:
            (REFS / "stages" / f"{cid}.md").write_text(content, encoding="utf-8")
            cards = [(q, anchor.strip()) for q, anchor, _role in _CARD.findall(content)]
            stage_index.append((cid, title, cards, _stage_blurb(content)))

    # index.md (TOC)
    (REFS / "index.md").write_text(_render_index(stage_index, registers_md), encoding="utf-8")

    # summary
    n_fn = sum(len(cards) for _, _, cards, _ in stage_index)
    n_reg = len(_registers_index(registers_md))
    n_enriched = registers_md.count("<!-- code-sites:start -->")
    total = sum(p.stat().st_size for p in REFS.rglob("*.md"))
    print(f"carved {HANDBOOK_MD.name} -> {REFS}")
    print(f"  overview.md, index.md, registers.md, {len(stage_index)} stage files")
    print(f"  stages={len(stage_index)}  function cards={n_fn}  registers={n_reg}  "
          f"(code-sites enriched: {n_enriched})  total={total//1024} KB")


if __name__ == "__main__":
    main()
