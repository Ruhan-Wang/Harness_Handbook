# -*- coding: utf-8 -*-
"""render_file.py — render a deep file card to markdown (NO LLM).

The file is the handbook's leaf node, and Phase 2a (deep mode) already wrote its
content: a detailed `description` plus a per-function inventory where each
function carries `purpose` / `data_flow` / `relations` (LLM prose) merged onto
the graph-derived facts (qualname, line range, signature, calls/called_by/
ext_calls). Phase 3's leaf layer is therefore pure RENDERING — faithfully laying
that card out as markdown, no model call.

`render_file_md(rel, card)` returns the markdown block for one file;
`file_one_liner(rel, card)` returns the short purpose line the rollup feeds the
LLM as evidence about what a leaf file does.
"""
from __future__ import annotations

from typing import Any

# Cap on how many call-relation neighbour names to list inline (the full count
# is always shown via n_*). Keeps a hot function's relation line readable.
_REL_NAMES_CAP = 10


def file_one_liner(rel: str, card: dict) -> str:
    """A single `- path — purpose [role]` line for rollup evidence / indexes."""
    purpose = (card.get("purpose") or "").strip()
    role = card.get("role") or "?"
    tail = f"  — {purpose}" if purpose else ""
    return f"- `{rel}`{tail}  [{role}]"


def _short_names(ids: list[str], cap: int = _REL_NAMES_CAP) -> str:
    """Render a list of qualname-ish ids as their leaf names, capped."""
    names = [str(i).split("::")[-1].split(".")[-1] for i in (ids or [])[:cap]]
    extra = len(ids) - cap if ids and len(ids) > cap else 0
    return ", ".join(names) + (f" (+{extra} more)" if extra > 0 else "")


def _render_function(fn: dict, lang: str = "en") -> list[str]:
    """Markdown lines for ONE function: signature + purpose/data_flow/relations
    + the graph call facts. Every field present in the deep card is shown."""
    zh = lang == "zh"
    L = {
        "purpose": "作用" if zh else "Purpose",
        "data_flow": "数据流" if zh else "Data flow",
        "relations": "调用关系" if zh else "Call relations",
        "callgraph": "调用图" if zh else "Call graph",
    }
    qual = fn.get("qualname") or fn.get("name") or "(anonymous)"
    lr = fn.get("line_range") or [None, None]
    line_label = "行" if zh else "lines"
    line_tag = f"  ({line_label} {lr[0]}–{lr[1]})" if lr and lr[0] else ""
    sig = (fn.get("signature") or "").strip()

    out: list[str] = [f"##### `{qual}`{line_tag}", ""]
    if sig:
        out += ["```", sig, "```", ""]

    # Each field is its OWN markdown paragraph — a blank line after every one,
    # otherwise consecutive non-blank lines collapse into a single run-on
    # paragraph (markdown soft-wrap) and the fields are unreadable.
    purpose = (fn.get("purpose") or "").strip()
    data_flow = (fn.get("data_flow") or "").strip()
    relations = (fn.get("relations") or "").strip()
    sep = "：" if zh else ": "
    if purpose:
        out += [f"**{L['purpose']}**{sep}{purpose}", ""]
    if data_flow:
        out += [f"**{L['data_flow']}**{sep}{data_flow}", ""]
    if relations:
        out += [f"**{L['relations']}**{sep}{relations}", ""]

    # Graph-derived call facts (always accurate; complement the prose).
    fact_bits: list[str] = []
    if fn.get("n_calls"):
        fact_bits.append(
            (f"调用 {fn['n_calls']} 个内部函数（{_short_names(fn.get('calls'))}）" if zh
             else f"calls {fn['n_calls']} internal fn ({_short_names(fn.get('calls'))})"))
    if fn.get("n_called_by"):
        fact_bits.append(
            (f"被 {fn['n_called_by']} 处调用（{_short_names(fn.get('called_by'))}）" if zh
             else f"called by {fn['n_called_by']} ({_short_names(fn.get('called_by'))})"))
    if fn.get("n_ext_calls"):
        fact_bits.append(
            (f"外部调用 {fn['n_ext_calls']} 个（{_short_names(fn.get('ext_calls'))}）" if zh
             else f"{fn['n_ext_calls']} external calls ({_short_names(fn.get('ext_calls'))})"))
    if fact_bits:
        joiner = "；" if zh else "; "
        end = "。" if zh else "."
        out += [f"*{L['callgraph']}*{sep}" + joiner.join(fact_bits) + end, ""]
    return out


def render_file_md(rel: str, card: dict | None, lang: str = "en") -> str:
    """Render one file's deep card to a markdown section.

    Heading level starts at H3 (`### file`) so a stage page can nest it under
    its own H1/H2. Falls back gracefully for a missing/empty card (e.g. a file
    the 2a reader dropped) so the file still appears in the handbook."""
    zh = lang == "zh"
    card = card or {}
    role = card.get("role") or "?"
    lifecycle = card.get("lifecycle") or ""
    badge = f"`{role}`" + (f" · `{lifecycle}`" if lifecycle and lifecycle != "none" else "")

    lines: list[str] = [f"### `{rel}`", "", badge]

    description = (card.get("description") or "").strip()
    purpose = (card.get("purpose") or "").strip()
    if description:
        lines += ["", description]
    elif purpose:                          # brief-only card (no deep description)
        lines += ["", purpose]
    else:
        lines += ["", "_(该文件暂无描述。)_" if zh else "_(This file has no description yet.)_"]

    funcs = card.get("functions") or []
    # Only functions that carry some prose are worth a detail block; pure
    # graph entries with no annotation still get listed (name + line range).
    if funcs:
        lines += ["", "#### 函数细节" if zh else "#### Function details", ""]
        for fn in funcs:
            lines += _render_function(fn, lang)
            lines.append("")            # blank line between functions
    return "\n".join(lines).rstrip() + "\n"
