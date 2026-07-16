# -*- coding: utf-8 -*-
"""render_agent.py — the AGENT arm of Phase 3 (deterministic, NO LLM in layer 1).

The human arm (build_handbook) writes flowing prose for a person reading top to
bottom. A code agent reads differently: it already has the code, it greps in to a
concept, lands on one block, and reads only that block. So this arm emits a
**fixed-schema locator index** — every stage rendered as the SAME field set, each
field gated on a structural signal, every fact anchored (stage-id / path:line /
reg-id) so the agent can jump and verify.

Design contract (see the handbook's own HOW-TO-USE header, emitted by
`how_to_use_md`):

  * Layer 1 (this file, zero LLM) derives everything from STRUCTURE only:
      - 共变·强 (strong co-change): a file's same-directory `<stem>_tests.rs`
        twin — mechanically certain, so it gets the ⚠️ "change one → change the
        test" mark. NEVER matched by bare basename (that pulls in every `mod.rs`
        in the repo); ALWAYS path-scoped to the same directory.
      - 共变·弱 (weak co-change): the stage's organization sub-groups — a
        *topical* grouping, NOT a guarantee, so it is folded to name+count+pointer
        and never claims "must change together".
      - 范本 (exemplar): the file in a sub-group with the most functions — the
        most complete idiomatic implementation to copy when adding a new X.
      - 消歧 (disambiguation): sibling stages (same parent) whose titles share a
        keyword — the stages an agent grepping the same concept will collide with.
        Layer-1 shows their titles; layer 2 (predisambiguation.json) refines the
        one-liner. Gated: a stage with no colliding sibling emits NO block.
      - 状态 (state): the registers (reg-*) this stage touches, from the already
        extracted register list — no new LLM call.

  * data-gating is a hard invariant: a field is empty IFF its structural signal is
    truly absent. The HOW-TO-USE header tells the agent "empty 消歧 = name doesn't
    collide" — so an accidental omission would actively mislead. Render strictly.

`render_agent_site(tree, summaries, registers, out_dir, lang)` writes
`<out_dir>/agent/{index.md, how_to_use.md, <stage>.md ...}`. It reuses
render_file.render_file_md for the per-file leaf cards (anchors + signatures +
call graph already there) and only adds the locator header on top.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import render_file as rf

logger = logging.getLogger(__name__)

# Path-component / filename tokens too generic to be a useful concept or
# collision signal (they appear in nearly every crate). Used ONLY to filter the
# *display* of entry concepts and the title-keyword disambiguation — never to
# drop a structural fact. Kept deliberately small; document-frequency does the
# rest (a token in one stage can't collide; see _title_tokens).
_GENERIC_TOKENS = {
    "mod", "lib", "main", "src", "rs", "errors", "error", "tests", "test",
    "common", "types", "type", "util", "utils", "helpers", "helper", "win",
    "unix", "core", "the", "and", "for", "of", "to", "a", "an", "with",
    # title-level generic words (themes, not concepts an agent disambiguates on):
    "support", "services", "service", "startup", "runtime", "shared", "cross",
    "cutting", "local", "data", "user", "system", "management", "handling",
    "primitives", "miscellaneous", "small", "libraries", "library", "support",
}

# A title keyword counts as a same-name COLLISION only when it appears in at most
# this many stages. Above it the word is a system-wide theme ("persistence",
# "context") that points everywhere → no disambiguation value. Measured from the
# real title-token frequency distribution: distinctive collisions (rollout=5,
# process=4) sit at/below 6; themes (persistence/state/config) sit at 7–17.
_MAX_COLLISION_DF = 6

# Role display order for picking the most representative "core files" of a stage.
_ROLE_PRIORITY = [
    "entrypoint", "orchestration", "domain_logic", "data_model", "io",
    "adapter", "config", "util", "test",
]


# ── structural index helpers (built once per build) ──────────────────────────


def _stem(path: str) -> str:
    """Filename without directory or `.rs` extension."""
    base = path.rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".rs") else base


def _dirof(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def build_file_stage_index(tree) -> dict[str, str]:
    """Global file-path → owning stage-id map (every file, all stages).

    The strong co-change twin lookup needs the WHOLE repo's file set — a file's
    test twin almost always lives in a *different* (test) stage, so a per-stage
    view would miss it.
    """
    idx: dict[str, str] = {}
    for sid, files in tree.buckets.items():
        for f in files:
            idx[f] = sid
    return idx


def strong_twins(rel: str, file_stage: dict[str, str]) -> list[tuple[str, str]]:
    """Same-directory test twins of `rel`: `<dir>/<stem>_tests.rs` /
    `_test.rs`. Returns [(twin_path, twin_stage_id)], path-scoped so a hot
    basename never drags in unrelated files. Empty when there is no twin."""
    d, s = _dirof(rel), _stem(rel)
    out: list[tuple[str, str]] = []
    for cand_stem in (f"{s}_tests", f"{s}_test"):
        cand = f"{d}/{cand_stem}.rs" if d else f"{cand_stem}.rs"
        if cand in file_stage:
            out.append((cand, file_stage[cand]))
    return out


def _title_tokens(title: str) -> set[str]:
    """Distinctive lowercase word tokens of a stage title (generic words
    dropped). Used to detect which stages collide on concept."""
    words = re.split(r"[^a-zA-Z0-9]+", (title or "").lower())
    return {w for w in words if w and w not in _GENERIC_TOKENS and len(w) > 2}


def build_title_token_index(tree) -> dict[str, list[str]]:
    """token → [stage ids whose title contains it], over ALL stages. Built once;
    its document-frequency gates which tokens are real collisions vs themes."""
    idx: dict[str, list[str]] = {}
    for sid in tree.order:
        for w in _title_tokens(tree.title(sid)):
            idx.setdefault(w, []).append(sid)
    return idx


def _ancestors(tree, sid: str) -> set[str]:
    out: set[str] = set()
    cur = (tree.stages_by_id.get(sid, {}) or {}).get("parent")
    while cur:
        out.add(cur)
        cur = (tree.stages_by_id.get(cur, {}) or {}).get("parent")
    return out


def _descendants(tree, sid: str) -> set[str]:
    out: set[str] = set()

    def walk(s: str) -> None:
        for c in tree.children(s):
            out.add(c)
            walk(c)
    walk(sid)
    return out


def _is_ancestor_chain(tree, ids: list[str]) -> bool:
    """True if the colliding stages all lie on a single ancestor chain (one of
    them is an ancestor of all the others). Such a 'collision' is just parent →
    child nesting — the agent reaches the child through the parent, so it is NOT
    a same-name confusion and must not be emitted."""
    s = set(ids)
    for a in ids:
        if (s - {a}) <= _descendants(tree, a):
            return True
    return False


def build_collision_index(tree, token_index: dict[str, list[str]]
                          ) -> list[tuple[str, list[str]]]:
    """The disambiguation payload, organized BY WORD (an agent searches a word,
    not a stage). Returns [(word, [stage ids])] for every word that is a genuine
    same-name collision:
      - document frequency in [2, _MAX_COLLISION_DF] — appears in a few stages
        (rollout=5 in), not a system-wide theme (persistence=17 out);
      - not a pure ancestor chain (parent/child nesting is not confusion).
    Sorted rarest-first (a 2-stage collision is the sharpest signal). This is the
    structural candidate set layer 2's LLM refines into per-stage one-liners.
    """
    out: list[tuple[str, list[str]]] = []
    for word, stages in token_index.items():
        if not (2 <= len(stages) <= _MAX_COLLISION_DF):
            continue
        if _is_ancestor_chain(tree, stages):
            continue
        ordered = sorted(stages, key=lambda s: tree.order.index(s)
                         if s in tree.order else 1e9)
        out.append((word, ordered))
    out.sort(key=lambda ws: (len(ws[1]), ws[0]))     # rarest first, then alpha
    return out


def collisions_for_stage(sid: str,
                         collisions: list[tuple[str, list[str]]]
                         ) -> list[tuple[str, list[str]]]:
    """The collision words that involve `sid` (for a back-link on its stage
    page). Empty → that stage's name does not collide."""
    return [(w, st) for w, st in collisions if sid in st]


def _group_files(group: dict) -> list[dict]:
    """Normalize an organization sub-group's file entries to dicts with at least
    {file, n_functions}."""
    out: list[dict] = []
    for f in group.get("files", []) or []:
        if isinstance(f, dict) and f.get("file"):
            out.append({"file": f["file"], "n_functions": f.get("n_functions") or 0,
                        "purpose": f.get("purpose") or "", "role": f.get("role") or ""})
        elif isinstance(f, str):
            out.append({"file": f, "n_functions": 0, "purpose": "", "role": ""})
    return out


def group_exemplar(group: dict, cards: dict) -> dict | None:
    """The most representative file of a sub-group = the one with the most
    functions (the fullest idiomatic implementation to copy). Falls back to the
    card's function count when the organization entry lacks n_functions. None for
    an empty group."""
    files = _group_files(group)
    if not files:
        return None

    def nfn(entry: dict) -> int:
        if entry["n_functions"]:
            return entry["n_functions"]
        card = cards.get(entry["file"]) or {}
        return len(card.get("functions") or [])

    best = max(files, key=nfn)
    if nfn(best) <= 0:
        return None                                  # nothing function-bearing
    return {"file": best["file"], "n_functions": nfn(best)}


def entry_concepts(tree, sid: str, cap: int = 8) -> list[str]:
    """The distinctive file/dir stems an agent would grep to land here. Generic
    stems dropped; de-duplicated preserving organization order."""
    seen: set[str] = set()
    out: list[str] = []
    for rel in tree.direct_files(sid):
        s = _stem(rel)
        if s in _GENERIC_TOKENS or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def core_files(tree, sid: str, cap: int = 6) -> list[tuple[str, str, int]]:
    """Top representative files of a stage as (path, role, n_functions), ranked
    by role priority then function count."""
    scored: list[tuple[int, int, str, str, int]] = []
    for rel in tree.direct_files(sid):
        card = tree.cards.get(rel) or {}
        role = card.get("role") or "?"
        nfn = len(card.get("functions") or [])
        try:
            rp = _ROLE_PRIORITY.index(role)
        except ValueError:
            rp = len(_ROLE_PRIORITY)
        scored.append((rp, -nfn, rel, role, nfn))
    scored.sort()
    return [(rel, role, nfn) for _, _, rel, role, nfn in scored[:cap]]


def registers_of(sid: str, registers: list[dict]) -> list[dict]:
    """The registers whose `stages` include this stage exactly. Empty → no direct
    register hit. (Kept for the human arm / direct lookups.)"""
    return [r for r in registers if sid in (r.get("stages") or [])]


# Register-id word tokens too generic to anchor a sink (they describe the SHAPE
# of state, not the concept). reg-unified-PROCESS-registry → keep {unified,
# process}; drop {reg, registry, state}.
_REG_STOP = {
    "reg", "state", "catalog", "store", "context", "policy", "registry",
    "config", "runtime", "data", "id", "identity", "set", "cache", "buffer",
    "profile", "info", "metadata", "snapshot", "window", "queue",
}


def _register_words(rid: str) -> set[str]:
    """Distinctive concept words of a register id (shape words dropped).
    Lowercased so matching against concept words is case-insensitive and the
    stopword filter (lowercase) applies regardless of the id's casing."""
    return {w for w in rid.lower().replace("reg-", "").split("-")
            if w not in _REG_STOP and len(w) > 2}


def _concept_subwords(tree, sid: str) -> set[str]:
    """Leaf stage's concept vocabulary: entry-concept stems + title tokens, each
    further split on `_` so `process_manager` yields {process, manager}."""
    ws: set[str] = set(entry_concepts(tree, sid, cap=20)) | _title_tokens(tree.title(sid))
    out: set[str] = set()
    for w in ws:
        for part in re.split(r"[_]", w):
            if len(part) > 2:
                out.add(part.lower())
    return out


def stage_registers(sid: str, registers: list[dict], tree) -> list[dict]:
    """The 状态 field for a stage, as a two-tier list. Each item is the register
    dict plus a `sink` flag and (for sinks) the `via` keyword that matched:

      - DIRECT  (sink=False): the register's own `stages` lists this stage or an
        ancestor — the extraction already placed it here.
      - SUNK    (sink=True):  a register placed on an ANCESTOR top-level stage
        whose id-word overlaps this leaf's concept vocabulary
        (reg-unified-*process*-registry ↔ leaf concept `process`). This is the
        zero-LLM "subtraction base": instead of flooding every leaf under
        stage-14 with all of stage-14's registers, only the leaves whose concept
        words actually match are kept. Structural + citeable (the agent sees the
        matched word); layer 2's LLM later prunes the residue.

    Deduped: a register that hits directly is never also listed as a sink.
    """
    direct_ids: set[str] = set()
    out: list[dict] = []
    # DIRECT: the extraction literally placed this register on THIS stage id.
    for r in registers:
        if sid in set(r.get("stages") or []):
            direct_ids.add(r["id"])
            out.append({**r, "sink": False, "via": None})
    # SUNK (leaves only): a register anchored on an ANCESTOR top-level stage,
    # kept ONLY where a register-id concept word matches this leaf's concept
    # vocabulary. This is the subtraction: stage-14's 30 registers do NOT all
    # flood its 17 leaves — only the concept-matching ones land on each leaf.
    if not tree.children(sid):
        ancestors = _ancestors(tree, sid)
        my_words = _concept_subwords(tree, sid)
        for r in registers:
            if r["id"] in direct_ids:
                continue
            if not (set(r.get("stages") or []) & ancestors):
                continue                             # not in this leaf's lineage
            hit = _register_words(r["id"]) & my_words
            if hit:
                out.append({**r, "sink": True, "via": sorted(hit)[0]})
    return out


# ── bilingual chrome ──────────────────────────────────────────────────────────

_UI = {
    "en": {
        "duty": "Duty", "concepts": "Entry concepts", "state": "State",
        "reg_sunk": " (inherited, via ", "reg_sunk_end": ")",
        "disambig_link": "⚠️ Name collides — searching these words also lands "
                         "elsewhere; see [disambiguation.md](disambiguation.md)",
        "files": "Core files", "exemplar": "Exemplar",
        "exemplar_hint": "copy this when adding a new one",
        "cochange_strong": "⚠️ Strong co-change (change src → change its test)",
        "cochange_weak": "Related (same sub-group — topical, verify before editing)",
        "files_n": "files", "fns": "fns",
    },
    "zh": {
        "duty": "职责", "concepts": "入口概念", "state": "状态",
        "reg_sunk": "（继承自父级，按概念词 ", "reg_sunk_end": "）",
        "disambig_link": "⚠️ 名字撞车 — 搜这些词还会落到别处；见 "
                         "[disambiguation.md](disambiguation.md)",
        "files": "核心文件", "exemplar": "范本",
        "exemplar_hint": "加新的照这个抄",
        "cochange_strong": "⚠️ 强共变（改 src 必改其 test）",
        "cochange_weak": "相关（同 sub-group，主题归类，改前先核实）",
        "files_n": "个文件", "fns": "函数",
    },
}


# ── stage block (shared by index entry and stage page header) ─────────────────


# The duty line is the opening of the rollup summary — the "is this the stage I
# want" text for coarse filtering. Taking only the FIRST SENTENCE proved too thin:
# many summaries open with a positioning sentence ("this stage is the X-facing
# bridge between A and B") and put the ACTUAL responsibilities in the 2nd/3rd
# sentence ("app-server/TUI/core all converge here..."), so first-sentence-only
# dropped exactly the info an agent needs to judge "is the thing I'm changing
# here". So we take the WHOLE first paragraph of the summary (no length cap) — it
# is the positioning sentence plus the responsibility sentences, and stops at the
# first blank line so later detail paragraphs are not pulled in.


def _duty_line(text: str) -> str:
    """The full first paragraph of `text` (up to the first blank line), newlines
    flattened to spaces. No truncation — the whole paragraph is the duty."""
    return (text or "").strip().split("\n\n")[0].replace("\n", " ").strip()


def stage_locator_block(tree, sid: str, summary: str, registers: list[dict],
                        file_stage: dict[str, str],
                        collisions: list[tuple[str, list[str]]], *,
                        lang: str = "en",
                        heading_level: int = 2,
                        link_heading: bool = False) -> str:
    """The fixed-schema locator block for one stage. Every field is gated on a
    structural signal; an absent signal omits the line entirely (data-gating).

    collisions: the global by-word collision index; the block emits a back-link
    to disambiguation.md only when THIS stage's name participates in a collision.
    link_heading: in the index, make the heading link to the stage page so an
    agent can jump in; on the stage page itself a self-link is pointless.
    """
    ui = _UI.get(lang, _UI["en"])
    h = "#" * max(1, min(heading_level, 6))
    title = tree.title(sid)
    head_text = f"[{sid} · {title}]({sid}.md)" if link_heading else f"{sid} · {title}"
    lines: list[str] = [f"{h} {head_text}", ""]

    # 职责 — the summary's full first paragraph (positioning + responsibilities).
    duty = _duty_line(summary or tree.description(sid) or "")
    if duty:
        lines.append(f"**{ui['duty']}**: {duty}")

    # 入口概念 — grep words.
    concepts = entry_concepts(tree, sid)
    if concepts:
        lines.append(f"**{ui['concepts']}**: " + " / ".join(f"`{c}`" for c in concepts))

    # 状态 — registers touched, two tiers: DIRECT (extraction placed it here)
    # and SUNK (inherited from an ancestor, kept only where a concept word
    # matches — marked so the agent knows it's a structural guess to verify).
    regs = stage_registers(sid, registers, tree)
    if regs:
        direct = [r for r in regs if not r["sink"]]
        sunk = [r for r in regs if r["sink"]]
        bits = [f"`{r['id']}`" for r in direct]
        bits += [f"`{r['id']}`{ui['reg_sunk']}{r['via']}{ui['reg_sunk_end']}"
                 for r in sunk]
        lines.append(f"**{ui['state']}**: " + ", ".join(bits))

    # ⚠️ 消歧 — only a back-link, and only when this stage's name collides.
    # The full by-word table lives in disambiguation.md (an agent searches a
    # word, not a stage, so the payload is organized there by word).
    my_cols = collisions_for_stage(sid, collisions)
    if my_cols:
        words = ", ".join(f"`{w}`" for w, _ in my_cols)
        lines += ["", f"**{ui['disambig_link']}** ({words})"]

    # 范本 — per sub-group exemplar.
    groups = tree.groups(sid)
    exemplars = []
    for g in groups:
        ex = group_exemplar(g, tree.cards)
        if ex:
            exemplars.append((g.get("title") or "", ex))
    if exemplars:
        lines += ["", f"**{ui['exemplar']}** ({ui['exemplar_hint']}):"]
        for gtitle, ex in exemplars:
            gt = f" [{gtitle}]" if gtitle else ""
            lines.append(f"- `{ex['file']}`{gt} ({ex['n_functions']} {ui['fns']})")

    # 共变·强 — same-dir test twins (mechanically certain).
    strong: list[str] = []
    for rel in tree.direct_files(sid):
        for twin, tstage in strong_twins(rel, file_stage):
            strong.append(f"- `{rel}` ↔ `{twin}`  [{tstage}]")
    if strong:
        lines += ["", f"**{ui['cochange_strong']}**:"] + strong

    # 共变·弱 — sub-groups folded (name + count + the stage page as pointer).
    if groups:
        lines += ["", f"**{ui['cochange_weak']}**:"]
        for g in groups:
            gfiles = _group_files(g)
            if not gfiles:
                continue
            lines.append(f"- {g.get('title') or '(group)'} "
                         f"({len(gfiles)} {ui['files_n']})")

    # 核心文件 — top representative files.
    cores = core_files(tree, sid)
    if cores:
        lines += ["", f"**{ui['files']}**:"]
        for rel, role, nfn in cores:
            lines.append(f"- `{rel}`  `{role}` ({nfn} {ui['fns']})")

    return "\n".join(lines).rstrip() + "\n"


# ── HOW-TO-USE header (operating protocol for the agent) ──────────────────────

_HOW_TO_USE = {
    "en": """# How a code agent should use this handbook

## What it is / isn't
- IS: a **locator index** — where a concept lives, what else changes when you
  change it, which similar names not to confuse.
- IS NOT: a replacement for the code. Every fact is anchored
  (`stage-id` / `path:line` / `reg-id`). **Jump there and Read the real file —
  the handbook can be stale; the code is the only source of truth.**

## How to look things up
- "where do I change/add X" → search **Entry concepts** in index → land on a
  stage → copy its **Exemplar**.
- "what else must change with A" → that file's **Strong co-change** (must change
  the test) + **Related** (same sub-group, verify).
- "I searched X and got many hits, which is right" → open
  [disambiguation.md](disambiguation.md) and look up the word.
- "is this a state change I might under-apply" → the **State** field's `reg-*` →
  open register.md for that register's full read/write set.

## An empty field is information
- a stage with NO disambiguation back-link = its name does not collide; search
  freely.
- empty **Strong co-change** = changing this does not drag a test along.
- Do not read an empty field as "not written yet" — it means the structural
  signal is genuinely absent.

## Trust boundary
- Anchors (`path:line`, `reg-id`, `stage-id`) = deterministic structure → trust,
  jump directly.
- Prose (duty / disambiguation wording) = summary → use to pick a direction, not
  as ground truth.
- Any decision that edits code defers to the real code you Read.
""",
    "zh": """# code agent 该怎么用这本 handbook

## 它是什么 / 不是什么
- 是：**定位索引** —— 概念在哪个 stage、改这里还要动哪里、哪些相似的名字别搞混。
- 不是：代码的替代品。所有事实都带锚点（`stage-id` / `path:line` / `reg-id`）。
  **跳过去 Read 真文件核实 —— handbook 可能过时，代码是唯一真相。**

## 典型任务怎么查
- 「我要改/加 X，从哪下手」→ 在 index 搜 **入口概念** → 落到 stage → 看 **范本** 照着抄。
- 「改了 A 还要改哪」→ 该文件的 **强共变**（必改 test）+ **相关**（同 sub-group，需核实）。
- 「搜 X 搜出一堆，哪个对」→ 打开 [disambiguation.md](disambiguation.md) 按词查。
- 「这是状态类改动，会漏吗」→ **状态** 字段的 `reg-*` → 去 register.md 查该 register 的读写全集。

## 字段的空也是信息
- 某 stage 没有消歧回链 = 这名字不撞车，放心搜。
- **强共变** 为空 = 改这里不连带改 test。
- 别把空字段当「没写全」—— 它表示结构上确实没有这个信号。

## 信任边界
- 锚点（`path:line` / `reg-id` / `stage-id`）= 确定性结构 → 可信，直接跳。
- 散文（职责 / 消歧措辞）= 摘要 → 用来判断方向，不作为事实依据。
- 任何要改代码的决定，以 Read 到的真实代码为准。
""",
}


def how_to_use_md(lang: str = "en") -> str:
    return _HOW_TO_USE.get(lang, _HOW_TO_USE["en"])


# ── stage page (locator header + reused file cards) ───────────────────────────


def stage_page_md(tree, sid: str, summary: str, registers: list[dict],
                  file_stage: dict[str, str],
                  collisions: list[tuple[str, list[str]]], *,
                  lang: str = "en") -> str:
    """Full agent stage page: the locator block (H1) then every owned file's
    deep card (reusing render_file_md — anchors/signatures/call-graph already
    there). Files are kept in organization order."""
    block = stage_locator_block(tree, sid, summary, registers, file_stage,
                                collisions, lang=lang, heading_level=1)
    parts = [block, ""]
    direct = tree.direct_files(sid)
    if direct:
        parts.append("---\n")
        for rel in direct:
            parts.append(rf.render_file_md(rel, tree.cards.get(rel), lang))
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# ── disambiguation page (organized BY WORD — an agent searches a word) ────────


def disambiguation_md(tree, collisions: list[tuple[str, list[str]]],
                      summaries: dict[str, str], title: str, *,
                      lang: str = "en",
                      notes: dict[str, dict[str, str]] | None = None,
                      written: set[str] | None = None) -> str:
    """The by-word disambiguation page: for each colliding search word, the
    stages it lands on, each with a one-line 'what this one is'. Layer 1 uses the
    stage's duty line; layer 2 swaps in a sharper `notes[word][sid]` one-liner.

    written: the set of stage ids whose pages are actually emitted (subtree mode
    writes only a subset). A stage NOT in `written` is shown as plain text rather
    than a link, so the page never carries a dead link — while still listing the
    cross-subtree collision (its whole point). None = full build, link everything.
    """
    zh = lang == "zh"
    head = "搜索词消歧" if zh else "Search-word disambiguation"
    intro = ("同一个词会落到多个 stage。搜到一堆不知道选哪个时,在这里按词查——"
             "每个 stage 后面一句话说明它是什么、去哪。" if zh
             else "One word lands on several stages. When a search returns many "
                  "hits, look the word up here — each stage has a one-line 'what "
                  "this one is'.")
    lines = [f"# {title} — {head}", "", intro, ""]
    if not collisions:
        lines.append("_(无同名碰撞。)_" if zh else "_(No name collisions.)_")
        return "\n".join(lines) + "\n"

    def one_liner(word: str, sid: str) -> str:
        note = ((notes or {}).get(word, {}) or {}).get(sid, "").strip()
        if note:
            return note
        duty = _duty_line(summaries.get(sid) or tree.description(sid) or "")
        return duty or tree.title(sid)

    def anchor(sid: str) -> str:
        # link only to a page that exists; otherwise plain code-span (no dead link)
        if written is None or sid in written:
            return f"[`{sid}`]({sid}.md)"
        return f"`{sid}`"

    hits_label = "命中" if zh else "hits"
    for word, stages in collisions:
        lines.append(f"## `{word}`  ({len(stages)} {hits_label})")
        for sid in stages:
            lines.append(f"- {anchor(sid)} {tree.title(sid)} — "
                         f"{one_liner(word, sid)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── index (every stage, fixed-schema block, depth-ordered) ────────────────────


def index_md(tree, summaries: dict[str, str], registers: list[dict],
             file_stage: dict[str, str],
             collisions: list[tuple[str, list[str]]], title: str, *,
             lang: str = "en", roots: list[str] | None = None) -> str:
    """The agent index: a HOW-TO pointer then every content-bearing stage as the
    same fixed-schema locator block, walked in skeleton (lifecycle) order."""
    pointer = ("先读 [how_to_use.md](how_to_use.md) 了解字段语义与信任边界。"
               if lang == "zh"
               else "Read [how_to_use.md](how_to_use.md) first for field semantics "
                    "and the trust boundary.")
    lines = [f"# {title} — {'Agent 定位索引' if lang=='zh' else 'Agent Locator Index'}",
             "", pointer, ""]

    def has_content(sid: str) -> bool:
        return bool(tree.children(sid) or tree.buckets.get(sid))

    def walk(sid: str, depth: int) -> None:
        if has_content(sid):
            lines.append(stage_locator_block(
                tree, sid, summaries.get(sid, ""), registers, file_stage,
                collisions, lang=lang, heading_level=min(depth + 2, 6),
                link_heading=True))
            lines.append("")
        for cid in tree.children(sid):
            walk(cid, depth + 1)

    for top in (roots if roots is not None else tree.top_level):
        walk(top, 0)
    return "\n".join(lines).rstrip() + "\n"


# ── site driver ───────────────────────────────────────────────────────────────


def render_agent_site(tree, summaries: dict[str, str], registers: list[dict],
                      out_dir: Path, *, lang: str = "en",
                      disambig_notes: dict[str, dict[str, str]] | None = None,
                      roots: list[str] | None = None) -> dict:
    """Write the agent arm under `<out_dir>/agent/`: how_to_use.md, index.md,
    disambiguation.md, and one page per content-bearing stage. Pure rendering —
    reuses the in-memory tree/summaries/registers the human build already
    produced (zero extra LLM).

    disambig_notes: optional layer-2 {word: {stage_id: one-liner}} that sharpens
    the disambiguation.md entries; falls back to each stage's duty line.
    """
    agent_dir = Path(out_dir) / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    file_stage = build_file_stage_index(tree)
    token_index = build_title_token_index(tree)
    collisions = build_collision_index(tree, token_index)

    import os
    title = os.environ.get("HANDBOOK_TITLE", "System Handbook")

    def has_content(sid: str) -> bool:
        return bool(tree.children(sid) or tree.buckets.get(sid))

    # The set of stage pages that will actually be emitted (subtree mode writes
    # only a subset). disambiguation.md links only to these — a cross-subtree
    # collision to an unwritten stage is shown as plain text, never a dead link.
    todo = list(tree.order) if roots is None else _subtree_ids(tree, roots)
    written = {sid for sid in todo if has_content(sid)}

    (agent_dir / "how_to_use.md").write_text(how_to_use_md(lang), encoding="utf-8")
    (agent_dir / "disambiguation.md").write_text(
        disambiguation_md(tree, collisions, summaries, title, lang=lang,
                          notes=disambig_notes, written=written),
        encoding="utf-8")
    (agent_dir / "index.md").write_text(
        index_md(tree, summaries, registers, file_stage, collisions, title,
                 lang=lang, roots=roots),
        encoding="utf-8")

    n_pages = 0
    for sid in todo:
        if not has_content(sid):
            continue
        (agent_dir / f"{sid}.md").write_text(
            stage_page_md(tree, sid, summaries.get(sid, ""), registers,
                          file_stage, collisions, lang=lang),
            encoding="utf-8")
        n_pages += 1

    logger.info("agent arm: %d stage pages + index.md + disambiguation.md "
                "(%d collision words) + how_to_use.md → %s",
                n_pages, len(collisions), agent_dir)
    return {"n_stage_pages": n_pages, "n_collisions": len(collisions),
            "agent_dir": str(agent_dir)}


def _subtree_ids(tree, roots: list[str]) -> list[str]:
    out: list[str] = []

    def collect(sid: str) -> None:
        out.append(sid)
        for c in tree.children(sid):
            collect(c)
    for r in roots:
        collect(r)
    return out
