# -*- coding: utf-8 -*-
"""rollup.py — bottom-up LLM summaries (substage / stage / system), cached.

Phase 3 narrates the handbook from the bottom up. The leaf layer (files) is
rendered without an LLM (render_file.py). This module writes the SUMMARIES above
the leaves: each non-leaf stage gets one LLM call that, given its children's
already-written summaries plus its directly-owned files' one-liners, produces a
short Chinese overview of what the stage does and how its parts cooperate. The
root call (`summarize_system`) rolls the top-level stage summaries into a
"what the whole system does" overview for the index.

Every summary is content-hash cached under cache_dir/, so a rerun only re-asks
the LLM for nodes whose inputs changed (the same pattern as
handbook_generate_ml/phase3/narrative.py). All calls go through the api_client
`Api` — no NexAU, no LLM_* endpoint.
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

# Bump when the prompt wording changes so stale caches are invalidated.
_PROMPT_VERSION = "phase3-rollup-v3-plain"


# ─── Prompts (English + Chinese) ─────────────────────────────────────────────


_STAGE_RULES_EN = """You are writing a system handbook for a large codebase, aimed at a curious
NON-EXPERT reader — someone smart but new to this project and its technology, who
should be able to understand it without already knowing the jargon. The handbook
is organized into "stages", and you are writing the OVERVIEW for one stage.

Below you are given this stage's title and description, plus what it contains —
either several SUB-STAGES (each already has its own overview) and/or SOURCE
FILES assigned directly to it (each with a one-line purpose).

Write a **100–200 word** plain-language overview in **English** that explains, in
everyday terms a newcomer can follow:
- what this stage is for and where it fits in the system's story (is it part of
  starting up, the main work loop, shutting down, or shared behind-the-scenes
  support?);
- what each of its parts (sub-stages or files) does and how they work together —
  like parts of a machine — to get this stage's job done.

Requirements:
- Write in plain, accessible language with short clear sentences. Explain any
  unavoidable technical term in plain words the first time you use it. A brief
  everyday analogy is welcome if it helps.
- Be concrete and accurate — say what actually happens; avoid empty phrasing like
  "handles related logic", but also avoid dense jargon dumps.
- Output ONLY the overview prose — no title, no list, no markdown markup, do not
  echo the input back."""


_STAGE_RULES_ZH = """你在为一个大型代码库编写系统手册，读者是一个聪明但**外行**的人——他对这个项目和相关
技术都不熟，应该能在不懂行话的前提下看明白。手册按「阶段」（stage）分层组织，现在要为某一个
阶段写一段**大白话概述**。

下面给你这个阶段的标题、描述，以及它包含的内容——可能是若干**子阶段**（每个已有自己的概述），
也可能是直接归属它的**源文件**（每个给出一句话用途）。

请用**中文大白话**写一段 **100–200 字** 的概述，用外行也能跟上的说法讲清楚：
- 这个阶段是干什么的、在整个系统的「故事」里处于哪一段（是开机启动、主干活儿的循环、收尾关机，
  还是幕后默默支撑大家的公共部分？）；
- 它的几个部件（子阶段或文件）各自干什么、又如何像机器零件一样配合，把这个阶段的活儿干完。

要求：
- 用平实口语的中文，多用短句。碰到绕不开的技术术语，第一次出现就用大白话解释一下；有合适的
  生活类比可以用一个。
- 具体、准确，点出实际做的事，别写「负责处理相关逻辑」这种空话，也别堆砌一大串行话。
- 只输出概述正文，不要标题、不要列表、不要 markdown 标记、不要复述输入。"""


_SYSTEM_RULES_EN = """You are writing the top-level OVERVIEW of a system handbook for a large codebase,
aimed at a curious NON-EXPERT reader — someone smart but new to this project, who
should come away understanding the big picture without already knowing the
jargon. This is the first thing they read, so it should feel welcoming and clear.

Below you are given the system's overall shape (archetype) and the overviews of
ALL its top-level stages (in execution / lifecycle order: from process startup,
through the main work loop, to teardown, ending with cross-cutting
infrastructure).

Write a **200–350 word** plain-language system overview in **English** that
explains, in everyday terms a newcomer can follow:
- what this system actually does and what kind of thing it is (in one plain
  mental picture);
- how it works from start to finish as a story — from starting up, through the
  main work it repeats, to shutting down — threading the key stages together;
- what shared behind-the-scenes support keeps the whole thing running.

Requirements: tell it as one clear story with a beginning, middle, and end.
Use plain, accessible language and short sentences; explain any unavoidable
technical term in plain words, and use a light everyday analogy if it helps. Be
accurate and concrete, no empty filler. Output ONLY the overview prose — no
title, no list."""


_SYSTEM_RULES_ZH = """你在为一个大型代码库编写系统手册的**总览**，读者是一个聪明但**外行**的人——他对这个
项目不熟，应该能读完就抓住大局，而不需要事先懂行话。这是他打开手册看到的第一段话，所以要
亲切、清楚、好懂。

下面给你这个系统的整体形态（archetype），以及它**所有顶层阶段**的概述（按执行/生命周期顺序：
从进程启动，经主工作循环，到收尾，最后是横切的基础设施）。

请用**中文大白话**写一段 **200–350 字** 的系统总览，用外行也能跟上的说法讲清楚：
- 这个系统到底是干什么的、是个什么样的东西（给读者一个简单直观的画面）；
- 它从开机启动、到反复干的主要活儿、再到收尾关机，整条主线像讲故事一样是怎么走下来的
  （把关键阶段串起来）；
- 幕后有哪些公共的支撑部分，在默默保证整个系统跑得起来。

要求：讲成一个有头有尾、脉络清楚的故事。用平实口语的中文、多用短句；绕不开的术语第一次出现
就用大白话解释，合适时用一个轻松的生活类比。要准确、具体，不要空话；只输出总览正文，不要
标题或列表。"""


def _stage_rules(lang: str) -> str:
    return _STAGE_RULES_ZH if lang == "zh" else _STAGE_RULES_EN


def _system_rules(lang: str) -> str:
    return _SYSTEM_RULES_ZH if lang == "zh" else _SYSTEM_RULES_EN



# ─── Cache ───────────────────────────────────────────────────────────────────


def _cache_path(cache_dir: Path, sid: str, key: str) -> Path:
    safe = sid.replace("/", "_")
    return Path(cache_dir) / "rollup" / f"{safe}_{key}.md"


def _cache_key(*parts: str) -> str:
    payload = "".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _read_cache(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else None
    except OSError:
        return None


def _write_cache(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        logger.warning("rollup cache write failed for %s: %s", path, e)


# ─── Prompt building ─────────────────────────────────────────────────────────


def _build_stage_prompt(title: str, description: str,
                        child_summaries: list[tuple[str, str]],
                        file_lines: list[str], lang: str = "en") -> str:
    parts = [_stage_rules(lang), "", f"## Stage title: {title}"]
    if description:
        parts += [f"## Stage description: {description}"]
    if child_summaries:
        parts += ["", "## Sub-stages it contains (with their overviews)"]
        for ctitle, csum in child_summaries:
            parts += [f"### {ctitle}", csum.strip() or "(no overview)"]
    if file_lines:
        parts += ["", "## Source files assigned directly to this stage"]
        parts += file_lines
    tail = ("现在用中文输出本阶段的概述：" if lang == "zh"
            else "Now output this stage's overview in English:")
    parts += ["", tail]
    return "\n".join(parts)


def _build_system_prompt(archetype: str,
                         top_summaries: list[tuple[str, str]],
                         lang: str = "en") -> str:
    parts = [_system_rules(lang), "", f"## System shape: {archetype or '(unknown)'}",
             "", "## Top-level stages (in execution order, with their overviews)"]
    for title, summ in top_summaries:
        parts += [f"### {title}", summ.strip() or "(no overview)"]
    tail = ("现在用中文输出系统总览：" if lang == "zh"
            else "Now output the system overview in English:")
    parts += ["", tail]
    return "\n".join(parts)


# ─── Public API ──────────────────────────────────────────────────────────────


def summarize_stage(api: Api, sid: str, title: str, description: str,
                    child_summaries: list[tuple[str, str]],
                    file_lines: list[str], *, cache_dir: Path,
                    refresh: bool = False, lang: str = "en") -> str:
    """One LLM call → an overview of stage `sid` (lang-controlled), cached.

    child_summaries: [(child_title, child_summary), ...] (already written).
    file_lines: one-liner markdown lines for files directly in this stage.
    Returns the summary text (cache hit or fresh). On LLM failure returns a
    minimal deterministic fallback so the build never blocks.
    """
    prompt = _build_stage_prompt(title, description, child_summaries, file_lines, lang)
    key = _cache_key(_PROMPT_VERSION, lang, "stage", sid, prompt)
    cpath = _cache_path(cache_dir, sid, key)
    if not refresh:
        cached = _read_cache(cpath)
        if cached is not None:
            return cached

    try:
        result = api.call(prompt, params={"temperature": 0.0})
        text = (result.raw_text or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("rollup stage %s LLM failed: %s — using fallback", sid, e)
        text = ""
    if not text:
        # Deterministic fallback: stage description (or title) so the page still
        # has prose and the build is never blocked by one bad call.
        text = description.strip() or title
    _write_cache(cpath, text)
    return text


def summarize_system(api: Api, archetype: str,
                     top_summaries: list[tuple[str, str]], *,
                     cache_dir: Path, refresh: bool = False,
                     lang: str = "en") -> str:
    """One LLM call → the system overview for index.md, content-hash cached."""
    prompt = _build_system_prompt(archetype, top_summaries, lang)
    key = _cache_key(_PROMPT_VERSION, lang, "system", archetype, prompt)
    cpath = _cache_path(cache_dir, "_system", key)
    if not refresh:
        cached = _read_cache(cpath)
        if cached is not None:
            return cached
    try:
        result = api.call(prompt, params={"temperature": 0.0})
        text = (result.raw_text or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("rollup system LLM failed: %s — using fallback", e)
        text = ""
    if not text:
        text = archetype or "(system overview generation failed.)"
    _write_cache(cpath, text)
    return text
