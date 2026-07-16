# -*- coding: utf-8 -*-
"""read_files.py — per-file reading pass (bottom-up skeleton input).

Phase 2a. The codebase is read bottom-up with FULL coverage: this module
**reads every file** and produces a per-file card (purpose, and in deep mode a
detailed description + the graph-derived function inventory with relations), so
the later stage-synthesis step (synth_stages.py) can divide the system into
stages — and Phase 3 narrate it — with the FILE as the handbook's leaf node.

Cost: O(files) LLM calls (batched + parallel). Each file's real source is read
(full file by default; truncated only if a char cap is set), not just the
graph-derived signature, so the card reflects what the file actually does.

Output: one card per file under cards_dir/ (written incrementally, crash-safe),
named by url-quoted path, plus cards_dir/_coverage.json. Each card:
    {
      "file": "<path>",
      "purpose": str, "role": str, "lifecycle": str,
      # deep mode also:
      "description": str,
      "functions": [ {id, qualname, line_range, signature,
                      calls, called_by, ext_calls, n_*,
                      purpose, data_flow, relations}, ... ],
    }
read_purposes() also returns {"file_purposes": {path: card}, "coverage": {...}}
in memory; load_cards(cards_dir) reconstructs that map from disk for 2b/2c.

`role` is constrained to a small vocabulary so synth_stages can group on it;
`lifecycle` is a short free-text hint (startup / main loop / teardown / ...).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "shared"))

from api_client import Api  # noqa: E402

import nav_pack as navmod  # noqa: E402

logger = logging.getLogger(__name__)

# Constrained role vocabulary — synth_stages groups/sorts on these, so they must
# be stable. The model is told to pick the closest one.
_ROLES = (
    "entrypoint", "orchestration", "domain_logic", "io_transport",
    "data_model", "config", "util", "test", "generated", "other",
)

_RULES = f"""You are reading SOURCE FILES one by one and writing a short, plain-language
PURPOSE for each, to drive a system handbook meant for a curious NON-EXPERT
reader. For each file you get its path and a head excerpt of its real source.

For EACH file return:
- "purpose": 1-2 plain sentences a newcomer can understand — what this file is
  responsible for and why it matters, in everyday terms. Be concrete about the
  key thing it does, but avoid unexplained jargon.
- "role": EXACTLY one of: {', '.join(_ROLES)}.
    entrypoint    = process/CLI/binary entry, main(), top-level run loop
    orchestration = wires phases together, dispatch, setup/teardown, drivers
    domain_logic  = the actual feature/business logic of a subsystem
    io_transport  = network/disk/IPC/serialization/protocol plumbing
    data_model    = types/structs/enums/schemas with little behavior
    config        = configuration loading/definition
    util          = small generic helpers used across the system
    test          = tests / fixtures / mocks
    generated     = generated or vendored code
    other         = none of the above
- "lifecycle": a SHORT hint of when in the run this file is active, e.g.
  "startup", "config load", "main loop", "request handling", "turn execution",
  "teardown", "cross-cutting", or "none".

OUTPUT — ONLY a JSON object in a ```json block:
{{
  "purposes": [
    {{"file": "<exact path>", "purpose": "...", "role": "<role>", "lifecycle": "..."}},
    ...
  ]
}}
Return one entry per file given, using the exact file paths provided."""


# Deep mode: the FILE is the handbook's leaf node, so its description IS the
# handbook content for that file — read the whole file and be thorough.
#
# PLAIN-LANGUAGE MODE: the handbook is written for a curious NON-EXPERT reader —
# someone smart but new to this codebase and its technology, maybe not even a
# specialist programmer. Explain like you're talking to that person, not to the
# author of the code.
_RULES_DEEP = f"""You are reading SOURCE FILES IN FULL and writing a plain-language, easy-to-follow
description of each, for a system handbook in which the FILE is the smallest unit
(its leaf node). The description you write IS the handbook's content for this
file.

WHO YOU ARE WRITING FOR: a curious OUTSIDER — someone intelligent but new to this
project and possibly not an expert programmer in this area. They should be able
to read your text and come away genuinely understanding what this file does and
why it matters, WITHOUT having to already know the codebase or the jargon.

HOW TO WRITE (this matters as much as the content):
- Use plain, everyday language. Prefer short, clear sentences over dense ones.
- Explain the WHY and the WHAT in human terms before any mechanism: what real
  problem does this file solve, and what would break without it?
- When you must use a technical term or acronym, explain it in plain words the
  first time (e.g. "a mutex (a lock that stops two tasks touching the same data
  at once)"). Never leave jargon unexplained.
- Use a brief everyday analogy when it genuinely makes something click.
- Do NOT dump implementation trivia. Favor the big picture and intuition. It is
  fine to name key types/functions, but always say what they're FOR in plain
  words, not just that they exist.
- Stay accurate. Simplify, but never say something that is actually wrong.
- Avoid empty filler like "handles"/"manages related logic"; be specific about
  what actually happens, just in accessible language.

Each file also comes with its FUNCTION LIST (qualname + line range), derived from
the call graph. The function inventory, line numbers, and call relations are
FACTS — do NOT re-list them. Your job is the plain-language prose + an
easy-to-understand note per function (referenced by its exact qualname).

For EACH file return:
- "purpose": 1-2 plain sentences a newcomer can understand: what this file is
  for, in everyday terms.
- "description": an accessible walkthrough (roughly 120-300 words) written for the
  outsider described above: what problem this file solves and why it exists; what
  it does, step by step, in plain language; how its main pieces work together
  (like parts of a machine); and any surprising or important behavior a reader
  should know. Explain jargon inline. Concrete but accessible — no "handles"/
  "manages" hand-waving.
- "functions": one entry per function in the file's function list, referenced by
  its exact "qualname", each with:
    - "purpose": in plain words, what this function does and why someone would
      use it (1-3 sentences). Avoid jargon or explain it.
    - "data_flow": what goes IN (its inputs and the information it reads) → what
      it does with that → what comes OUT (its result and anything it changes),
      told as a simple before→after story a non-expert can follow.
    - "relations": how it fits into the bigger flow — who calls on it and when,
      and what it hands off to and why. Tell it as a short story grounded in the
      provided "calls"/"called by" facts; don't just list names.
- "role": EXACTLY one of: {', '.join(_ROLES)}.
    entrypoint    = process/CLI/binary entry, main(), top-level run loop
    orchestration = wires phases together, dispatch, setup/teardown, drivers
    domain_logic  = the actual feature/business logic of a subsystem
    io_transport  = network/disk/IPC/serialization/protocol plumbing
    data_model    = types/structs/enums/schemas with little behavior
    config        = configuration loading/definition
    util          = small generic helpers used across the system
    test          = tests / fixtures / mocks
    generated     = generated or vendored code
    other         = none of the above
- "lifecycle": SHORT hint of when in the run this file is active (e.g. "startup",
  "config load", "main loop", "request handling", "teardown", "cross-cutting").

OUTPUT — ONLY a JSON object in a ```json block:
{{
  "purposes": [
    {{"file": "<exact path>", "purpose": "...", "description": "...",
      "role": "<role>", "lifecycle": "...",
      "functions": [
        {{"qualname": "<fn qualname>", "purpose": "...", "data_flow": "...",
          "relations": "..."}}, ...]}},
    ...
  ]
}}
Return one entry per file given, using the exact file paths provided."""


# ─── Chinese prompt variants (lang="zh") ─────────────────────────────────────
# Same JSON schema and the same English role-enum VALUES (entrypoint/...), so
# downstream parsing/classification is unchanged — only the prose values
# (purpose/description/data_flow/relations) are written in Chinese.

_RULES_ZH = f"""你在逐个阅读源文件，为每个文件写一句简短、**通俗易懂的白话用途**（PURPOSE），用于驱动
一本给**外行**读者看的系统手册。每个文件会给你它的路径和真实源码的开头片段。

对每个文件返回：
- "purpose"：1-2 句大白话，让新手一看就懂——这个文件负责什么、为什么重要。要具体点出它做的
  关键事，但不要留下没解释的行话。
- "role"：必须是以下之一（保持英文枚举值不变）：{', '.join(_ROLES)}。
    entrypoint    = 进程/CLI/二进制入口、main()、顶层运行循环
    orchestration = 串联各阶段、分发、setup/teardown、驱动器
    domain_logic  = 某子系统真正的功能/业务逻辑
    io_transport  = 网络/磁盘/IPC/序列化/协议管道
    data_model    = 类型/结构体/枚举/schema，几乎无行为
    config        = 配置加载/定义
    util          = 跨系统使用的小型通用辅助
    test          = 测试/fixture/mock
    generated     = 生成或 vendored 代码
    other         = 以上都不是
- "lifecycle"：简短提示该文件在运行中何时活跃，如 "startup"、"config load"、"main loop"、
  "request handling"、"turn execution"、"teardown"、"cross-cutting" 或 "none"。

输出——只输出一个 ```json 块中的 JSON 对象（**JSON 的 key 用英文，值用中文**）：
{{
  "purposes": [
    {{"file": "<exact path>", "purpose": "...", "role": "<role>", "lifecycle": "..."}},
    ...
  ]
}}
为给定的每个文件返回一条，使用提供的确切文件路径。"""


_RULES_DEEP_ZH = f"""你在**完整阅读**源文件，并为每个文件写一段**通俗易懂的白话描述**，用于一本「文件是最小
单元（叶子节点）」的系统手册。你写的描述**就是**该文件在手册中的内容。

**你在写给谁看**：一个聪明但**外行**的读者——他对这个项目和相关技术都不熟悉，甚至不一定是这方面
的专业程序员。他应该能读完你的文字后，**真正明白**这个文件是干什么的、为什么重要，而**不需要**事先
懂这套代码或行话。

**怎么写（和写什么一样重要）**：
- 用平实的大白话、口语化的中文。多用短句，少用又长又绕的句子。
- 先讲清楚 **为什么** 和 **是什么**（这个文件解决了什么实际问题？没有它会出什么岔子？），再谈机制。
- 必须用到技术术语或缩写时，第一次出现就用大白话解释一下（例如「互斥锁（一把锁，防止两个任务
  同时改同一份数据）」）。绝不留下没解释的行话。
- 如果一个日常生活里的类比能让读者「秒懂」，就用一个简短的类比。
- **不要**堆砌实现细节。抓大局、讲直觉。可以点出关键的类型/函数，但一定要用大白话说清它们是
  **干什么用的**，而不只是说它们存在。
- 保持准确。可以简化，但绝不能说错。
- 别写「处理相关逻辑」「负责管理」这种空话，要具体，但要用外行也懂的说法。

每个文件还附带它的**函数列表**（qualname + 行号范围），来自调用图。函数清单、行号和调用关系都是
**事实**——不要重新罗列。你的工作是写白话描述 + 为每个函数（按其确切 qualname 引用）写一句外行
也能懂的说明。

对每个文件返回：
- "purpose"：1-2 句大白话，让新手一看就懂：这个文件是干什么的、为什么有用。
- "description"：一段外行也能看懂的讲解（约 120-300 字）：这个文件解决了什么问题、为什么存在；
  它一步步做了什么（用大白话）；它的几个主要部件如何像机器零件一样配合工作；有哪些让人意外或
  重要、读者应该知道的行为。行话要就地解释。要具体，但要好懂——不要含糊其辞。
- "functions"：文件函数列表里每个函数一条，按其确切 "qualname" 引用，各含：
    - "purpose"：用大白话说这个函数干什么、为什么有人会用它（1-3 句）。少用行话，用了就解释。
    - "data_flow"：**进去什么**（输入和它读取的信息）→ **它拿这些做了什么** → **出来什么**
      （结果，以及它改动了什么），像讲一个「之前→之后」的小故事，让外行也能跟上。
    - "relations"：它在整个流程里的位置——谁在什么时候会用到它、它又把活儿交给谁、为什么。
      基于提供的 "calls"/"called by" 事实，讲成一个简短的小故事，别只罗列名字。
- "role"：必须是以下之一（保持英文枚举值不变）：{', '.join(_ROLES)}。
    entrypoint    = 进程/CLI/二进制入口、main()、顶层运行循环
    orchestration = 串联各阶段、分发、setup/teardown、驱动器
    domain_logic  = 某子系统真正的功能/业务逻辑
    io_transport  = 网络/磁盘/IPC/序列化/协议管道
    data_model    = 类型/结构体/枚举/schema，几乎无行为
    config        = 配置加载/定义
    util          = 跨系统使用的小型通用辅助
    test          = 测试/fixture/mock
    generated     = 生成或 vendored 代码
    other         = 以上都不是
- "lifecycle"：简短提示该文件在运行中何时活跃（如 "startup"、"config load"、"main loop"、
  "request handling"、"teardown"、"cross-cutting"）。

输出——只输出一个 ```json 块中的 JSON 对象（**JSON 的 key 用英文，值用中文**）：
{{
  "purposes": [
    {{"file": "<exact path>", "purpose": "...", "description": "...",
      "role": "<role>", "lifecycle": "...",
      "functions": [
        {{"qualname": "<fn qualname>", "purpose": "...", "data_flow": "...",
          "relations": "..."}}, ...]}},
    ...
  ]
}}
为给定的每个文件返回一条，使用提供的确切文件路径。"""


def _rules_for(detail: str, lang: str) -> str:
    """Pick the rules prompt for (detail, lang). Defaults to English."""
    if lang == "zh":
        return _RULES_DEEP_ZH if detail == "deep" else _RULES_ZH
    return _RULES_DEEP if detail == "deep" else _RULES


def build_inventory(graph: dict, *, rel_cap: int = 25) -> dict[str, list[dict]]:
    """Deterministic per-file function inventory + call relations from the graph.

    The function list, each function's qualname (its index), line range,
    signature, and call relations are FACTS in graph.json — derive them here
    instead of asking the LLM to enumerate (which it gets wrong). The LLM only
    adds per-function prose (purpose/data_flow/relations), merged in by qualname.

    Returns {file: [ {id, qualname, name, class_name, line_range, signature,
                      calls, called_by, ext_calls, n_calls, n_called_by,
                      n_ext_calls}, ... ]}.
    `id` is the unique node id (the function's index). `calls`/`called_by` are
    relations to OTHER INTERNAL functions, given as those neighbours' unique ids
    (capped at rel_cap; true totals in n_calls/n_called_by). Relations are keyed
    by node id, NOT qualname — Rust free functions share bare qualnames (`run`,
    `main`), so keying by qualname would merge every same-named function's edges."""
    nodes = graph["nodes"]
    internal = {nid for nid, n in nodes.items()
                if n.get("kind") == "internal" and not n.get("synthetic")}
    calls: dict[str, set[str]] = defaultdict(set)
    called_by: dict[str, set[str]] = defaultdict(set)
    ext_calls: dict[str, set[str]] = defaultdict(set)
    for e in graph.get("edges", []):
        cid, eid = e.get("caller_id"), e.get("callee_id")
        if cid not in internal or cid == eid:
            continue
        if eid in internal:
            # internal→internal: accurate, locatable relation (keyed by id).
            calls[cid].add(eid)
            called_by[eid].add(cid)
        else:
            # Cross-module / library call. The Rust adapter resolves these to
            # `boundary` nodes, so we record the TARGET NAME (not a node id) —
            # honest about it being unresolved rather than misattributing it to
            # some same-named internal function.
            tgt = nodes.get(eid, {}).get("qualname") if isinstance(eid, str) else None
            if not tgt and isinstance(eid, str):
                tgt = eid[len("boundary:"):] if eid.startswith("boundary:") else eid
            if tgt:
                ext_calls[cid].add(tgt)

    inv: dict[str, list[dict]] = defaultdict(list)
    for nid in internal:
        n = nodes[nid]
        if n.get("line_start") is None:
            continue
        c, cb = sorted(calls.get(nid, ())), sorted(called_by.get(nid, ()))
        ec = sorted(ext_calls.get(nid, ()))
        inv[n["file"]].append({
            "id": nid,
            "qualname": n.get("qualname"),
            "name": n.get("name"),
            "class_name": n.get("class_name") or "",
            "line_range": [n.get("line_start"), n.get("line_end")],
            "signature": (n.get("signature") or "")[:200],
            "calls": c[:rel_cap],
            "called_by": cb[:rel_cap],
            "ext_calls": ec[:rel_cap],
            "n_calls": len(c),
            "n_called_by": len(cb),
            "n_ext_calls": len(ec),
        })
    for fns in inv.values():
        fns.sort(key=lambda x: x["line_range"][0] or 0)
    return inv


def _read_excerpt(source_root: Path, rel: str, max_chars: int) -> str:
    """A file's real source. With max_chars <= 0 the WHOLE file is returned (no
    truncation — the right default for deep mode). Otherwise a head excerpt is
    returned (head carries imports, top-level types and entry functions)."""
    try:
        text = (source_root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"(could not read: {e})"
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"


def _file_block(source_root: Path, f: dict, max_chars: int,
                funcs: list[dict] | None = None, detail: str = "brief") -> str:
    """A file's descriptor for the prompt: path + source excerpt, plus (in deep
    mode) the graph-derived function list so the LLM annotates each by name."""
    rel = f["file"]
    classes = f.get("classes") or []
    cls = f"  classes={classes}" if classes else ""
    excerpt = _read_excerpt(source_root, rel, max_chars)
    block = (f"### FILE: {rel}  ({f.get('n_functions', 0)} fn){cls}\n"
             f"```\n{excerpt}\n```")
    if detail == "deep" and funcs:
        def _short(ids, n=8):
            names = [i.split("::")[-1] for i in ids[:n]]
            return ", ".join(names) + (f" (+{len(ids) - n} more)" if len(ids) > n else "")
        lines = []
        for fn in funcs:
            lines.append(
                f"  - {fn['qualname']}  (lines {fn['line_range'][0]}-{fn['line_range'][1]})")
            calls = (fn.get("calls") or []) + [f"{x}(ext)" for x in (fn.get("ext_calls") or [])]
            if calls:
                lines.append(f"      calls: {_short(calls)}")
            if fn.get("called_by"):
                lines.append(f"      called by: {_short(fn['called_by'])}")
        block += ("\n#### Functions to annotate (reference each by its qualname; "
                  "call facts from the graph):\n" + "\n".join(lines))
    return block


def _build_batch_prompt(source_root: Path, batch: list[dict], max_chars: int,
                        detail: str = "brief",
                        inventory: dict[str, list[dict]] | None = None,
                        lang: str = "en") -> str:
    inv = inventory or {}
    blocks = "\n\n".join(
        _file_block(source_root, f, max_chars, inv.get(f["file"]), detail)
        for f in batch)
    rules = _rules_for(detail, lang)
    return "\n".join([
        rules,
        "",
        f"## Files to describe ({len(batch)})",
        blocks,
        "",
        "Return the JSON block only, one entry per file above.",
    ])


_ANNOTATION_FIELDS = ("purpose", "data_flow", "relations")


def _merge_function_notes(graph_funcs: list[dict], llm_funcs: list) -> list[dict]:
    """Attach the LLM's per-function prose (purpose / data_flow / relations) onto
    the authoritative graph inventory, matched by qualname then name. Inventory
    (ids, line ranges, call edges) is always complete; the prose is best-effort.

    Function name is kept as `_merge_function_notes` for callers; it now merges
    the richer annotation object, not just a one-line note."""
    by_qn: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for fn in llm_funcs or []:
        if not isinstance(fn, dict):
            continue
        ann = {k: (fn.get(k) or "").strip() for k in _ANNOTATION_FIELDS}
        # tolerate the older one-line "note" shape by folding it into purpose
        if not ann["purpose"] and fn.get("note"):
            ann["purpose"] = (fn.get("note") or "").strip()
        if fn.get("qualname"):
            by_qn[fn["qualname"]] = ann
        if fn.get("name"):
            by_name.setdefault(fn["name"], ann)
    out = []
    for gf in graph_funcs:
        ann = by_qn.get(gf["qualname"]) or by_name.get(gf["name"]) \
            or {k: "" for k in _ANNOTATION_FIELDS}
        out.append({**gf, **ann})
    return out


def _describe_batch(api: Api, source_root: Path, batch: list[dict],
                    max_chars: int, detail: str = "brief",
                    inventory: dict[str, list[dict]] | None = None,
                    lang: str = "en"
                    ) -> dict[str, dict]:
    """Describe one batch of files. Returns {file: entry}. In deep mode each
    entry also carries `description` + the merged `functions` inventory. Files
    the LLM drops are left out (caller backfills as undescribed)."""
    prompt = _build_batch_prompt(source_root, batch, max_chars, detail, inventory, lang)
    try:
        result = api.call(prompt, params={"temperature": 0.0})
    except Exception as e:  # noqa: BLE001
        logger.warning("read_files batch crashed: %s", e)
        return {}
    parsed = result.parsed_json
    if not isinstance(parsed, dict):
        return {}
    inv = inventory or {}
    out: dict[str, dict] = {}
    batch_files = {f["file"] for f in batch}
    for p in parsed.get("purposes", []) or []:
        if not isinstance(p, dict):
            continue
        fpath = p.get("file")
        if fpath not in batch_files:
            continue
        role = p.get("role")
        if role not in _ROLES:
            role = "other"
        entry = {
            "purpose": (p.get("purpose") or "").strip(),
            "role": role,
            "lifecycle": (p.get("lifecycle") or "").strip(),
        }
        if detail == "deep":
            entry["description"] = (p.get("description") or "").strip()
            entry["functions"] = _merge_function_notes(
                inv.get(fpath, []), p.get("functions") or [])
        out[fpath] = entry
    return out


# ─── Chunked fallback for files too big to read whole ────────────────────────


def _read_lines(source_root: Path, rel: str) -> list[str] | None:
    try:
        return (source_root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _func_source(lines: list[str], fn: dict) -> str:
    a, b = fn.get("line_range") or (None, None)
    if not a:
        return ""
    return "\n".join(lines[a - 1:b])  # b inclusive


def _chunk_funcs(funcs: list[dict], lines: list[str], chunk_chars: int
                 ) -> list[list[dict]]:
    """Greedily group functions so each chunk's combined source stays under
    chunk_chars (always ≥1 function per chunk)."""
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for fn in funcs:
        s = len(_func_source(lines, fn))
        if cur and size + s > chunk_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(fn)
        size += s
    if cur:
        chunks.append(cur)
    return chunks


def _build_chunk_prompt(rel: str, chunk_funcs: list[dict], lines: list[str],
                        lang: str = "en") -> str:
    blocks = []
    for fn in chunk_funcs:
        calls = (fn.get("calls") or []) + [f"{x}(ext)" for x in (fn.get("ext_calls") or [])]
        ground = ""
        if calls:
            ground += "\n  calls: " + ", ".join(c.split("::")[-1] for c in calls[:8])
        if fn.get("called_by"):
            ground += "\n  called by: " + ", ".join(c.split("::")[-1] for c in fn["called_by"][:8])
        blocks.append(
            f"#### {fn['qualname']}  (lines {fn['line_range'][0]}-{fn['line_range'][1]}){ground}\n"
            f"```\n{_func_source(lines, fn)}\n```")
    return "\n".join([
        _rules_for("deep", lang),
        "",
        f"## File (too large for one pass — processing a CHUNK of its functions): {rel}",
        "Describe ONLY the functions below (with their source). Give the file-level "
        "purpose/description as best you can from this chunk.",
        "\n\n".join(blocks),
        "",
        f"Return ONE entry for {rel} covering exactly these functions.",
    ])


def _describe_file_chunked(api: Api, source_root: Path, file_dict: dict,
                           funcs: list[dict], chunk_chars: int,
                           lang: str = "en") -> dict:
    """Fallback when the whole-file deep call failed (usually context-length):
    slice the file by function (using graph line ranges), describe each chunk,
    and merge. The function inventory is always preserved even if chunks fail."""
    rel = file_dict["file"]
    lines = _read_lines(source_root, rel)
    base = {"purpose": "", "role": "other", "lifecycle": "none", "description": "",
            "functions": _merge_function_notes(funcs, [])}
    if lines is None or not funcs:
        return base
    chunks = _chunk_funcs(funcs, lines, chunk_chars)
    logger.info("[chunked] %s: %d fn → %d chunk(s)", rel, len(funcs), len(chunks))
    all_llm_funcs: list = []
    descs: list[str] = []
    purpose, role, lifecycle = "", None, ""
    for ci, chunk in enumerate(chunks):
        try:
            res = api.call(_build_chunk_prompt(rel, chunk, lines, lang), params={"temperature": 0.0})
            parsed = res.parsed_json
        except Exception as e:  # noqa: BLE001
            logger.warning("[chunked] %s chunk %d/%d failed: %s", rel, ci + 1, len(chunks), e)
            continue
        if not isinstance(parsed, dict):
            continue
        entries = parsed.get("purposes") or []
        ent = entries[0] if entries else {}
        all_llm_funcs += ent.get("functions") or []
        if ent.get("description"):
            descs.append(ent["description"].strip())
        if not purpose and ent.get("purpose"):
            purpose = ent["purpose"].strip()
        if role is None and ent.get("role") in _ROLES:
            role = ent["role"]
        if not lifecycle and ent.get("lifecycle"):
            lifecycle = ent["lifecycle"].strip()
    base["purpose"] = purpose
    base["role"] = role or "other"
    base["lifecycle"] = lifecycle or "none"
    base["description"] = " ".join(descs)
    base["functions"] = _merge_function_notes(funcs, all_llm_funcs)
    return base


def _describe_batch_safe(api: Api, source_root: Path, batch: list[dict],
                         max_chars: int, detail: str, inventory: dict,
                         chunk_chars: int, lang: str = "en") -> dict[str, dict]:
    """Three-tier graceful degradation so small files batch cheaply while large
    ones still get read:

      Tier 1 — describe the whole batch in one call (cheap; the point of batching).
      Tier 2 — for any file the batch dropped (batch too large / partial return),
               retry it ALONE (one file per call).
      Tier 3 — deep only: a single file that STILL fails (too large for one pass)
               is split by function and merged (`_describe_file_chunked`).
    """
    result = _describe_batch(api, source_root, batch, max_chars, detail, inventory, lang)

    # Tier 2: retry dropped files individually (skip if the batch was already 1).
    missing = [f for f in batch if f["file"] not in result]
    if missing and len(batch) > 1:
        logger.info("[2a] batch dropped %d/%d file(s) — retrying individually",
                    len(missing), len(batch))
        for f in missing:
            result.update(_describe_batch(api, source_root, [f], max_chars,
                                          detail, inventory, lang))
        missing = [f for f in batch if f["file"] not in result]

    # Tier 3: function-chunk the files that still failed (deep only).
    if detail == "deep":
        for f in missing:
            logger.info("[2a] %s still too large alone — retrying by function chunks",
                        f["file"])
            result[f["file"]] = _describe_file_chunked(
                api, source_root, f, (inventory or {}).get(f["file"], []), chunk_chars, lang)
    return result


# ─── Per-file card persistence ───────────────────────────────────────────────


def _card_path(cards_dir: Path, rel: str) -> Path:
    """Card path mirrors the source tree: cards/<rel>.json (readable + navigable,
    e.g. cards/app-server/src/lib.rs.json), rather than a url-encoded flat name."""
    return Path(cards_dir) / (rel + ".json")


def _write_card(cards_dir: Path, rel: str, entry: dict) -> None:
    """Write one file's card to disk immediately (incremental persistence — a
    crash mid-run keeps every card already produced). The card is self-describing
    via its "file" key. A single failed write (e.g. OS path-length limit on a
    deep path) is logged, not fatal — the rest of the run continues."""
    try:
        p = _card_path(cards_dir, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"file": rel, **entry}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except OSError as e:
        logger.warning("could not write card for %s: %s", rel, e)


def load_cards(cards_dir: Path) -> dict[str, dict]:
    """Load all per-file cards back into the {file: card} map downstream expects.
    A card is identified by its "file" key (not the filename), so meta files like
    _coverage.json (no "file" key) are skipped naturally — and source files whose
    path happens to start with '_' are NOT wrongly dropped."""
    out: dict[str, dict] = {}
    for p in sorted(Path(cards_dir).rglob("*.json")):  # recursive: tree-mirrored
        try:
            card = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rel = card.get("file") if isinstance(card, dict) else None
        if rel:
            out[rel] = {k: v for k, v in card.items() if k != "file"}
    return out


def _is_done(card: dict, detail: str) -> bool:
    """A card worth keeping on --resume: has a real purpose, and in deep mode a
    real description or at least one annotated function. Empty/failed/backfilled
    cards (and brief cards when now running deep) are treated as NOT done, so they
    get retried."""
    if not card.get("purpose"):
        return False
    if detail == "deep":
        return bool(card.get("description")) or any(
            f.get("purpose") for f in card.get("functions", []))
    return True


def read_purposes(
    api: Api,
    graph: dict,
    source_root: Path,
    *,
    cards_dir: Path | None = None,
    batch_size: int = 8,
    max_workers: int = 6,
    max_chars_per_file: int = 6000,
    detail: str = "brief",
    chunk_chars: int = 60000,
    resume: bool = False,
    lang: str = "en",
) -> dict[str, Any]:
    """Read every file in the graph and give each a card. Returns the
    file_purposes structure (see module docstring).

    If cards_dir is given, each file's card is written there the moment its batch
    completes (one JSON per file, crash-resilient) plus a `_coverage.json`.

    detail="deep" reads each file in full and writes a detailed description plus
    per-function purpose/data_flow/relations merged onto the graph-derived
    function inventory (the file is the handbook leaf). Pair with batch_size=1."""
    nav = navmod.build_nav_pack(graph)
    # ALL scanned files (incl. function-less type/schema/mod files), so cards are
    # 1:1 with the source tree — not just files that have call-graph functions.
    files = navmod.all_file_descriptors(graph, nav)
    if cards_dir:
        cards_dir = Path(cards_dir)
        cards_dir.mkdir(parents=True, exist_ok=True)

    # Deep mode attaches the deterministic per-file function inventory + relations.
    inventory = build_inventory(graph) if detail == "deep" else {}

    # --resume: keep already-good cards, only (re)process the rest.
    file_purposes: dict[str, dict] = {}
    if resume and cards_dir:
        existing = load_cards(cards_dir)
        done = {rel: card for rel, card in existing.items() if _is_done(card, detail)}
        file_purposes.update(done)
        before = len(files)
        files = [f for f in files if f["file"] not in done]
        logger.info("resume: %d/%d files already done, %d to process",
                    len(done), before, len(files))

    batches = [files[i:i + batch_size] for i in range(0, len(files), batch_size)]
    cap = "no limit" if max_chars_per_file <= 0 else f"{max_chars_per_file} chars"
    logger.info("read_files: %d files in %d batch(es) (cap/file=%s, detail=%s%s)",
                len(files), len(batches), cap, detail,
                f", cards→{cards_dir}" if cards_dir else "")

    from progress import Progress
    prog = Progress(logger, "2a read_files", len(batches))
    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_describe_batch_safe, api, source_root, b,
                            max_chars_per_file, detail, inventory, chunk_chars, lang): i
                for i, b in enumerate(batches)}
        for fut in cf.as_completed(futs):
            i = futs[fut]
            try:
                result = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.warning("read_files batch %d failed: %s", i, e)
                result = {}
            file_purposes.update(result)
            # Persist each card now (main thread → no concurrent disk writes).
            if cards_dir:
                for rel, entry in result.items():
                    _write_card(cards_dir, rel, entry)
            prog.tick(note=f"{len(file_purposes)} files described")

    # Backfill dropped files so coverage is honest (synth still sees the path).
    # Even when the LLM dropped a file, keep its graph-derived inventory in deep
    # mode (the function facts don't depend on the LLM).
    missing: list[str] = []
    for f in files:
        if f["file"] not in file_purposes:
            entry = {"purpose": "", "role": "other", "lifecycle": "none"}
            if detail == "deep":
                entry["description"] = ""
                entry["functions"] = _merge_function_notes(
                    inventory.get(f["file"], []), [])
            file_purposes[f["file"]] = entry
            missing.append(f["file"])
            if cards_dir:
                _write_card(cards_dir, f["file"], entry)

    # Totals over ALL files (resumed-done + processed + backfilled), not just the
    # to-process slice — so --resume coverage stays honest.
    coverage = {
        "n_files": len(file_purposes),
        "n_described": len(file_purposes) - len(missing),
        "missing": sorted(missing),
    }
    if cards_dir:
        (cards_dir / "_coverage.json").write_text(
            json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("read_files: %d/%d described, %d undescribed",
                coverage["n_described"], coverage["n_files"], len(missing))
    return {"file_purposes": file_purposes, "coverage": coverage}


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(format="[%(levelname)5s] %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(description="Per-file purpose pass (read every file)")
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--source-root", type=Path, required=True)
    ap.add_argument("--cards-dir", type=Path, required=True,
                    help="directory for per-file cards (one JSON/file + _coverage.json)")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--max-chars-per-file", type=int, default=0,
                    help="0 = no truncation (read the whole file)")
    ap.add_argument("--detail", choices=["brief", "deep"], default="brief",
                    help="brief: 1-line purpose (batched). deep: full-file read "
                         "→ detailed description + per-function "
                         "purpose/data_flow/relations on the graph inventory.")
    ap.add_argument("--chunk-chars", type=int, default=60000,
                    help="deep: chunk size for the function-split retry on files "
                         "too large for one pass")
    ap.add_argument("--resume", action="store_true",
                    help="skip files that already have a good card in cards-dir")
    ap.add_argument("--lang", choices=["en", "zh"], default="en",
                    help="narration language for card prose (en default; zh = "
                         "Chinese purpose/description/data_flow/relations)")
    args = ap.parse_args(argv)

    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    api = Api()
    res = read_purposes(api, graph, args.source_root.resolve(),
                        cards_dir=args.cards_dir,
                        batch_size=args.batch_size,
                        max_chars_per_file=args.max_chars_per_file,
                        detail=args.detail,
                        chunk_chars=args.chunk_chars,
                        resume=args.resume,
                        lang=args.lang)
    logger.info("wrote %d cards to %s", res["coverage"]["n_files"], args.cards_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
