# -*- coding: utf-8 -*-
"""Render one translation unit into a markdown <details> block.

Rendering follows option C (function aggregation + inline call notes):
  - single  → standard 7-section details
  - multi_region → father details with each region as ### sub-section,
    callouts inline at the region where helper is called.
"""
from __future__ import annotations

import re
from typing import Sequence

from extract_source import Snippet
from translate_member import TranslationUnit


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _slug(qualname: str) -> str:
    """Make a stable anchor id from a qualname."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", qualname).strip("_").lower()
    return f"fn-{s}"


def _line_range_str(lr) -> str:
    if isinstance(lr, (list, tuple)) and len(lr) == 2:
        return f"{lr[0]}-{lr[1]}"
    return str(lr)


def _bullet_list(items: Sequence[str], indent: str = "") -> str:
    return "\n".join(f"{indent}- {it}" for it in items)


def _stringify_relation_item(item) -> str:
    """Accept either a bare string or a dict like {qualname/expr, note}."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        head = (
            item.get("qualname")
            or item.get("name")
            or item.get("expr")
            or item.get("path")
            or item.get("source")
            or ""
        )
        note = item.get("note") or item.get("description") or ""
        if head and note:
            return f"`{head}` — {note}"
        return head or note or str(item)
    return str(item)


_REG_ACTION_ICON_ZH = {
    "write": "✏️ 写",
    "read": "👁 读",
    "clear": "🧹 清",
    "reset": "♻️ 重置",
}

_REG_ACTION_ICON_EN = {
    "write": "✏️ writes",
    "read": "👁 reads",
    "clear": "🧹 clears",
    "reset": "♻️ resets",
}

_SECTION_LABELS = {
    "zh": {
        "stage_context": "stage 上下文",
        "synopsis": "这段代码在干什么",
        "interface": "接口 · 参数 / IO",
        "execution_flow": "执行流",
        "source": "源码",
        "design_decisions": "Non-obvious 设计决策",
        "design_decisions_multi": "Non-obvious 设计决策（跨 region 聚合）",
        "relations": "关联网络",
        "overall_structure": "总体结构",
        "callout_template": "⤵ 此 region 调用 [`{qn}`](#{anchor}) — {note}",
        "code_missing": "_(代码片段未提供)_",
        "region_word": "Region",
        "table_header": "| Region | 行号 | 角色 | 终态 |",
        "rel_callers": "**调用方**",
        "rel_callees": "**核心被调用**",
        "rel_config": "**配置/状态来源**",
        "rel_results": "**结果去向**",
        "rel_siblings": "**同类 sibling**",
        "rel_register": "**📊 寄存器交互**",
        "action_icons": _REG_ACTION_ICON_ZH,
    },
    "en": {
        "stage_context": "Stage context",
        "synopsis": "What this code does",
        "interface": "Interface · params / IO",
        "execution_flow": "Execution flow",
        "source": "Source",
        "design_decisions": "Non-obvious design decisions",
        "design_decisions_multi": "Non-obvious design decisions (cross-region)",
        "relations": "Relations",
        "overall_structure": "Overall structure",
        "callout_template": "⤵ This region calls [`{qn}`](#{anchor}) — {note}",
        "code_missing": "_(source snippet unavailable)_",
        "region_word": "Region",
        "table_header": "| Region | Lines | Role | Terminal state |",
        "rel_callers": "**Callers**",
        "rel_callees": "**Core callees**",
        "rel_config": "**Config / state sources**",
        "rel_results": "**Results to**",
        "rel_siblings": "**Related siblings**",
        "rel_register": "**📊 Register interactions**",
        "action_icons": _REG_ACTION_ICON_EN,
    },
}


def _labels(lang: str) -> dict:
    return _SECTION_LABELS.get(lang, _SECTION_LABELS["zh"])


def _format_register_interactions(items, action_icons: dict) -> str:
    """Render register_interactions as `[write] reg-X — note; [read] reg-Y — note`."""
    if not isinstance(items, list) or not items:
        return ""
    parts = []
    for it in items:
        if not isinstance(it, dict):
            continue
        action = it.get("action") or ""
        reg = it.get("register") or ""
        note = it.get("note") or ""
        icon = action_icons.get(action, action or "?")
        seg = f"{icon} `{reg}`"
        if note:
            seg += f" — {note}"
        parts.append(seg)
    return "; ".join(parts)


def _relations_block(relations: dict, lang: str = "zh") -> str:
    out = []
    L = _labels(lang)
    label_map = [
        ("callers", L["rel_callers"]),
        ("core_callees", L["rel_callees"]),
        ("config_state_sources", L["rel_config"]),
        ("results_to", L["rel_results"]),
        ("siblings", L["rel_siblings"]),
    ]
    for key, label in label_map:
        items = relations.get(key) or []
        if not items:
            continue
        rendered = [_stringify_relation_item(it) for it in items]
        out.append(f"- {label}: " + "; ".join(rendered))

    reg_inter_md = _format_register_interactions(
        relations.get("register_interactions"), L["action_icons"]
    )
    if reg_inter_md:
        out.append(f"- {L['rel_register']}: {reg_inter_md}")

    return "\n".join(out)


def _design_decisions_block(decisions: Sequence[str]) -> str:
    return _bullet_list(decisions)


def _interface_block(iface: dict, lang: str = "zh") -> str:
    """Render the I/O contract: signature line + params / reads / returns / effects."""
    if not isinstance(iface, dict):
        return ""
    zh = lang != "en"
    out: list[str] = []
    sig = (iface.get("signature") or "").strip()
    if sig:
        out += [f"`{sig}`", ""]
    params = iface.get("params") or []
    if params:
        segs = []
        for p in params:
            if isinstance(p, dict):
                nm, ty, ro = p.get("name", ""), p.get("type", ""), p.get("role", "")
                s = f"`{nm}`" + (f": `{ty}`" if ty else "")
                if ro:
                    s += f" — {ro}"
                segs.append(s)
            else:
                segs.append(str(p))
        out.append(("- 参数: " if zh else "- params: ") + "; ".join(segs))
    reads = iface.get("reads_state") or []
    if reads:
        out.append(("- 读状态: " if zh else "- reads: ")
                   + ", ".join(f"`{r}`" for r in reads))
    ret = iface.get("returns")
    if ret:
        out.append(("- 返回: " if zh else "- returns: ") + str(ret))
    eff = iface.get("side_effects") or []
    if eff:
        out.append(("- 副作用: " if zh else "- effects: ")
                   + "; ".join(str(e) for e in eff))
    return "\n".join(out)


# ─── Single function rendering ────────────────────────────────────────────────


def _render_single(unit: TranslationUnit, t: dict, lang: str = "zh") -> str:
    L = _labels(lang)
    snip = unit.snippets[0]
    role = t.get("locator_role", "").strip()
    summary = (
        f"<b>{unit.qualname}</b> — {snip.file}:{_line_range_str(snip.line_range)}"
        f" · {role}"
    )

    flow_items = t.get("execution_flow") or []
    flow_md = (
        "\n".join(f"{i}. {step}" for i, step in enumerate(flow_items, 1))
        if flow_items else ""
    )

    code_block = f"```python\n{snip.text}\n```"

    parts = [
        f'<details id="{_slug(unit.qualname)}">',
        f"<summary>{summary}</summary>",
        "",
        f"> **{L['stage_context']}**: {t.get('stage_context', '').strip()}",
        "",
        f"**{L['synopsis']}**",
        "",
        t.get("synopsis", "").strip(),
        "",
    ]
    iface_md = _interface_block(t.get("interface") or {}, lang)
    if iface_md:
        parts.extend([f"**{L['interface']}**", "", iface_md, ""])
    if flow_md:
        parts.extend([f"**{L['execution_flow']}**", "", flow_md, ""])
    parts.extend([
        f"**{L['source']}**",
        "",
        code_block,
        "",
        f"**{L['design_decisions']}**",
        "",
        _design_decisions_block(t.get("design_decisions") or []),
        "",
        f"**{L['relations']}**",
        "",
        _relations_block(t.get("relations") or {}, lang),
        "",
        "</details>",
        "",
    ])
    return "\n".join(parts)


# ─── Multi-region rendering ───────────────────────────────────────────────────


def _render_multi_region(unit: TranslationUnit, t: dict, lang: str = "zh") -> str:
    L = _labels(lang)
    first = unit.snippets[0]
    last = unit.snippets[-1]
    file = first.file
    overall_lr = f"{first.line_range[0]}-{last.line_range[1]}"
    role = t.get("locator_role", "").strip()
    summary = (
        f"<b>{unit.qualname}</b> — {file}:{overall_lr} "
        f"({len(unit.entries)} regions) · {role}"
    )

    rows = [L["table_header"], "|---|---|---|---|"]
    for s in t.get("overall_structure") or []:
        lr = _line_range_str(s.get("line_range", ""))
        rows.append(
            f"| {s.get('region_idx', '?')} | {lr} | "
            f"{s.get('role', '')} | {s.get('terminal_state', '')} |"
        )
    overall_table = "\n".join(rows)

    region_blocks: list[str] = []
    snip_by_lr = {tuple(s.line_range): s for s in unit.snippets}

    for r in t.get("regions") or []:
        idx = r.get("region_idx", "?")
        title = r.get("title", "")
        lr_pair = r.get("line_range") or []
        if isinstance(lr_pair, list) and len(lr_pair) == 2:
            lr_key = (int(lr_pair[0]), int(lr_pair[1]))
        else:
            lr_key = None
        snip = snip_by_lr.get(lr_key)
        gloss = (r.get("gloss") or "").strip()
        callouts = r.get("callouts") or []

        callout_lines = []
        for c in callouts:
            qn = c.get("to_qualname", "")
            note = c.get("note", "")
            anchor = _slug(qn)
            callout_lines.append(
                "> " + L["callout_template"].format(qn=qn, anchor=anchor, note=note)
            )
        callout_md = "\n".join(callout_lines)

        code_md = f"```python\n{snip.text}\n```" if snip else L["code_missing"]

        chunk = [
            f"#### {L['region_word']} {idx} · {title} ({file}:{_line_range_str(lr_pair)})",
            "",
            gloss,
            "",
        ]
        if callout_md:
            chunk.extend([callout_md, ""])
        chunk.extend([code_md, ""])
        region_blocks.append("\n".join(chunk))

    parts = [
        f'<details id="{_slug(unit.qualname)}">',
        f"<summary>{summary}</summary>",
        "",
        f"> **{L['stage_context']}**: {t.get('stage_context', '').strip()}",
        "",
        f"### {L['synopsis']}",
        "",
        t.get("synopsis", "").strip(),
        "",
    ]
    iface_md = _interface_block(t.get("interface") or {}, lang)
    if iface_md:
        parts.extend([f"### {L['interface']}", "", iface_md, ""])
    parts.extend([
        f"### {L['overall_structure']}",
        "",
        overall_table,
        "",
        "---",
        "",
    ])
    parts.append("\n---\n\n".join(region_blocks))
    parts.extend([
        "",
        "---",
        "",
        f"### {L['design_decisions_multi']}",
        "",
        _design_decisions_block(t.get("design_decisions") or []),
        "",
        f"### {L['relations']}",
        "",
        _relations_block(t.get("relations") or {}, lang),
        "",
        "</details>",
        "",
    ])
    return "\n".join(parts)


# ─── Top-level entry ──────────────────────────────────────────────────────────


def render_unit(unit: TranslationUnit, translation: dict, lang: str = "zh") -> str:
    if unit.type_kind == "multi_region":
        return _render_multi_region(unit, translation, lang)
    return _render_single(unit, translation, lang)
