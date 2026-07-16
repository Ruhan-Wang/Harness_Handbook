# -*- coding: utf-8 -*-
"""Phase 3 translation — convert one (stage, qualname) translation unit into a 7-section JSON.

The unit of translation is one qualname within one stage:
  - single-entry function          → "single" schema
  - same qualname appears as N regions (non-contiguous OK) → "multi_region" schema

Caching: ``cache/translate/<stage>/<qualname>.json``, keyed by concatenated sha1s
of all entries in this unit. Source unchanged → cache hit → no LLM call.

Sequential within a stage: the prompt for the i-th unit references already-translated
sibling units' synopses (准则 6 横向连接).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allow standalone invocation: make phase2's api_client + sibling extract_source importable.
_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent, _HERE.parent / "phase2"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from api_client import Api, LLMCallResult  # noqa: E402
from extract_source import Snippet, extract_from_member  # noqa: E402
from project_context import get_project_context  # noqa: E402

logger = logging.getLogger(__name__)


# ─── Data shapes ──────────────────────────────────────────────────────────────


# Bump when the Tier 3 prompt / schema changes so caches auto-invalidate.
# v3-generic: intro/examples genericized (project injected via project_context).
TIER3_PROMPT_VERSION = "v3-generic"


@dataclass
class TranslationUnit:
    """One translation atom = all mapping entries for one qualname within one stage."""

    stage_id: str
    qualname: str
    entries: list[dict] = field(default_factory=list)  # raw mapping member dicts
    snippets: list[Snippet] = field(default_factory=list)  # parallel to entries
    type_kind: str = "single"  # or "multi_region"

    @property
    def cache_key(self) -> str:
        sha = hashlib.sha1()
        sha.update(TIER3_PROMPT_VERSION.encode())
        for snip in self.snippets:
            sha.update(snip.sha1.encode())
        return sha.hexdigest()[:16]


# ─── Unit assembly ────────────────────────────────────────────────────────────


def collect_units(
    stage_id: str,
    stage_members: list[dict],
    source_root: Path,
) -> list[TranslationUnit]:
    """Group stage members by qualname (preserving first-appearance order).

    Returned order = first-appearance order in mapping.yaml. This is what
    render_member uses to sort details blocks.
    """
    units: dict[str, TranslationUnit] = {}
    order: list[str] = []
    for mem in stage_members:
        qn = mem["qualname"]
        if qn not in units:
            units[qn] = TranslationUnit(stage_id=stage_id, qualname=qn)
            order.append(qn)
        units[qn].entries.append(mem)
        units[qn].snippets.append(extract_from_member(source_root, mem))

    out = []
    for qn in order:
        u = units[qn]
        u.type_kind = "multi_region" if len(u.entries) > 1 else "single"
        out.append(u)
    return out


# ─── Prompt ────────────────────────────────────────────────────────────────────
#
# Two language variants. Selected via `lang` parameter on translate_unit().
# Schemas are bilingual too — section names and field comments differ slightly
# in tone (e.g. EN expects active verbs and concision; ZH expects 学术专业 tone).


_PRINCIPLES_ZH = """## 翻译准则（每段必须遵守）

硬准则:
  1. 意图优先于机制——写「做什么 / 为什么」，不复述「怎么做」
  2. 锚定具体代码——每个论断能反向定位到代码段（引用变量名 / 行号 / 模式）
  3. 显式 non-obvious 决策——try/except 容忍、显式 None 检查、容错策略
  4. 准确——只描述实际存在的代码，不臆测、不预测未来

软准则:
  5. 粒度匹配复杂度——短函数整段，长函数 / 多 region 切段
  6. 横向连接——与已翻译的兄弟函数对照（见末尾「siblings_synopsis」上下文）
  7. 诚实复杂度——复杂的地方说复杂、不抹平
  8. 学术专业中文——名词化表达、正式 register，但不堆术语

段间互斥（冲突时优先级 ⑥ > ④ > ③）:
  - synopsis 只讲 What
  - execution_flow（或 regions[*].gloss）按代码顺序复述 What
  - design_decisions 独占 Why
  - relations 不含推测调用
"""


_PRINCIPLES_EN = """## Translation principles (every section must obey)

Hard rules:
  1. **Intent over mechanism** — say what the code is for and why, not a line-by-line restating of how.
  2. **Anchored to code** — every claim points back to a specific code element (a variable name, a branch, a pattern).
  3. **Surface non-obvious decisions** — `try/except` that swallows on purpose, explicit `is not None` checks, fallback strategies. These are the things a reader misses by only reading the code.
  4. **Accurate** — describe only what's actually there. Don't speculate, don't predict future behavior.

Soft rules:
  5. **Granularity matches complexity** — short function = one block; long / multi-region function = sectioned.
  6. **Cross-reference** — when relevant, link back to sibling functions already translated (see the "sibling synopses" context at the end).
  7. **Honest complexity** — say complex things are complex; don't smooth over.
  8. **Technical writing English** — active voice, concrete verbs, short sentences. Style: Python official docs, not academic prose. Avoid passive voice and nominalizations.

Section mutual exclusion (conflict priority ⑥ > ④ > ③):
  - synopsis says only What
  - execution_flow (or regions[*].gloss) says What in code order
  - design_decisions has exclusive ownership of Why
  - relations contains no speculative calls
"""

_SINGLE_SCHEMA_ZH = """## 输出格式（single）

返回 ```json 包裹的 JSON 对象，结构如下:

```json
{
  "schema_version": 1,
  "type": "single",
  "locator_role": "<一句话角色定位，10-25 个汉字>",
  "stage_context": "<2-4 句，回答「在 stage 里啥角色」「触发条件」「与兄弟关系」>",
  "synopsis": "<2-4 句，What + 输入来源 + 输出去向 + 副作用>",
  "interface": {
    "signature": "<完整签名，如 (self, environment) -> None>",
    "params": [{"name": "<参数名>", "type": "<类型，不确定写 ?>", "role": "<一句话作用/来源>"}],
    "reads_state": ["<本函数读取的实例/全局状态，如 self._some_field>"],
    "returns": "<返回什么；若为 None，说明真正的产出（副作用）>",
    "side_effects": ["<写入的状态 / 外部副作用，如 打开连接、写文件>"]
  },
  "execution_flow": [
    "<step 1: 动作 + 用什么参数/状态 + 产出什么>",
    "<step 2: ...>"
  ],
  "design_decisions": [
    "<决策 1: 取舍点 + 为何这么选 + 替代方案后果>",
    "<决策 2: ...>"
  ],
  "relations": {
    "callers": ["<qualname or 简要描述>"],
    "core_callees": ["..."],
    "config_state_sources": ["..."],
    "results_to": ["..."],
    "siblings": [],
    "register_interactions": [
      {"action": "write|read|clear|reset", "register": "reg-<id>", "note": "<10-25 字何时/为什么>"}
    ]
  }
}
```

数量约束:
  - interface.signature: 必填，照源码写准（参数、类型注解、返回注解）
  - interface 的 params / reads_state / returns / side_effects: 照源码列全，宁可多写不要漏；没有则空数组/写「无」
  - execution_flow: 2-8 个 step
  - design_decisions: 1-5 条
  - relations.{callers, core_callees, config_state_sources, results_to}: 至少 4 类必须非空
  - relations.register_interactions: 如果函数确实读写/重置任何 state_register（见输入「state_registers」部分），必须列出；否则空数组 []
"""


_SINGLE_SCHEMA_EN = """## Output format (single)

Return a JSON object inside a ```json fence:

```json
{
  "schema_version": 1,
  "type": "single",
  "locator_role": "<one-line role tag, 6-15 words>",
  "stage_context": "<2-4 sentences answering: what role does this play in the stage; when is it invoked; how does it relate to its siblings>",
  "synopsis": "<2-4 sentences: What + inputs + outputs + side effects>",
  "interface": {
    "signature": "<full signature, e.g. (self, environment) -> None>",
    "params": [{"name": "<param>", "type": "<type, or ? if unclear>", "role": "<one-line role / source>"}],
    "reads_state": ["<instance/global state this function reads, e.g. self._some_field>"],
    "returns": "<what it returns; if None, state the real product (side effect)>",
    "side_effects": ["<state written / external effects, e.g. opens a connection, writes a file>"]
  },
  "execution_flow": [
    "<step 1: action + which args/state + what's produced>",
    "<step 2: ...>"
  ],
  "design_decisions": [
    "<decision 1: the trade-off point + why this choice + what an alternative would cost>",
    "<decision 2: ...>"
  ],
  "relations": {
    "callers": ["<qualname or brief description>"],
    "core_callees": ["..."],
    "config_state_sources": ["..."],
    "results_to": ["..."],
    "siblings": [],
    "register_interactions": [
      {"action": "write|read|clear|reset", "register": "reg-<id>", "note": "<5-12 words: when / why>"}
    ]
  }
}
```

Count constraints:
  - interface.signature: required, copied accurately from the source (params, type hints, return annotation)
  - interface params / reads_state / returns / side_effects: list them all from the source; prefer completeness over brevity; empty array / "none" if there are none
  - execution_flow: 2-8 steps
  - design_decisions: 1-5 items
  - relations.{callers, core_callees, config_state_sources, results_to}: at least 4 of these must be non-empty
  - relations.register_interactions: list every register this function reads/writes/resets (from the input "state_registers" list); empty array [] only if the function genuinely interacts with none.

Style reminders for the prose fields:
  - Active voice. "It resets the buffer" not "the buffer is reset by it".
  - Concrete verbs. "Build the request" not "perform construction of the request".
  - Inline backticks for variable names and qualnames: `_state`, `Client`, `_init`.
  - No throat-clearing ("It is worth noting that...", "In essence...").
"""

_MULTI_SCHEMA_ZH = """## 输出格式（multi_region）

函数被 mapping 拆成多个 region。返回 JSON 时为每个 region 单独写 gloss；
design_decisions 与 relations 仍在函数级聚合（跨 region 的设计取舍归这里）。

```json
{
  "schema_version": 1,
  "type": "multi_region",
  "locator_role": "<整函数一句话角色定位>",
  "stage_context": "<2-4 句>",
  "synopsis": "<3-6 句，整函数 What + 总体结构 + 输入输出>",
  "interface": {
    "signature": "<整函数完整签名>",
    "params": [{"name": "<参数名>", "type": "<类型，不确定写 ?>", "role": "<一句话作用/来源>"}],
    "reads_state": ["<读取的 self._ 属性>"],
    "returns": "<返回什么；若 None，说明真正产出>",
    "side_effects": ["<写入的 self._ 属性 / 外部副作用>"]
  },
  "overall_structure": [
    {"region_idx": 1, "line_range": [a, b], "role": "<短标题>", "terminal_state": "<进入哪里 / 返回什么>"},
    {"region_idx": 2, "line_range": [c, d], "role": "...", "terminal_state": "..."}
  ],
  "regions": [
    {
      "region_idx": 1,
      "line_range": [a, b],
      "title": "<region 标题，与 overall_structure 对应>",
      "gloss": "<2-5 句 NL，讲 What + 关键代码模式 + 与下个 region 的衔接>",
      "callouts": [
        {"to_qualname": "<被调函数 qualname>", "note": "<这一刻为什么调它，10-25 字>"}
      ]
    }
  ],
  "design_decisions": [
    "<跨 region 设计取舍 1>",
    "<跨 region 设计取舍 2>"
  ],
  "relations": {
    "callers": [...],
    "core_callees": [...],
    "config_state_sources": [...],
    "results_to": [...],
    "siblings": [...],
    "register_interactions": [
      {"action": "write|read|clear|reset", "register": "reg-<id>", "note": "<10-25 字何时/为什么>"}
    ]
  }
}
```

callouts 用于「helper 在 mapping 里夹在 region 之间」的场景：例如 _unwind_messages_to_free_tokens
位于 _query_llm region 2 和 region 3 之间，则在 region 2 的 callouts 里标明它会被调用。
callouts 可以为空数组 []。

register_interactions 的 action 必须是 write/read/clear/reset 之一；register 字段必须取自输入「state_registers」列表的真实 id。函数若不与任何 register 交互，给空数组 []。
"""


_MULTI_SCHEMA_EN = """## Output format (multi_region)

The function is split into multiple regions in mapping. Write a gloss per region;
keep design_decisions and relations at the function level (decisions that span
regions belong here).

```json
{
  "schema_version": 1,
  "type": "multi_region",
  "locator_role": "<one-line role tag for the whole function>",
  "stage_context": "<2-4 sentences>",
  "synopsis": "<3-6 sentences: function-wide What + overall structure + inputs/outputs>",
  "interface": {
    "signature": "<full signature of the whole function>",
    "params": [{"name": "<param>", "type": "<type, or ? if unclear>", "role": "<one-line role / source>"}],
    "reads_state": ["<self._ attributes read>"],
    "returns": "<what it returns; if None, state the real product>",
    "side_effects": ["<self._ attributes written / external effects>"]
  },
  "overall_structure": [
    {"region_idx": 1, "line_range": [a, b], "role": "<short label>", "terminal_state": "<where control goes next / what's returned>"},
    {"region_idx": 2, "line_range": [c, d], "role": "...", "terminal_state": "..."}
  ],
  "regions": [
    {
      "region_idx": 1,
      "line_range": [a, b],
      "title": "<region title, matching overall_structure>",
      "gloss": "<2-5 sentences: What + key code pattern + how it hands off to the next region>",
      "callouts": [
        {"to_qualname": "<qualname of called helper>", "note": "<5-12 words: why it's called here>"}
      ]
    }
  ],
  "design_decisions": [
    "<cross-region decision 1>",
    "<cross-region decision 2>"
  ],
  "relations": {
    "callers": [...],
    "core_callees": [...],
    "config_state_sources": [...],
    "results_to": [...],
    "siblings": [...],
    "register_interactions": [
      {"action": "write|read|clear|reset", "register": "reg-<id>", "note": "<5-12 words: when / why>"}
    ]
  }
}
```

`callouts` is for the case where a helper appears in mapping between this
function's regions — e.g. `_unwind_messages_to_free_tokens` sits between
`_query_llm` region 2 and region 3 in the stage member list, so region 2 should
declare it in `callouts`. Empty array [] is fine if there are none.

`register_interactions` action must be one of write/read/clear/reset. The
`register` field must be a real id from the "state_registers" input list. Empty
array [] only if the function interacts with no registers.

Style reminders:
  - Active voice.
  - Short sentences.
  - No throat-clearing.
"""


_PROMPT_FRAGMENTS_BY_LANG = {
    "zh": {
        "principles": _PRINCIPLES_ZH,
        "single_schema": _SINGLE_SCHEMA_ZH,
        "multi_schema": _MULTI_SCHEMA_ZH,
    },
    "en": {
        "principles": _PRINCIPLES_EN,
        "single_schema": _SINGLE_SCHEMA_EN,
        "multi_schema": _MULTI_SCHEMA_EN,
    },
}

_PROMPT_HEADERS = {
    "zh": {
        "intro": "你是 {project_name} 项目的资深工程师，也是这本 handbook 的翻译师。把一个函数（或函数内多个 region）的源码翻译成中文 handbook 条目。",
        "input": "# 输入",
        "unit": "## 翻译单元",
        "stage": "## 所属 stage",
        "siblings": "## 同 stage 已翻译过的兄弟单元（最近 8 个，按翻译顺序）",
        "siblings_empty": "  (无——本 unit 是该 stage 第一个翻译)",
        "registers": "## state_registers（用于 relations.register_interactions —— 看一下源码与 self._ 字段是否对应到这些 register）",
        "registers_empty": "  (无 state_registers 定义)",
        "entries": "## 各 entry 详情",
        "self_check": (
            "# 自检（输出前内部跑一遍）\n"
            "1. 每个论断能在代码哪里指出来？\n"
            "2. design_decisions 是否点出代码里默默做的取舍？\n"
            "3. 没有把 What 写进 design_decisions、也没有把 Why 写进 synopsis 或 execution_flow？\n"
            "4. relations 的 callers / core_callees / config_state_sources / results_to 4 类都非空？\n"
            "5. 是否做到了横向连接（与上面 siblings 对照、或与同 stage 已知函数对比）？\n"
            "6. 源码里如果有对某个 state_register 的读写/自增/重置（如 `self._field = ...` / `.append(...)` / `self._n += 1`），是否在 register_interactions 里如实列出？\n\n"
            "直接输出 ```json 代码块。不要前后说明。"
        ),
        "entry_head_purpose": "**phase2 purpose**",
        "entry_head_source": "**source**",
    },
    "en": {
        "intro": "You are a senior engineer on the {project_name} project and the translator for this handbook. Convert one function (or one function's regions) into a handbook entry. Output is JSON only.",
        "input": "# Input",
        "unit": "## Translation unit",
        "stage": "## Owning stage",
        "siblings": "## Already-translated siblings in this stage (most recent 8, in translation order)",
        "siblings_empty": "  (none — this is the stage's first translation)",
        "registers": "## state_registers (use this to populate relations.register_interactions — check whether the source's self._ fields match any of these registers)",
        "registers_empty": "  (no state_registers declared)",
        "entries": "## Per-entry detail",
        "self_check": (
            "# Self-check (run silently before emitting JSON)\n"
            "1. Every claim points to specific code?\n"
            "2. design_decisions surfaces non-obvious trade-offs?\n"
            "3. No What in design_decisions, no Why in synopsis or execution_flow?\n"
            "4. All four of relations.{callers, core_callees, config_state_sources, results_to} non-empty?\n"
            "5. Cross-referenced to siblings or related functions where relevant?\n"
            "6. If the source reads/writes/increments/resets any state_register (e.g. `self._field = ...`, `.append(...)`, `self._n += 1`) — are those reflected in register_interactions?\n\n"
            "Emit only the ```json block. No prose before or after."
        ),
        "entry_head_purpose": "**phase2 purpose**",
        "entry_head_source": "**source**",
    },
}


def build_prompt(
    unit: TranslationUnit,
    skeleton: dict,
    sibling_synopses: list[tuple[str, str]],
    lang: str = "zh",
) -> str:
    """Compose the LLM prompt for one translation unit."""
    stage = next(
        (s for s in skeleton["stages"] if s["id"] == unit.stage_id), None
    )
    if stage is None:
        raise ValueError(f"stage {unit.stage_id!r} missing from skeleton")

    if lang not in _PROMPT_HEADERS:
        raise ValueError(f"unsupported lang {lang!r}; expected one of {list(_PROMPT_HEADERS)}")
    hdr = _PROMPT_HEADERS[lang]
    frags = _PROMPT_FRAGMENTS_BY_LANG[lang]

    # state_registers — feed all of them so LLM can decide which (if any)
    # this function interacts with. Filter would be wrong: a function in
    # stage-3 might still touch a register documented mostly in stage-4.
    register_lines = []
    for r in skeleton.get("state_registers") or []:
        rid = r.get("id", "")
        sem = (r.get("semantics") or "").replace("\n", " ")[:250]
        register_lines.append(f"  - **{rid}**: {sem}")
    registers_block = (
        "\n".join(register_lines) if register_lines else hdr["registers_empty"]
    )

    # Per-entry source + purpose listing
    entry_blocks = []
    for i, (entry, snip) in enumerate(zip(unit.entries, unit.snippets), 1):
        head = (
            f"### Entry {i} · type={entry.get('type')} "
            f"· line_range={entry.get('line_range')} · sha1={snip.sha1[:8]}"
        )
        purpose = entry.get("purpose") or "(no purpose recorded)"
        entry_blocks.append(
            f"{head}\n\n{hdr['entry_head_purpose']}:\n{purpose}\n\n"
            f"{hdr['entry_head_source']}:\n```python\n{snip.text}\n```"
        )
    entries_text = "\n\n".join(entry_blocks)

    sib_lines = []
    if sibling_synopses:
        for qn, syn in sibling_synopses[-8:]:  # keep prompt size bounded
            sib_lines.append(f"  - {qn}: {syn}")
    sib_block = "\n".join(sib_lines) if sib_lines else hdr["siblings_empty"]

    schema_block = (
        frags["multi_schema"] if unit.type_kind == "multi_region" else frags["single_schema"]
    )

    ctx = get_project_context()
    intro = hdr["intro"].format(project_name=ctx.name)

    return f"""{ctx.block(lang)}

{intro}

{hdr['input']}

{hdr['unit']}
qualname: {unit.qualname}
stage: {unit.stage_id}
type: {unit.type_kind}  ({len(unit.entries)} entry/entries)

{hdr['stage']}
id: {stage['id']}
title: {stage.get('title', '')}
description: {stage.get('description', '')}

{hdr['siblings']}
{sib_block}

{hdr['registers']}
{registers_block}

{hdr['entries']}
{entries_text}

{frags['principles']}

{schema_block}

{hdr['self_check']}
"""


# ─── Cache ────────────────────────────────────────────────────────────────────


def _cache_path(cache_root: Path, stage_id: str, qualname: str, lang: str = "zh") -> Path:
    # Embed qualname into a filesystem-safe path. Language goes into the filename
    # so zh and en translations of the same function coexist without collisions.
    safe_qn = re.sub(r"[^A-Za-z0-9_.]", "_", qualname)
    suffix = "" if lang == "zh" else f".{lang}"
    return cache_root / "translate" / stage_id / f"{safe_qn}{suffix}.json"


def _unit_cache_key(unit: TranslationUnit, lang: str) -> str:
    sha = hashlib.sha1()
    sha.update(TIER3_PROMPT_VERSION.encode())
    # Keep zh keys backward-compatible: lang segment is omitted for the default
    # language so existing caches written before the lang refactor still match.
    # New languages (en, ...) get a distinct keyspace.
    if lang != "zh":
        sha.update(f"|lang={lang}|".encode())
    for snip in unit.snippets:
        sha.update(snip.sha1.encode())
    return sha.hexdigest()[:16]


def load_cached(cache_root: Path, unit: TranslationUnit, lang: str = "zh") -> dict | None:
    p = _cache_path(cache_root, unit.stage_id, unit.qualname, lang)
    if not p.exists():
        return None
    try:
        record = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if record.get("cache_key") != _unit_cache_key(unit, lang):
        return None
    return record.get("translation")


def save_cached(
    cache_root: Path,
    unit: TranslationUnit,
    translation: dict,
    raw_llm_text: str,
    lang: str = "zh",
) -> None:
    p = _cache_path(cache_root, unit.stage_id, unit.qualname, lang)
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "stage_id": unit.stage_id,
        "qualname": unit.qualname,
        "type_kind": unit.type_kind,
        "n_entries": len(unit.entries),
        "lang": lang,
        "cache_key": _unit_cache_key(unit, lang),
        "translation": translation,
        "raw_llm_text": raw_llm_text,
    }
    p.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── Validation ────────────────────────────────────────────────────────────────


def validate_translation(unit: TranslationUnit, t: dict) -> str | None:
    """Return error string or None."""
    if not isinstance(t, dict):
        return "translation must be a dict"
    if t.get("schema_version") != 1:
        return f"schema_version must be 1, got {t.get('schema_version')!r}"

    declared = t.get("type")
    expected = unit.type_kind
    if declared != expected:
        return f"type mismatch: schema says {declared!r}, unit is {expected!r}"

    for key in ("locator_role", "stage_context", "synopsis"):
        if not (isinstance(t.get(key), str) and t[key].strip()):
            return f"missing or empty field: {key}"

    # interface — the explicit I/O contract (params / reads / returns / effects).
    # signature is the minimum; the rest are encouraged but flexible.
    iface = t.get("interface")
    if not isinstance(iface, dict):
        return "missing field: interface (the parameter/IO contract)"
    if not (isinstance(iface.get("signature"), str) and iface["signature"].strip()):
        return "interface.signature missing or empty"

    relations = t.get("relations") or {}
    for cat in ("callers", "core_callees", "config_state_sources", "results_to"):
        v = relations.get(cat)
        if not isinstance(v, list) or not v:
            return f"relations.{cat} must be a non-empty list"

    # register_interactions is optional (a function may not touch any register)
    # but if present, every item must be a dict with at least action + register.
    reg_inter = relations.get("register_interactions")
    if reg_inter is not None:
        if not isinstance(reg_inter, list):
            return "relations.register_interactions must be a list (or omitted)"
        for i, item in enumerate(reg_inter):
            if not isinstance(item, dict):
                return f"register_interactions[{i}] must be a dict"
            action = item.get("action")
            if action not in ("write", "read", "clear", "reset"):
                return (
                    f"register_interactions[{i}].action must be one of "
                    f"write/read/clear/reset, got {action!r}"
                )
            if not isinstance(item.get("register"), str) or not item["register"]:
                return f"register_interactions[{i}].register must be a non-empty string"

    decisions = t.get("design_decisions") or []
    if not isinstance(decisions, list) or not decisions:
        return "design_decisions must contain at least 1 item"

    if expected == "single":
        flow = t.get("execution_flow") or []
        if not isinstance(flow, list) or not flow:
            return "execution_flow must be a non-empty list"
    else:  # multi_region
        regions = t.get("regions") or []
        if len(regions) != len(unit.entries):
            return (
                f"regions count mismatch: schema has {len(regions)}, "
                f"unit has {len(unit.entries)} entries"
            )
        for i, r in enumerate(regions):
            if not isinstance(r.get("gloss"), str) or not r["gloss"].strip():
                return f"regions[{i}].gloss missing"
            if "line_range" not in r:
                return f"regions[{i}].line_range missing"
        struct = t.get("overall_structure") or []
        if len(struct) != len(regions):
            return "overall_structure count must match regions count"

    return None


# ─── Top-level entry ───────────────────────────────────────────────────────────


def translate_unit(
    api: Api,
    unit: TranslationUnit,
    skeleton: dict,
    sibling_synopses: list[tuple[str, str]],
    cache_root: Path,
    *,
    force_refresh: bool = False,
    max_retries: int = 2,
    lang: str = "zh",
) -> dict:
    """Translate one unit. Returns the translation JSON (validated)."""
    if not force_refresh:
        cached = load_cached(cache_root, unit, lang)
        if cached is not None:
            logger.info("cache hit (%s): %s/%s", lang, unit.stage_id, unit.qualname)
            err = validate_translation(unit, cached)
            if err is None:
                return cached
            logger.warning("cached translation invalid (%s); refetching", err)

    prompt = build_prompt(unit, skeleton, sibling_synopses, lang=lang)

    last_err = "no attempt"
    last_raw = ""
    for attempt in range(1, max_retries + 1):
        logger.info(
            "LLM call (%s): %s/%s (attempt %d/%d, %d entries)",
            lang, unit.stage_id, unit.qualname, attempt, max_retries, len(unit.entries),
        )
        result: LLMCallResult = api.call(prompt)
        last_raw = result.raw_text
        translation = result.parsed_json
        if translation is None:
            last_err = "LLM did not return a parseable JSON block"
            logger.warning("attempt %d: %s", attempt, last_err)
            continue
        err = validate_translation(unit, translation)
        if err is None:
            save_cached(cache_root, unit, translation, result.raw_text, lang=lang)
            return translation
        last_err = err
        logger.warning("attempt %d failed validation: %s", attempt, err)

    raise RuntimeError(
        f"translate_unit failed for {unit.qualname} after {max_retries} attempts: {last_err}\n"
        f"raw response (last attempt, first 500 chars):\n{last_raw[:500]}"
    )
