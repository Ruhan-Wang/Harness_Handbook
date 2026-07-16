# -*- coding: utf-8 -*-
"""Read-only views over the skeleton: render order, chapter numbers, and the
short "brief" blocks fed into prompts. Pure functions on the skeleton dict — no
LLM, no IO.
"""
from __future__ import annotations


# ─── Ordering ────────────────────────────────────────────────────────────────


def stage_render_order(skeleton: dict) -> list[str]:
    """Order = (top-level stages by skeleton order) → side flows → crosscuts → subsys-*.

    Sub-stages of stage-4 are inlined after stage-4 itself, in the order they
    appear under stage-4.children.
    """
    out: list[str] = []
    seen: set[str] = set()

    stages_by_id = {s["id"]: s for s in skeleton["stages"]}

    def push(sid: str):
        if sid in seen or sid not in stages_by_id:
            return
        out.append(sid)
        seen.add(sid)
        for ch in stages_by_id[sid].get("children") or []:
            push(ch)

    for s in skeleton["stages"]:
        sid = s["id"]
        if sid.startswith("stage-") and not s.get("parent"):
            push(sid)

    for s in skeleton["stages"]:
        sid = s["id"]
        if sid.startswith("side-"):
            push(sid)

    for s in skeleton["stages"]:
        sid = s["id"]
        if sid.startswith("crosscut-") or sid.startswith("subsys-"):
            push(sid)

    return out


def stage_chapter_numbers(skeleton: dict) -> dict[str, str]:
    """Map each `stage-*` id to a gap-free, render-time chapter number derived
    from TREE POSITION — not from the numeric suffix baked into the id.

    Pass C can remove / merge / split stages, leaving holes in the raw ids
    (e.g. after `stage-4.5` is merged away you're left with `stage-4.4` then
    `stage-4.6`). Stage ids are stable opaque keys referenced across the
    mapping members, parent/children pointers, and the Pass B/D caches, so they
    are deliberately never renumbered. Instead the reader-facing chapter number
    is assigned here, at render time, by walking the same tree
    `stage_render_order` uses:

      - top-level `stage-*` nodes get `1`, `2`, … in skeleton-list order;
      - each node's children get `<parent>.1`, `<parent>.2`, … recursively.

    The result is always contiguous regardless of id gaps. Appendix-like nodes
    (`side-` / `crosscut-` / `subsys-`) are not sequential chapters, so they
    are left out of the map; callers fall back to the raw id for those.
    """
    stages_by_id = {s["id"]: s for s in skeleton["stages"]}
    numbers: dict[str, str] = {}

    def walk(sid: str, label: str) -> None:
        if sid not in stages_by_id:  # dangling child pointer — skip
            return
        numbers[sid] = label
        for i, ch in enumerate(stages_by_id[sid].get("children") or [], start=1):
            walk(ch, f"{label}.{i}")

    top = 0
    for s in skeleton["stages"]:
        sid = s["id"]
        if sid.startswith("stage-") and not s.get("parent"):
            top += 1
            walk(sid, str(top))
    return numbers


# ─── Brief blocks (for prompts / ground truth) ───────────────────────────────


def _stages_brief(skeleton: dict) -> str:
    chapters = stage_chapter_numbers(skeleton)
    lines = []
    for s in skeleton["stages"]:
        sid = s["id"]
        if sid.startswith("stage-") and not s.get("parent"):
            desc = (s.get("description") or "").replace("\n", " ")[:200]
            # Reader-facing chapter number is the gap-free positional one; the
            # stable `sid` is kept alongside as the internal anchor.
            ch = chapters.get(sid, sid)
            lines.append(f"- Stage {ch} ({sid}) · {s.get('title','')}: {desc}")
    return "\n".join(lines)


def _side_brief(skeleton: dict) -> str:
    lines = []
    for s in skeleton["stages"]:
        sid = s["id"]
        if sid.startswith(("side-", "crosscut-", "subsys-")):
            desc = (s.get("description") or s.get("role", "")).replace("\n", " ")[:160]
            lines.append(f"- {sid} · {s.get('title','')}: {desc}")
    return "\n".join(lines)


def _registers_brief(skeleton: dict) -> str:
    """All state_registers as a brief table for Tier 1 + register-appendix prompts."""
    lines = []
    for r in skeleton.get("state_registers") or []:
        rid = r.get("id", "")
        sem = (r.get("semantics") or "").replace("\n", " ")[:300]
        lines.append(f"- **{rid}**: {sem}")
    return "\n".join(lines) or "(no state registers)"


def _stage_registers_brief(skeleton: dict, stage_id: str) -> str:
    """Filter state_registers whose semantics mention this stage_id."""
    relevant = []
    for r in skeleton.get("state_registers") or []:
        sem = r.get("semantics") or ""
        if stage_id in sem:
            relevant.append(f"- **{r.get('id','')}**: {sem[:280]}")
    if not relevant:
        return "(本 stage 在 skeleton 中未被任何 register 显式提及。若代码里确有交互，请说出来；否则在「📊 状态流动」block 中标「无」。)"
    return "\n".join(relevant)


def _members_brief(stage_members: list) -> str:
    lines = []
    for m in stage_members[:30]:
        qn = m["qualname"]
        t = m.get("type", "?")
        lr = m.get("line_range")
        lines.append(f"- {qn} ({t}, lines {lr})")
    if len(stage_members) > 30:
        lines.append(f"- ... ({len(stage_members) - 30} more)")
    return "\n".join(lines) or "(no members)"
