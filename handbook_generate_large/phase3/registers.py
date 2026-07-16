# -*- coding: utf-8 -*-
"""registers.py — extract state registers + render the "状态流动" appendix.

A *state register* is a global / shared piece of state that flows ACROSS stages:
configuration, auth/credentials, the live session, the tool catalog, sandbox
policy, persistence handles, connection pools, and so on. The Phase 2 skeleton
synthesis never populated `state_registers` (it is hardcoded empty), so Phase 3
recovers them here — one LLM call over the top-level stage summaries plus the
data_model files — and the SAME call also maps each register to the stage ids it
touches, so stage pages can be annotated without a second pass.

Output is consumed by build_handbook:
  - `render_register_table` → the index.md "🔄 状态流动总览" table.
  - `render_stage_registers` → a per-stage "📊 本阶段涉及的状态" section.

LLM via the api_client `Api`; content-hash cached like rollup.py.
"""
from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "shared"))

from api_client import Api  # noqa: E402

logger = logging.getLogger(__name__)

_PROMPT_VERSION = "phase3-registers-v3-plain"


_RULES_EN = """You are writing the system handbook for a large codebase. Identify this
system's **state registers** — pieces of **global / shared state that flow ACROSS
multiple stages and are read/written repeatedly**.

What counts as a register (examples for style only — judge by the actual content):
- the config stack (merged effective configuration), feature flags;
- auth state / credentials, secret stores;
- the live session object, thread identity & history, rollout/persistence handles;
- the tool catalog, plugin set, MCP connections, model provider/catalog;
- sandbox policy, exec policy, execution environment, network proxy state;
- UI state, server processor state, observability/telemetry, background job queues.

Below you are given the overviews of **all top-level stages** plus a set of
**data_model files** (role=data_model) and their purposes. Use these to identify
the registers that actually exist in this system.

Requirements:
- Give each register a stable id (form `reg-xxx`, lowercase-hyphen), a one-line
  **plain-language** meaning in **English** that a non-expert can understand
  (what this shared piece of state is, in everyday terms), and **which stages it
  touches** (use the stage ids given below; only real ids that actually appear).
- Identify ONLY genuinely cross-stage global state — do not treat local
  variables or single-file internal state as registers.
- Let the count reflect the system's real scale (a large agent runtime usually
  has 20–35).

Output ONLY one JSON block:
```json
{
  "registers": [
    {"id": "reg-xxx", "semantics": "one-line semantics", "stages": ["stage-5", "stage-9"]}
  ]
}
```"""


_RULES_ZH = """你在为一个大型代码库编写系统手册。识别这个系统的**状态寄存器**（state
register）——即**跨多个阶段流动、被反复读写的全局/共享状态**。

什么算 register（举例仅示意风格，请按实际内容判断）：
- 配置栈（合并后的有效配置）、特性开关；
- 认证状态/凭据、密钥存储；
- 活跃会话对象、线程身份与历史、rollout/持久化句柄；
- 工具目录、插件集、MCP 连接、模型提供方/目录；
- 沙箱策略、执行策略、执行环境、网络代理状态；
- UI 状态、服务端处理器状态、可观测性/遥测、后台任务队列。

下面给你**所有顶层阶段**的概述，以及一批 **data_model 文件**（role=data_model）及其用途。
据此识别系统中真实存在的 register。

要求：
- 每个 register 给一个稳定 id（形如 `reg-xxx`，小写中划线，**id 保持英文**），一句**中文大白话**
  语义（用外行也懂的说法，说清这份共享状态到底是什么），以及它**涉及哪些阶段**（用下面给出的
  stage id，只能用真实出现的 id）。
- 只识别**真正跨阶段的全局状态**——不要把局部变量、单文件内部状态当 register。
- 数量反映系统真实规模（大型 agent runtime 通常 20–35 个）。

只输出一个 JSON 块（**JSON 的 key 和 id 用英文，semantics 用中文**）：
```json
{
  "registers": [
    {"id": "reg-xxx", "semantics": "一句话中文语义", "stages": ["stage-5", "stage-9"]}
  ]
}
```"""


_GAP_RULES_EN = """You are completing the **state register** list (global state that flows across
stages) for the same system.

Earlier rounds already identified the registers listed below (id + semantics).
Find ONLY the ones still MISSING — cross-stage global state that genuinely exists
but is NOT in the list below. Notes:
- Do NOT repeat already-listed registers (don't rename a same-semantics one and
  report it again);
- Focus on easily-overlooked ones: background jobs/queues, caches, connection
  pools, rate limits, token budgets, goal/memory and other extension state,
  telemetry/feedback buffers, update-check state, etc.;
- If nothing is missing, return an empty registers array.

Output a JSON block, same schema as before (put ONLY the **new** registers):
```json
{"registers": [{"id": "reg-xxx", "semantics": "one-line semantics", "stages": ["stage-N"]}]}
```"""


_GAP_RULES_ZH = """你在为同一个系统补全**状态寄存器**（跨阶段流动的全局状态）清单。

前几轮已识别下面这些 register（id + 语义）。只找出**仍遗漏的**——真实存在但不在下面清单里的
跨阶段全局状态。注意：
- 不要重复已列出的 register（不要把语义相同的换个名再报一遍）；
- 重点查容易被忽略的：后台任务/队列、缓存、连接池、速率限制、令牌预算、目标/记忆等扩展态、
  遥测/反馈缓冲、更新检查状态等；
- 如果没有遗漏，返回空的 registers 数组。

输出一个 JSON 块，schema 与之前相同（只放**新增**的 register；**id 用英文，semantics 用中文**）：
```json
{"registers": [{"id": "reg-xxx", "semantics": "一句话中文语义", "stages": ["stage-N"]}]}
```"""


def _rules(lang: str) -> str:
    return _RULES_ZH if lang == "zh" else _RULES_EN


def _gap_rules(lang: str) -> str:
    return _GAP_RULES_ZH if lang == "zh" else _GAP_RULES_EN


# ─── Cache ───────────────────────────────────────────────────────────────────


def _cache_key(*parts: str) -> str:
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()[:12]


def _cache_path(cache_dir: Path, key: str) -> Path:
    return Path(cache_dir) / "registers" / f"registers_{key}.json"


# ─── Extraction ──────────────────────────────────────────────────────────────


def extract_registers(api: Api,
                      top_summaries: list[tuple[str, str, str]],
                      data_model_files: list[tuple[str, str]],
                      valid_stage_ids: set[str], *,
                      cache_dir: Path, refresh: bool = False,
                      data_model_cap: int = 120,
                      max_rounds: int = 5, dry_streak: int = 2,
                      lang: str = "en") -> list[dict]:
    """Extract the system's state registers via LOOP-UNTIL-DRY, content-hash
    cached as one unit.

    Round 1 lists registers from the stage summaries + data_model files. Each
    later round shows the LLM everything found SO FAR and asks only for what was
    MISSED, accumulating new ones. The loop stops after `dry_streak` consecutive
    rounds that add nothing (a single dry round can be noise — the model just
    didn't think of more that turn), or after `max_rounds`. A simple one-shot
    call has high miss rate on a big system; the dry-streak tail catches the
    registers the first pass forgot.

    top_summaries: [(stage_id, title, summary), ...] for ALL top-level stages.
    data_model_files: [(path, purpose), ...] (role=data_model), capped.
    valid_stage_ids: every real stage id; a register's `stages` is filtered to
      this set so hallucinated ids are dropped.

    Returns [{id, semantics, stages}], ids unique, stages real. On total failure
    returns [] so the build never blocks.
    """
    import json

    stage_block = "\n".join(
        f"- {sid} · {title}：{(summ or '').strip()}"
        for sid, title, summ in top_summaries)
    dm = data_model_files[:data_model_cap]
    dm_block = "\n".join(f"- `{p}`：{(purpose or '').strip()}" for p, purpose in dm)
    dm_note = ("" if len(data_model_files) <= data_model_cap
               else f"\n(another {len(data_model_files) - data_model_cap} data_model files not listed)")
    evidence = "\n".join([
        "## Top-level stages (with overviews)", stage_block, "",
        f"## data_model files (total {len(data_model_files)}, excerpt)", dm_block + dm_note,
    ])

    # Cache key covers the whole loop config (evidence + version + loop params),
    # so the multi-round result is cached/reused as a single unit.
    key = _cache_key(_PROMPT_VERSION, lang, evidence, f"r{max_rounds}s{dry_streak}")
    cpath = _cache_path(cache_dir, key)
    if not refresh and cpath.exists():
        try:
            return json.loads(cpath.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    def _ask(prompt: str) -> list[dict]:
        try:
            result = api.call(prompt, params={"temperature": 0.0})
            return _normalize_registers(result.parsed_json, valid_stage_ids)
        except Exception as e:  # noqa: BLE001
            logger.warning("register LLM call failed: %s", e)
            return []

    # Round 1 — initial extraction.
    found: list[dict] = []
    seen_ids: set[str] = set()
    tail = ("现在输出 registers JSON 块：" if lang == "zh"
            else "Now output the registers JSON block:")
    first = _ask("\n".join([_rules(lang), "", evidence, "", tail]))
    for r in first:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            found.append(r)
    logger.info("registers: round 1 found %d", len(found))

    # Loop-until-dry — show what's found, ask for the misses.
    no_new = 0
    for rnd in range(2, max_rounds + 1):
        if no_new >= dry_streak:
            break
        known = "\n".join(f"- `{r['id']}`：{r['semantics']}" for r in found)
        gap_tail = ("现在只输出**新增**的 registers JSON 块：" if lang == "zh"
                    else "Now output ONLY the NEW registers as a JSON block:")
        gap_prompt = "\n".join([
            _gap_rules(lang), "", evidence, "",
            "## Already-identified registers (do NOT repeat these)", known, "",
            gap_tail])
        new = _ask(gap_prompt)
        added = 0
        for r in new:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                found.append(r)
                added += 1
        logger.info("registers: round %d added %d (total %d)", rnd, added, len(found))
        no_new = no_new + 1 if added == 0 else 0

    try:
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text(json.dumps(found, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        logger.warning("register cache write failed: %s", e)
    logger.info("registers: extracted %d register(s) over loop-until-dry", len(found))
    return found


def _normalize_registers(parsed, valid_stage_ids: set[str]) -> list[dict]:
    """Validate the LLM output: dict shape, unique ids, real stage ids only."""
    if not isinstance(parsed, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for r in parsed.get("registers", []) or []:
        if not isinstance(r, dict):
            continue
        rid = (r.get("id") or "").strip()
        semantics = (r.get("semantics") or "").strip()
        if not rid or not semantics or rid in seen:
            continue
        seen.add(rid)
        stages = [s for s in (r.get("stages") or [])
                  if isinstance(s, str) and s in valid_stage_ids]
        out.append({"id": rid, "semantics": semantics, "stages": stages})
    return out


# ─── Rendering ───────────────────────────────────────────────────────────────


def render_register_table(registers: list[dict],
                          title_of=None,
                          lang: str = "en") -> str:
    """The register overview table. `title_of(sid)` (optional) maps a stage id
    to its title for nicer links; falls back to the bare id."""
    zh = lang == "zh"
    heading = "## 🔄 状态流动总览" if zh else "## 🔄 State Flow Overview"
    if not registers:
        empty = "_(没有提取到状态寄存器。)_" if zh else "_(No state registers extracted.)_"
        return f"{heading}\n\n{empty}\n"
    header = ("| 状态寄存器 | 含义 | 涉及阶段 |" if zh
              else "| State register | Semantics | Stages touched |")
    lines = [heading, "", header, "| --- | --- | --- |"]
    for r in registers:
        stage_links = []
        for sid in r.get("stages", []):
            label = title_of(sid) if title_of else sid
            stage_links.append(f"[{label}]({sid}.md)")
        stages_cell = ", ".join(stage_links) if stage_links else "—"
        sem = r["semantics"].replace("|", "\\|")
        lines.append(f"| `{r['id']}` | {sem} | {stages_cell} |")
    return "\n".join(lines) + "\n"


_STAGE_SECTION_MARKER = "## 📊 State Registers Touched"
_STAGE_SECTION_MARKER_ZH = "## 📊 本阶段涉及的状态"


def render_stage_registers(sid: str, registers: list[dict], lang: str = "en") -> str:
    """The per-stage register section, or '' if this stage touches no register.
    Marker line is stable (per lang) so the appender stays idempotent."""
    hits = [r for r in registers if sid in r.get("stages", [])]
    if not hits:
        return ""
    lines = [stage_section_marker(lang), ""]
    for r in hits:
        lines.append(f"- `{r['id']}` — {r['semantics']}")
    return "\n".join(lines) + "\n"


def stage_section_marker(lang: str = "en") -> str:
    """Exposed so build_handbook can check idempotency before appending."""
    return _STAGE_SECTION_MARKER_ZH if lang == "zh" else _STAGE_SECTION_MARKER
