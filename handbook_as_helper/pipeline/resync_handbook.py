#!/usr/bin/env python3
"""resync_handbook.py — after the executor lands a code change, roll the phase-2 mapping
forward and bring the handbook's DERIVED layer (cards, line anchors, code-sites, index)
back in line with the code. Self-contained: only phase-1/2/3's own interfaces plus this
repo's build_handbook_skill are used.

Design (the user's): SEMANTICS FIRST BY NAME, COORDINATES LAST. Old line numbers never
participate in any judgment — "who changed" is decided by the plan declarations plus a
per-function content fingerprint; "where everything is" is recomputed wholesale from the
new tree's AST at the very end.

  A. semantic roll      — the planner's declarations (will_modify / will_add /
                          will_remove) drive the change-set, all by qualname.
  B. sha verdict        — for every mapped function: fingerprint of its NEW AST span vs
                          its PRISTINE AST span (position-independent, so a shifted-but-
                          untouched function compares EQUAL). Merged with A into the
                          verdict table: missed (declared but unchanged), unplanned
                          (changed but undeclared → upgraded), renamed (gone × new def
                          with identical body), removed, new.
  C. apply + coordinates— changed/new get ONE phase-2 classification round each over the
                          NEW source (stage + region split + purpose, region ranges come
                          AST-snapped from the proposal); then every surviving entry's
                          line_range/sha1 is recomputed: functions from the new AST,
                          regions of unchanged functions by pure arithmetic
                          (old + (new_fn_start − old_fn_start) — valid because an equal
                          fingerprint guarantees a verbatim-identical body).
  D. handbook writeback — unchanged-but-moved: card summary anchors rewritten;
                          changed/new/renamed: cards retranslated via phase-3 (content
                          cache first) and placed in their (re)assigned stage chapter;
                          removed/renamed-from: cards deleted; registers' code-sites
                          re-grepped; index.md rebuilt; mechanical end checks (sha
                          recompute + card↔entry coverage) into the report.

LLM appears ONLY at: the C classification of changed/new functions and the D translation
of their cards. Everything else is table lookup + AST + hashing + arithmetic + grep.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent                  # final_handbook_as_helper/pipeline/
HELPER_ROOT = HERE.parent
REPO_ROOT = HELPER_ROOT.parent
def _resolve_gen() -> Path:
    """The phase-2/3 generator whose modules resync drives, resolved by SCALE:

        $HANDBOOK_GEN_ROOT      explicit path (wins)
        $HANDBOOK_GEN_SCALE     'large' -> handbook_generate_large,
                                'small' -> handbook_generate_small
        (default)               first existing member-level generator
                                (handbook_generate_terminus, then _small)

    NOTE ON SCALE: `handbook_generate_large` uses a DIFFERENT, file-level phase-2/3
    API (file_assign / nav_pack / render_file — no pass_a_classify/apply/render_member/
    translate_member). resync's engine is MEMBER-level, so it drives the small/terminus
    generators. Selecting the large generator raises a clear error below rather than a
    cryptic ImportError; wiring resync onto the large file-level API is a separate port.
    """
    if os.environ.get("HANDBOOK_GEN_ROOT"):
        return Path(os.environ["HANDBOOK_GEN_ROOT"])
    scale = (os.environ.get("HANDBOOK_GEN_SCALE") or "").strip().lower()
    if scale in ("large", "big"):
        return REPO_ROOT / "handbook_generate_large"
    if scale in ("small", "member"):
        return REPO_ROOT / "handbook_generate_small"
    for cand in ("handbook_generate_terminus", "handbook_generate_small",
                 "handbook_generate_large", "handbook_generate"):
        if (REPO_ROOT / cand).exists():
            return REPO_ROOT / cand
    return REPO_ROOT / "handbook_generate"


_GEN = _resolve_gen()                                   # phase-2/3 code (member-level)
_REPO = REPO_ROOT.parent / "Harness_Translation"        # phase-2 final artifacts, phase-1
for _p in (str(_GEN / "phase3"), str(_GEN / "phase2"), str(HERE), str(HELPER_ROOT / "handbook_skills")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import pass_a_classify as pa        # noqa: E402  (phase-2: classification prompt)
    import apply as p2apply             # noqa: E402  (phase-2: apply_classification)
    from render_member import _slug, render_unit        # noqa: E402  (phase-3)
    from translate_member import (      # noqa: E402  (phase-3)
        build_prompt, collect_units, load_cached, save_cached, validate_translation)
except ModuleNotFoundError as _e:       # noqa: E402
    raise ModuleNotFoundError(
        f"resync could not load its member-level phase-2/3 modules from {_GEN} "
        f"(missing {_e.name!r}). resync drives the MEMBER-level generator "
        "(handbook_generate_small / handbook_generate_terminus: pass_a_classify + "
        "apply + render_member + translate_member). handbook_generate_large uses a "
        "different file-level API (file_assign / render_file) that resync does not yet "
        "support — set HANDBOOK_GEN_ROOT or HANDBOOK_GEN_SCALE=small to a member-level "
        "generator."
    ) from _e

import build_handbook_skill as bhs      # noqa: E402  (this repo: code-sites + index)
import lang_layer as _L                 # noqa: E402  (multi-language substrate)


def _first_existing(*candidates: Path) -> Path:
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


PHASE2_FINAL = (
    Path(os.environ["PHASE2_FINAL"]) if os.environ.get("PHASE2_FINAL")
    else _REPO / "handbook/phase2/iterations/final"
)
PRISTINE_HANDBOOK_JSON = _REPO / "handbook/phase3/output/handbook_en.json"
UPSTREAM_CACHE = _REPO / "handbook/phase3/cache"        # phase-3's own translate cache
CACHE_ROOT = HERE / "cache" / "translate_resync"
LANG = "en"

# Usage accounting for the resync's own LLM calls (the agents' usage lives in the
# NexAU traces; these single-shot calls would otherwise be invisible). resync() points
# _USAGE_PATH at <case>/resync_llm_usage.jsonl; _reclassify_one/_translate_card tag the
# phase; _EnvLLM appends one record per call.
_LLM_PHASE = "unknown"
_USAGE_PATH: Path | None = None
_USAGE_LOCK = threading.Lock()          # serialize appends when D translation runs threaded


def _log_usage(model: str, usage: dict) -> None:
    if _USAGE_PATH is None or not usage:
        return
    rec = {"phase": _LLM_PHASE, "model": model,
           "in": usage.get("prompt_tokens", 0),
           "out": usage.get("completion_tokens", 0)}
    cached = usage.get("prompt_cache_hit_tokens",
                       (usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
    if cached is not None:
        rec["cached"] = cached
    try:
        with _USAGE_LOCK, _USAGE_PATH.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass                                   # accounting must never break the resync


class _EnvLLM:
    """Resync LLM on the SAME OpenAI-compatible endpoint/model the agents use (the
    OPENAI_*/LLM_* env). One bare /chat/completions POST per call, mirroring
    api_client.Api's contract (.call(prompt) → .raw_text / .parsed_json) so the
    classification and translation code paths cannot tell the backends apart.
    Retries transient failures. Each call's token usage is appended to the case's
    resync_llm_usage.jsonl (see _log_usage)."""

    def __init__(self, max_retries: int = 3, backoff_sec: float = 2.0) -> None:
        # Resolve the OpenAI-compatible endpoint from the standard OpenAI env vars, then
        # the LLM_* equivalents, then the public OpenAI defaults — so resync works even
        # when code_agent's env bridge has not been imported in this process.
        self.base = (os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
                     or "https://api.openai.com/v1").rstrip("/")
        self.model = (os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL")
                      or "gpt-4o-mini")
        # Require an explicit key (mirrors the planner's _load_official_dict): fail loud on a
        # missing key rather than sending "Bearer EMPTY" and getting a 401 mid-run. For a
        # keyless local endpoint, set OPENAI_API_KEY=EMPTY (or LLM_API_KEY=EMPTY) explicitly.
        self.key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not self.key:
            raise EnvironmentError(
                "missing API key: set OPENAI_API_KEY (or LLM_API_KEY). For a keyless local "
                "endpoint, set OPENAI_API_KEY=EMPTY.")
        self.extra = (json.loads(os.environ["LLM_EXTRA_BODY"])
                      if os.environ.get("LLM_EXTRA_BODY") else {})
        self.max_retries = max_retries
        self.backoff_sec = backoff_sec

    def call(self, prompt: str, params: dict | None = None):
        import time

        import requests
        from api_client import LLMCallResult, _extract_json_block

        body = {"model": self.model, "temperature": 0.0, "max_tokens": 12000,
                "messages": [{"role": "user", "content": prompt}], **self.extra,
                **(params or {})}
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.time()
            try:
                r = requests.post(
                    f"{self.base}/chat/completions",
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {self.key}"},
                    json=body, timeout=600,
                )
                r.raise_for_status()
                data = r.json()
                text = data["choices"][0]["message"]["content"] or ""
                _log_usage(self.model, data.get("usage") or {})
                return LLMCallResult(
                    raw_text=text, status_code=r.status_code, request_id="",
                    elapsed_sec=time.time() - t0,
                    parsed_json=_extract_json_block(text))
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < self.max_retries:
                    time.sleep(self.backoff_sec * attempt)
        assert last is not None
        raise last


_api = None


def _get_api():
    """The resync LLM backend (the OpenAI-compatible OPENAI_*/LLM_* endpoint), created
    once per process. The classification + translation calls use the same model as the
    agents — one model, one bill."""
    global _api
    if _api is None:
        _api = _EnvLLM()
    return _api


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ─── plan declarations ────────────────────────────────────────────────────────

_DECL_KEYS = ("will_modify", "will_add", "will_remove")


def parse_declarations(plan_text: str) -> dict:
    """The LAST ```json block in the plan that carries any will_* key. Tolerant: a
    missing/broken block degrades to empty lists (the sha verdict then carries the whole
    change-set as 'unplanned' — resync still works, just noisier)."""
    out = {k: [] for k in _DECL_KEYS}
    for m in re.finditer(r"```json\s*(.*?)```", plan_text, re.S):
        try:
            d = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(d, dict) and any(k in d for k in _DECL_KEYS):
            for k in _DECL_KEYS:
                v = d.get(k)
                out[k] = [str(q) for q in v if isinstance(q, str)] \
                    if isinstance(v, list) else []
    return out


def validate_declarations(decl: dict) -> dict:
    """①.5 mechanical check (zero LLM): modify/remove must name LEDGER functions,
    add must NOT. Catches planner typos before anything downstream consumes them."""
    mapping = yaml.safe_load((PHASE2_FINAL / "mapping.yaml").read_text())
    known = {m["qualname"] for st in (mapping.get("stages") or {}).values()
             for m in st.get("members") or []
             if m.get("type") in ("function", "region") and m.get("qualname")}
    errors = []
    for q in decl.get("will_modify") or []:
        if q not in known:
            errors.append(f"will_modify names a function not in the ledger: {q}")
    for q in decl.get("will_remove") or []:
        if q not in known:
            errors.append(f"will_remove names a function not in the ledger: {q}")
    for q in decl.get("will_add") or []:
        if q in known:
            errors.append(f"will_add names a function already in the ledger: {q}")
    return {"ok": not errors, "errors": errors}


# ─── mechanics: spans, fingerprints, syntax gate (LANGUAGE-AGNOSTIC) ─────────
# The per-function line spans, rename fingerprint and fresh call graph are provided
# by lang_layer (_L): Python via `ast` (byte-identical to the original helpers),
# every other language via the handbook_generate_small tree-sitter adapters. Only
# _trim_eof_garbage + the freeze bookkeeping live here.

def _trim_eof_garbage(py: Path, err_lineno: int, max_trim: int = 8) -> bool:
    """Known Python-executor failure shape: the replace tool's fuzzy fallback appends
    truncated line fragments after the real last line. Only acts when the error sits in
    the last few lines; strips trailing lines until the file compiles (no write if it
    never does). Python-only — other languages skip straight to the freeze."""
    lines = py.read_text().splitlines()
    if err_lineno and err_lineno < len(lines) - max_trim:
        return False
    for k in range(1, max_trim + 1):
        cand = "\n".join(lines[:-k]) + "\n"
        try:
            compile(cand, py.name, "exec")
        except SyntaxError:
            continue
        py.write_text(cand)
        return True
    return False


def _syntax_gate(code_dir: Path, report: dict, lang: str,
                 files: list[str]) -> set[str]:
    """Each MAPPED source file in the edited tree must parse; one that doesn't is
    FROZEN (report-only) — its ledger entries and cards are left untouched, because a
    failed parse would otherwise misread every function in it as removed (card-deletion
    cascade). Python EOF-tail corruption is auto-repaired first.

    Only the files the mapping references (`files`, relative paths) are checked — never
    the whole tree — so a large workspace (e.g. a Rust cargo tree with thousands of
    sources) stays cheap and resync never freezes a file it wouldn't touch anyway. A
    file missing from the edited tree is left to the caller's own handling."""
    bad: set[str] = set()
    for rel_s in files:
        f = code_dir / rel_s
        if not f.exists():
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        if _L.syntax_ok(f, lang, src):
            continue
        if lang == "python":
            try:
                compile(src, f.name, "exec")
            except SyntaxError as e:
                if _trim_eof_garbage(f, e.lineno or 0):
                    report["repaired_files"].append(rel_s)
                    continue
                report["errors"].append(f"{rel_s}:{e.lineno}: {e.msg} — file frozen")
                bad.add(rel_s)
                continue
        bad.add(rel_s)
        report["errors"].append(f"{rel_s}: does not parse — file frozen")
    return bad


# ─── LLM step 1: phase-2 classification of a changed/new function ────────────

def _classify_propose(api, qualname: str, span: tuple[int, int], fname: str,
                      mapping_doc: dict, skeleton: dict, graph: dict,
                      code_dir: Path) -> dict | None:
    """The PURE (LLM-only) half of a classification round over the NEW source: build the
    actor prompt and return the proposal dict (stage assignment(s), region split, purpose
    text), or None when the reply is unusable. Reads a mapping/graph SNAPSHOT only and
    NEVER mutates the mapping, so it is safe to run concurrently — the mapping write-back
    (apply_classification) is done separately and serially by the caller."""
    global _LLM_PHASE
    _LLM_PHASE = "classify"
    node = {"qualname": qualname, "file": fname,
            "line_start": span[0], "line_end": span[1]}
    src = pa.render_source_with_line_numbers(code_dir / fname, span[0], span[1])
    callers, callees = pa._build_caller_callee_context(qualname, graph, mapping_doc)
    overview = pa._build_stage_overview(mapping_doc)
    prompt = (pa.build_actor_prompt(node, src, skeleton, callers, callees, overview)
              + "\n\n" + pa._PROPOSAL_SCHEMA_HINT
              + "\n\nReturn ONLY the JSON proposal object, no other text.")
    prop = api.call(prompt).parsed_json
    if not isinstance(prop, dict) or prop.get("qualname") != qualname:
        return None
    prop.setdefault("file", fname)
    prop.setdefault("line_range", [span[0], span[1]])
    return prop


def _apply_proposal(prop: dict, skeleton: dict, mapping_doc: dict,
                    code_dir: Path) -> None:
    """Serial mapping write-back for a proposal from _classify_propose (mutates
    mapping_doc in place; region ranges are AST-snapped inside apply_classification).
    Must run single-threaded — it rewrites shared mapping members."""
    valid = {s["id"] for s in skeleton.get("stages", [])}
    p2apply.apply_classification(mapping_doc, prop, code_dir, valid_stage_ids=valid)


def _reclassify_one(api, qualname: str, span: tuple[int, int], fname: str,
                    mapping_doc: dict, skeleton: dict, graph: dict,
                    code_dir: Path) -> bool:
    """ONE classification round over the NEW source (propose + apply), fully serial.
    Retained for the old one-shot contract; resync() itself now fetches proposals
    concurrently and applies them serially (see the C step). Returns True when applied."""
    prop = _classify_propose(api, qualname, span, fname, mapping_doc, skeleton, graph,
                             code_dir)
    if prop is None:
        return False
    _apply_proposal(prop, skeleton, mapping_doc, code_dir)
    return True


# ─── LLM step 2: phase-3 translation of one (stage, function) card ───────────

def _sibling_synopses() -> dict[str, list[tuple[str, str]]]:
    if not PRISTINE_HANDBOOK_JSON.exists():
        return {}
    d = json.loads(PRISTINE_HANDBOOK_JSON.read_text())
    out: dict[str, list[tuple[str, str]]] = {}
    for sid, s in (d.get("stages") or {}).items():
        out[sid] = [(fn.get("qualname", "?"), syn)
                    for fn in s.get("functions") or []
                    if (syn := (fn.get("translation") or {}).get("synopsis") or "")]
    return out


def _translate_card(stage_id: str, members: list[dict], code_dir: Path,
                    skeleton: dict, synopses: dict) -> str:
    """One unit → card markdown. Cache ladder: upstream phase-3 cache → local cache →
    ONE validated LLM call (an invalid reply raises; the caller keeps the old card and
    reports)."""
    global _LLM_PHASE
    _LLM_PHASE = "translate"
    unit = collect_units(stage_id, members, code_dir)[0]
    translation = None
    if UPSTREAM_CACHE.exists():
        translation = load_cached(UPSTREAM_CACHE, unit, lang=LANG)
    if translation is None:
        translation = load_cached(CACHE_ROOT, unit, lang=LANG)
    if translation is None:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        prompt = build_prompt(unit, skeleton, synopses.get(stage_id, []), lang=LANG)
        # validate_translation()'s hard rules, spelled out — common model failure
        # modes: honest empty relations lists, a dropped schema_version, region-count
        # drift on multi-region units (stronger models tend to satisfy these implicitly)
        prompt += (
            "\n\nSTRICT OUTPUT RULES (the validator REJECTS the reply otherwise):\n"
            "- `schema_version` MUST be present and exactly 1.\n"
            "- EVERY list under `relations` (callers, core_callees, "
            "config_state_sources, results_to) MUST be a non-empty array; when there "
            "is genuinely nothing to list, use [\"(none)\"].\n"
            f"- `type` MUST be \"{unit.type_kind}\".\n")
        if unit.type_kind == "multi_region":
            prompt += (
                f"- `regions` MUST contain EXACTLY {len(unit.entries)} items, one per "
                f"source region in order, each with `gloss` and `line_range`; "
                f"`overall_structure` MUST have the same number of items.\n")
        result = _get_api().call(prompt)
        translation = result.parsed_json
        if translation is None:
            raise RuntimeError(f"translation: no parseable JSON for {unit.qualname}")
        # mechanical normalization of INFORMATION-FREE schema slips (residual model
        # slips even with the strict rules in the prompt): schema_version is a
        # constant, and an empty relations list means "(none)" — neither is worth
        # failing a whole card over. Substantive problems (region counts, missing
        # sections) still fail validation below.
        translation.setdefault("schema_version", 1)
        rel = translation.get("relations")
        if isinstance(rel, dict):
            for k in ("callers", "core_callees", "config_state_sources", "results_to"):
                if not rel.get(k):
                    rel[k] = ["(none)"]
        err = validate_translation(unit, translation)
        if err is not None:
            raise RuntimeError(f"translation invalid for {unit.qualname}: {err}")
        save_cached(CACHE_ROOT, unit, translation, result.raw_text, lang=LANG)
    return render_unit(unit, translation, lang=LANG)


# ─── handbook card surgery ────────────────────────────────────────────────────

def _stage_files(hb_dir: Path) -> list[Path]:
    return sorted((hb_dir / "stages").glob("*.md"))


def _card_re(slug: str) -> re.Pattern:
    return re.compile(r'<details id="' + re.escape(slug) + r'">.*?</details>\n?', re.S)


def _card_files(hb_dir: Path, slug: str) -> list[Path]:
    needle = f'<details id="{slug}">'
    return [f for f in _stage_files(hb_dir) if needle in f.read_text()]


def _delete_cards(hb_dir: Path, slug: str, only: set[Path] | None = None) -> int:
    n = 0
    for f in _card_files(hb_dir, slug):
        if only is not None and f not in only:
            continue
        f.write_text(_card_re(slug).sub("", f.read_text(), count=1))
        n += 1
    return n


def _refresh_anchor(hb_dir: Path, qualname: str, fname: str,
                    old_env: tuple[int, int], new_env: tuple[int, int]) -> bool:
    """Rewrite ONE card's `file.py:a-b` summary range, identified by its OLD envelope
    (a multi-chapter function has several same-slug cards; the envelope tells them
    apart). Any `(N regions)` suffix after the range is untouched."""
    if old_env == new_env:
        return True
    pat = re.compile(
        r"(<summary><b>" + re.escape(qualname) + r"</b>\s*—\s*" + re.escape(fname)
        + r":)" + rf"{old_env[0]}-{old_env[1]}\b")
    for f in _stage_files(hb_dir):
        text, n = pat.subn(lambda m: m.group(1) + f"{new_env[0]}-{new_env[1]}",
                           f.read_text(), count=1)
        if n:
            f.write_text(text)
            return True
    return False


def _rename_card_summary(hb_dir: Path, old_q: str, new_q: str) -> int:
    """Mechanical name swap on a renamed function's card(s): the `<details id>` slug and
    the `<summary><b>…</b>` qualname are rewritten in place, the prose body is kept.
    Used when card translation is OFF — the card stays findable and coverage-checkable
    under its new name (body text may still mention the old name until a translate
    pass runs)."""
    old_slug, new_slug = _slug(old_q), _slug(new_q)
    n = 0
    for f in _card_files(hb_dir, old_slug):
        text = f.read_text()
        text = text.replace(f'<details id="{old_slug}">', f'<details id="{new_slug}">')
        text = text.replace(f"<summary><b>{old_q}</b>", f"<summary><b>{new_q}</b>")
        f.write_text(text)
        n += 1
    return n


def _chapter_file(hb_dir: Path, sid: str, mapping: dict, exclude: str) -> Path | None:
    """The md file hosting stage `sid`'s cards: its own chapter file when one exists,
    else the file holding a card of another member of that stage."""
    f = hb_dir / "stages" / f"{sid}.md"
    if f.exists():
        return f
    for m in (mapping.get("stages") or {}).get(sid, {}).get("members") or []:
        if m.get("qualname") and m["qualname"] != exclude:
            hosts = _card_files(hb_dir, _slug(m["qualname"]))
            if hosts:
                return hosts[0]
    return None


# ─── minimal-patch of an existing card (changed functions) ─────────────────────
# For a function whose body CHANGED, the old card is assumed correct and reused as the
# baseline: instead of translating a fresh card, we hand the LLM the OLD card plus a unified
# diff of just that function and ask for the SMALLEST edit that reflects the change. Output is
# markdown (so unchanged sentences stay byte-identical → minimal handbook diff). A light
# STRUCTURAL self-check guards it; on any failure the caller falls back to full translation
# (which carries the strict JSON validator). The line anchor is rolled mechanically, not by
# the LLM.

_CARD_STRUCT_LABELS = ("**Relations**", "**Callers**", "**Core callees**",
                       "**Config / state sources**", "**Results to**")

_PATCH_PROMPT = """You maintain a reference "card" for ONE function in a Terminus-2 handbook.
The function's body just changed. You are given the CURRENT card (assumed correct for the old
code) and a unified diff of the function's source (old → new). Produce the UPDATED card.

RULES — make the SMALLEST possible edit:
- REUSE the current card as the baseline. Keep every sentence/line that the code change does
  not affect BYTE-FOR-BYTE. Do NOT rephrase, reorder, or "improve" anything for style.
- Change ONLY the facts the diff actually affects: the one-line role, and the Relations
  entries (Callers, Core callees, Config / state sources, Results to) — e.g. a newly called
  function, a new state read/write, a removed caller, a changed condition/behavior.
- If the change affects NONE of the card's stated facts, return the current card UNCHANGED.
- Do NOT modify the `file.py:NN-NN` line range in the summary — leave it exactly as is (it is
  corrected automatically afterwards).
- Keep the exact card structure: the `<details id="...">` wrapper, the `<summary><b>...</b>`
  line, the `**Relations**` block with its four bullet labels, and the closing `</details>`.

Output ONLY the card (the single `<details>...</details>` block) — no code fences, no
commentary.

=== CURRENT CARD ===
{old_card}

=== FUNCTION SOURCE CHANGE (unified diff) ===
{diff}
"""


def _func_unified_diff(old_lines: list[str], so: tuple[int, int],
                       new_lines: list[str], sn: tuple[int, int], fname: str) -> str:
    a = [ln + "\n" for ln in old_lines[so[0] - 1:so[1]]]
    b = [ln + "\n" for ln in new_lines[sn[0] - 1:sn[1]]]
    return "".join(difflib.unified_diff(a, b, fromfile=f"{fname} (old)",
                                        tofile=f"{fname} (new)", lineterm="\n"))


def _old_card_text(hb_dir: Path, sid: str, mapping: dict, q: str) -> str | None:
    """The existing card markdown for q — its sid chapter first, else any chapter holding it."""
    slug = _slug(q)
    target = _chapter_file(hb_dir, sid, mapping, q)
    if target:
        m = _card_re(slug).search(target.read_text())
        if m:
            return m.group(0)
    for f in _card_files(hb_dir, slug):
        m = _card_re(slug).search(f.read_text())
        if m:
            return m.group(0)
    return None


def _card_struct_ok(card: str, slug: str, qual: str) -> bool:
    """Light shape check on a minimal-patched card (no JSON validation): a single well-formed
    <details> card with the right slug/qualname and the Relations block intact."""
    return (f'<details id="{slug}">' in card
            and f"<summary><b>{qual}</b>" in card
            and card.count("<details") == 1 and card.count("</details>") == 1
            and all(lbl in card for lbl in _CARD_STRUCT_LABELS))


def _patch_card(old_card: str, func_diff: str, qual: str) -> str:
    """ONE LLM call: minimally edit `old_card` to reflect `func_diff`. Returns markdown
    (caller runs the structural check and falls back to full translation on failure)."""
    global _LLM_PHASE
    _LLM_PHASE = "patch"
    result = _get_api().call(_PATCH_PROMPT.format(old_card=old_card, diff=func_diff))
    text = (result.raw_text or "").strip()
    text = re.sub(r"^```(?:\w+)?\s*", "", text)      # strip stray code fences
    text = re.sub(r"\s*```$", "", text).strip()
    return text


# ─── the resync driver ────────────────────────────────────────────────────────

def resync(code_dir: Path, hb_dir: Path, pristine_dir: Path, decl: dict,
           mapping_out: Path | None = None, translate_cards: bool = True,
           lang: str = "python", source_exts: tuple[str, ...] = (".py",)) -> dict:
    """A→B→C→D as in the module docstring. Returns the report dict (the caller persists
    it as resync_report.json).

    `lang` / `source_exts` drive the LANGUAGE substrate (via lang_layer): "python"
    uses `ast` (unchanged behavior); "rust"/"typescript"/"go"/... use the
    handbook_generate_small tree-sitter adapters. All spans, the syntax gate, the
    rename fingerprint and the fresh call graph route through it.

    translate_cards=False turns OFF the final phase-3 card translation (the most
    expensive LLM step): everything mechanical still runs in full — verdicts, ledger
    roll, coordinates, anchor refresh, removed-card deletion, registers, index — but
    changed functions keep their OLD card prose (stale text, correct line anchors),
    renamed cards get a mechanical name swap, and new functions get no card yet; all
    of them are listed in report["cards_pending"] so a later translate-only pass can
    pick them up."""
    report: dict = {"verdicts": {}, "missed": [], "unplanned": [], "renamed": [],
                    "removed": [], "new": [], "unassigned": [], "anchors_refreshed": 0,
                    "cards_translated": [], "cards_patched": [], "cards_deleted": [],
                    "cards_pending": [],
                    "repaired_files": [], "frozen_files": [], "errors": [], "check": {}}
    decl = {k: list(decl.get(k) or []) for k in _DECL_KEYS}

    # this run's own LLM usage ledger (fresh per run — reruns must not accumulate)
    global _USAGE_PATH
    _USAGE_PATH = (mapping_out.parent / "resync_llm_usage.jsonl"
                   if mapping_out is not None else None)
    if _USAGE_PATH is not None:
        _USAGE_PATH.unlink(missing_ok=True)

    # PER-CASE translation cache. The phase-3 cache stores ONE file per
    # (stage, qualname) — but every case edits the source differently, so a SHARED
    # cache dir makes parallel cases overwrite each other's entries (observed: a full
    # 8-way Phase-B sweep turned every lookup into a miss and re-translated whole
    # cases). Per-case keeps the only useful property — same-case reruns hit — and
    # kills the contention.
    global CACHE_ROOT
    if mapping_out is not None:
        CACHE_ROOT = mapping_out.parent / "cache_translate"

    mapping = yaml.safe_load((PHASE2_FINAL / "mapping.yaml").read_text())
    skeleton = yaml.safe_load((PHASE2_FINAL / "skeleton.yaml").read_text())
    original = deepcopy(mapping)                       # old envelopes for anchor refresh

    # ledger units by qualname
    units: dict[str, list[tuple[str, dict]]] = {}
    for sid, st in (mapping.get("stages") or {}).items():
        for mem in st.get("members") or []:
            if mem.get("type") in ("function", "region") and mem.get("line_range"):
                units.setdefault(mem["qualname"], []).append((sid, mem))

    # gate exactly the mapped files (not the whole tree), then read spans on both trees
    files = sorted({entries[0][1]["file"] for entries in units.values()})
    bad_files = _syntax_gate(code_dir, report, lang, files)

    spans_old: dict[str, dict] = {}
    spans_new: dict[str, dict] = {}
    lines_old: dict[str, list[str]] = {}
    lines_new: dict[str, list[str]] = {}
    for fname in files:
        try:
            spans_old[fname] = _L.spans(pristine_dir / fname, lang)
            lines_old[fname] = (pristine_dir / fname).read_text().splitlines()
        except Exception as e:  # noqa: BLE001 — a bad pristine file freezes, never crashes
            spans_old[fname] = {}
            report["errors"].append(f"pristine {fname}: {e!r}")
        if fname in bad_files or not (code_dir / fname).exists():
            spans_new[fname] = {}
            if fname not in bad_files:
                report["errors"].append(f"{fname}: missing from edited tree — frozen")
                bad_files.add(fname)
            continue
        try:
            spans_new[fname] = _L.spans(code_dir / fname, lang)
            lines_new[fname] = (code_dir / fname).read_text().splitlines()
        except Exception as e:  # noqa: BLE001 — passed the parse gate but analyze failed
            spans_new[fname] = {}
            bad_files.add(fname)
            report["errors"].append(f"{fname}: span extraction failed ({e!r}) — frozen")
    report["frozen_files"] = sorted(bad_files)

    # ── B · sha verdicts (by name; position-independent) ──────────────────────
    verdict: dict[str, str] = {}
    gone: list[str] = []
    for q, entries in units.items():
        fname = entries[0][1]["file"]
        if fname in bad_files:
            verdict[q] = "unparsable"
            continue
        so, sn = spans_old[fname].get(q), spans_new[fname].get(q)
        if so is None:                                  # ledger/pristine drift — freeze
            verdict[q] = "unparsable"
            report["errors"].append(f"{q}: not found in pristine AST — frozen")
            continue
        if sn is None:
            verdict[q] = "gone"
            gone.append(q)
            continue
        old_sha = _sha1("\n".join(lines_old[fname][so[0] - 1:so[1]]))
        new_sha = _sha1("\n".join(lines_new[fname][sn[0] - 1:sn[1]]))
        verdict[q] = "unchanged" if old_sha == new_sha else "changed"

    # brand-new defs (in the edited tree, absent from ledger AND pristine)
    new_defs: dict[str, tuple[str, tuple[int, int]]] = {}
    for fname in files:
        if fname in bad_files:
            continue
        for q, span in spans_new[fname].items():
            if q not in units and q not in spans_old.get(fname, {}):
                new_defs[q] = (fname, span)

    # renames: a gone qualname whose body matches a new def verbatim
    all_new = set(new_defs)          # snapshot BEFORE rename consumption (for reconcile:
    #                                  a declared rename = remove(old)+add(new), and its
    #                                  target must still count as a fulfilled add)
    renames: dict[str, str] = {}                        # old -> new
    for q in list(gone):
        fname = units[q][0][1]["file"]
        so = spans_old[fname][q]
        fp = _L.body_fingerprint(lines_old[fname][so[0] - 1:so[1]], lang)
        for nq, (nf, nspan) in list(new_defs.items()):
            if nf != fname:
                continue
            if _L.body_fingerprint(lines_new[nf][nspan[0] - 1:nspan[1]], lang) == fp:
                renames[q] = nq
                verdict[q] = "renamed"
                del new_defs[nq]
                gone.remove(q)
                break
    report["renamed"] = [f"{a} -> {b}" for a, b in renames.items()]

    # merge with the declarations → final verdicts + discrepancy report
    will_modify = set(decl["will_modify"])
    will_remove = set(decl["will_remove"])
    will_add = set(decl["will_add"])
    report["missed"] = sorted(
        [q for q in will_modify if verdict.get(q) == "unchanged"]
        + [q for q in will_remove if verdict.get(q) in ("unchanged", "changed")]
        + [q for q in will_add if q not in all_new])           # declared add, never built
    report["unplanned"] = sorted(
        [q for q, v in verdict.items() if v == "changed" and q not in will_modify]
        + [q for q in gone if q not in will_remove]
        + [q for q in new_defs if q not in will_add])          # undeclared new def
    report["verdicts"] = dict(sorted(verdict.items()))
    report["removed"] = sorted(gone)

    changed = sorted(q for q, v in verdict.items() if v == "changed")

    # ── A/C · semantic roll on the ledger ─────────────────────────────────────
    def _drop_entries(q: str) -> None:
        for st in (mapping.get("stages") or {}).values():
            st["members"] = [m for m in st.get("members") or []
                             if not (m.get("qualname") == q
                                     and m.get("type") in ("function", "region"))]

    # removed → entries deleted
    for q in gone:
        _drop_entries(q)

    # renamed → entries carried over under the new name (body identical: stage, purpose
    # and region STRUCTURE survive; coordinates roll in the pass below)
    for old_q, new_q in renames.items():
        for _sid, mem in units[old_q]:
            mem["qualname"] = new_q

    # changed + new → one classification round each. The LLM PROPOSAL is a pure call
    # (reads the new source + a mapping/graph snapshot, returns JSON), so proposals are
    # fetched CONCURRENTLY (RESYNC_WORKERS); the mapping write-back (apply_classification)
    # and every mechanical fallback run STRICTLY SERIAL below in the original target
    # order, so the resulting mapping is identical regardless of worker count. (Proposals
    # read the pre-classification mapping snapshot for their stage-overview / caller
    # context rather than an incrementally-updated one — immaterial: the targets are
    # independent functions.)
    graph = _L.fresh_graph(code_dir, lang, source_exts[0] if source_exts else ".py")
    synopses = _sibling_synopses()
    targets = [(q, units[q][0][1]["file"], spans_new[units[q][0][1]["file"]][q])
               for q in changed]
    targets += [(q, nf, nspan) for q, (nf, nspan) in sorted(new_defs.items())]

    def _propose(t):
        q, fname, span = t
        try:
            return t, _classify_propose(_get_api(), q, span, fname, mapping,
                                        skeleton, graph, code_dir), None
        except Exception as e:  # noqa: BLE001
            return t, None, e

    workers = max(1, int(os.environ.get("RESYNC_WORKERS", "1")))
    if targets:
        _get_api()                                     # warm the shared client singleton
    if len(targets) <= 1 or workers == 1:
        proposals = [_propose(t) for t in targets]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            proposals = list(ex.map(_propose, targets))

    for (q, fname, span), prop, err in proposals:
        is_new = q in new_defs
        ok = False
        if err is not None:
            report["errors"].append(f"{q}: classification failed ({err!r})")
        elif prop is None:
            report["errors"].append(
                f"{q}: classification proposal rejected — mechanical fallback")
        else:
            try:
                _apply_proposal(prop, skeleton, mapping, code_dir)
                ok = True
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"{q}: classification apply failed ({e!r})")
        if ok:
            if is_new:
                report["new"].append(q)
            continue
        # fallback: changed keeps its old primary stage as ONE whole-function member;
        # new goes to its first caller's stage, else unmapped_functions
        seg = "\n".join(lines_new[fname][span[0] - 1:span[1]])
        member = {"qualname": q, "type": "function", "file": fname,
                  "line_range": [span[0], span[1]], "sha1": _sha1(seg),
                  "purpose": (units[q][0][1].get("purpose", "") if not is_new
                              else "(new function added by a code change)")}
        if not is_new:
            sid = units[q][0][0]
            _drop_entries(q)
            mapping["stages"][sid]["members"].append(member)
        else:
            host = None
            short = q.split(".")[-1]
            call_re = re.compile(r"(?<!\w)" + re.escape(short) + r"\s*\(")
            for cq, centries in units.items():
                cf = centries[0][1]["file"]
                cspan = spans_new.get(cf, {}).get(cq if cq not in renames
                                                  else renames[cq])
                if cq == q or cspan is None:
                    continue
                if call_re.search("\n".join(lines_new[cf][cspan[0] - 1:cspan[1]])):
                    host = centries[0][0]
                    break
            if host:
                mapping["stages"][host]["members"].append(member)
                report["new"].append(q)
            else:
                mapping.setdefault("unmapped_functions", []).append(
                    {"qualname": q, "file": fname,
                     "reason": "new function; classification failed, caller unknown"})
                report["unassigned"].append(q)

    # ── C · coordinates last: recompute every surviving non-classified entry ──
    # (apply_classification already wrote fresh AST-snapped coordinates for the entries
    #  it inserted; here we roll the UNCHANGED/RENAMED ones — functions from the new
    #  AST, regions by arithmetic delta, shas from the new text.)
    classified = {q for q, _f, _s in targets}
    for sid, st in (mapping.get("stages") or {}).items():
        for mem in st.get("members") or []:
            q = mem.get("qualname")
            if not q or mem.get("type") not in ("function", "region") \
                    or not mem.get("line_range") or q in classified:
                continue
            old_q = next((a for a, b in renames.items() if b == q), q)
            if verdict.get(old_q) == "unparsable":
                continue                               # frozen
            fname = mem["file"]
            sn = spans_new.get(fname, {}).get(q)
            so = spans_old.get(fname, {}).get(old_q)
            if sn is None or so is None:
                continue
            if mem["type"] == "function":
                mem["line_range"] = [sn[0], sn[1]]
            else:                                      # region: pure arithmetic
                delta = sn[0] - so[0]
                a, b = mem["line_range"]
                mem["line_range"] = [a + delta, b + delta]
            a, b = mem["line_range"]
            mem["sha1"] = _sha1("\n".join(lines_new[fname][a - 1:b]))

    if mapping_out is not None:
        mapping.setdefault("metadata", {})["resynced_by"] = "handbook_as_helper_v2"
        mapping_out.write_text(yaml.safe_dump(mapping, allow_unicode=True,
                                              sort_keys=False))

    # ── D · handbook writeback ────────────────────────────────────────────────
    # old/new per-(stage, function) envelopes → anchor refresh for untouched cards
    def _envelopes(doc: dict) -> dict[tuple[str, str], tuple[tuple[int, int], str]]:
        env: dict = {}
        for sid, st in (doc.get("stages") or {}).items():
            for m in st.get("members") or []:
                if m.get("type") in ("function", "region") and m.get("line_range"):
                    key = (sid, m["qualname"])
                    a, b = m["line_range"]
                    if key in env:
                        (lo, hi), f = env[key]
                        env[key] = ((min(lo, a), max(hi, b)), f)
                    else:
                        env[key] = ((a, b), m["file"])
        return env

    env_old, env_new = _envelopes(original), _envelopes(mapping)
    retranslate_quals = set(changed) | set(new_defs) | set(renames.values()) \
        | {renames.get(q, q) for q in changed}

    # renamed cards: with translation OFF they survive under the new name (mechanical
    # swap BEFORE the anchor pass so the refresh regex matches the new qualname)
    if not translate_cards:
        for old_q, new_q in renames.items():
            _rename_card_summary(hb_dir, old_q, new_q)

    # anchor refresh — with translation ON, cards about to be retranslated are skipped
    # (their replacement carries fresh anchors); with translation OFF every surviving
    # card gets its coordinates rolled, stale prose or not. When the classification
    # MOVED a function to another stage, (old sid, q) has no new envelope — fall back
    # to the function's UNIQUE new envelope when there is exactly one (still the right
    # function-level range for the old card).
    new_by_qual: dict[str, list] = {}
    for (_s2, qn), val in env_new.items():
        new_by_qual.setdefault(qn, []).append(val)

    def _anchor_pass(only: set[str] | None, skip: set[str]) -> int:
        n = 0
        for (sid, q), (oe, fname) in env_old.items():
            q_now = renames.get(q, q)
            if only is not None and q_now not in only:
                continue
            if q_now in skip or verdict.get(q) in ("unparsable", "gone"):
                continue
            ne = env_new.get((sid, q_now))
            if ne is None:
                cands = new_by_qual.get(q_now) or []
                ne = cands[0] if len(cands) == 1 else None
            if ne and ne[0] != oe and _refresh_anchor(hb_dir, q_now, fname, oe, ne[0]):
                n += 1
        return n

    skip_anchor = retranslate_quals if translate_cards else set()
    report["anchors_refreshed"] += _anchor_pass(None, skip_anchor)

    # removed (and, when translating, renamed-from) → cards deleted
    drop = list(gone) + (list(renames) if translate_cards else [])
    for q in drop:
        n = _delete_cards(hb_dir, _slug(q))
        if n:
            report["cards_deleted"].append(q)

    if not translate_cards:
        # derived prose FROZEN: changed keep their old cards (anchors already rolled),
        # new functions have no card yet — everything a translate pass still owes is
        # listed for a later run
        report["cards_pending"] = sorted(retranslate_quals)
        retranslate_quals = set()

    # changed / new / renamed-to → retranslate one card per hosting stage.
    # The LLM translation (`_translate_card`) is the dominant cost and is a PURE call
    # (reads code/skeleton/cache, returns markdown), so it is run concurrently when
    # RESYNC_WORKERS>1; the handbook FILE WRITES below stay strictly serial and
    # deterministic (same ordering as before), so nothing races on a chapter file.
    failed_quals: set[str] = set()

    # 1) collect jobs, preserving each qual's OWN-chapter-first stage ordering ----------
    #    OWN-chapter sids first: a sid without its own chapter file lands in a sibling
    #    HOST file, and must never steal that host's in-place card slot — the host's
    #    existing same-slug card belongs to a DIFFERENT stage. Host placements append.
    q_order: dict[str, list[str]] = {}                 # qual -> ordered [sid, ...]
    jobs: dict[tuple[str, str], list[dict]] = {}       # (qual, sid) -> sorted members
    for q in sorted(retranslate_quals):
        by_sid: dict[str, list[dict]] = {}
        for sid, st in (mapping.get("stages") or {}).items():
            for m in st.get("members") or []:
                if m.get("qualname") == q and m.get("type") in ("function", "region") \
                        and m.get("line_range"):
                    by_sid.setdefault(sid, []).append(m)
        if not by_sid:
            continue                                   # fell through to unmapped
        ordered = sorted(
            by_sid.items(),
            key=lambda kv: (not (hb_dir / "stages" / f"{kv[0]}.md").exists(), kv[0]))
        q_order[q] = [sid for sid, _ in ordered]
        for sid, mems in ordered:
            jobs[(q, sid)] = sorted(mems, key=lambda m: m["line_range"][0])

    # 2) build each card (parallel). A CHANGED function's card is MINIMAL-PATCHED from its
    #    existing card (assumed correct); new / renamed / patch-failed fall back to a full
    #    translation. Both are pure calls → safe to run concurrently (RESYNC_WORKERS).
    #    Disable the patch path entirely with RESYNC_MINIMAL_PATCH=0 (always full translate).
    changed_set = set(changed)
    patch_on = os.environ.get("RESYNC_MINIMAL_PATCH", "1").lower() not in ("0", "false", "off")
    cards: dict[tuple[str, str], str] = {}
    terr: dict[tuple[str, str], Exception] = {}
    patched_keys: set[tuple[str, str]] = set()

    def _xlate(key: tuple[str, str]):
        q, sid = key
        mems = jobs[key]
        # minimal-patch path: a changed function that still has an existing card + both spans
        if patch_on and q in changed_set and q not in new_defs:
            fname = mems[0]["file"]
            so = spans_old.get(fname, {}).get(q)
            sn = spans_new.get(fname, {}).get(q)
            old_card = _old_card_text(hb_dir, sid, mapping, q)
            if old_card and so and sn:
                try:
                    diff = _func_unified_diff(lines_old[fname], so,
                                              lines_new[fname], sn, fname)
                    cand = _patch_card(old_card, diff, q)
                    if _card_struct_ok(cand, _slug(q), q):
                        return key, cand, None, True
                except Exception:  # noqa: BLE001  — any failure → fall back to full translate
                    pass
        try:
            return key, _translate_card(sid, [dict(m) for m in mems], code_dir,
                                        skeleton, synopses), None, False
        except Exception as e:  # noqa: BLE001
            return key, None, e, False

    workers = max(1, int(os.environ.get("RESYNC_WORKERS", "1")))
    if workers == 1 or len(jobs) <= 1:
        results = [_xlate(k) for k in jobs]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_xlate, list(jobs)))
    for key, card, err, patched in results:
        if err is None:
            cards[key] = card
            if patched:
                patched_keys.add(key)
        else:
            terr[key] = err

    # 3) place cards into chapter files — SERIAL, deterministic ------------------------
    patched_quals: set[str] = set()
    for q in sorted(q_order):
        slug = _slug(q)
        placed: set[Path] = set()
        failed = wrote = False
        for sid in q_order[q]:
            key = (q, sid)
            if key in terr:
                failed = True
                report["errors"].append(f"{q}@{sid}: translation failed: {terr[key]!r}")
                continue
            card = cards[key]
            target = _chapter_file(hb_dir, sid, mapping, q) \
                or (_card_files(hb_dir, slug) or [None])[0]
            if target is None:
                report["errors"].append(f"{q}@{sid}: no chapter file for the card")
                continue
            is_own_chapter = target.name == f"{sid}.md"
            text = target.read_text()
            if is_own_chapter and f'<details id="{slug}">' in text \
                    and target not in placed:
                target.write_text(_card_re(slug).sub(
                    lambda _m: card.rstrip() + "\n", text, count=1))
            else:
                with target.open("a") as fh:
                    fh.write(f"\n<!-- card placed by resync (stage {sid}) -->\n\n"
                             f"{card.rstrip()}\n")
            placed.add(target)
            if key in patched_keys:
                patched_quals.add(q)
            wrote = True
        if failed:
            failed_quals.add(q)
        if placed and not failed:
            # cards left in chapters this function no longer belongs to
            for f in _card_files(hb_dir, slug):
                if f not in placed:
                    f.write_text(_card_re(slug).sub("", f.read_text(), count=1))
        if wrote:
            (report["cards_patched"] if q in patched_quals
             else report["cards_translated"]).append(q)

    if failed_quals:
        # a failed translation keeps the OLD card: roll its anchors NOW (it was skipped
        # in the main anchor pass on the assumption a fresh card would replace it) and
        # list the card as still owed
        report["cards_pending"] = sorted(set(report["cards_pending"]) | failed_quals)
        report["anchors_refreshed"] += _anchor_pass(failed_quals, set())

    if patched_quals:
        # minimal-patched cards keep the OLD line anchor (the LLM is told not to touch it) →
        # roll it to the new range now (these quals were skipped in the main anchor pass).
        report["anchors_refreshed"] += _anchor_pass(patched_quals, set())

    # registers' code-sites against the edited tree; index from the current files
    reg_md = hb_dir / "registers.md"
    if reg_md.exists():
        text, _n = bhs._enrich_registers(reg_md.read_text(), root=code_dir)
        reg_md.write_text(text + "\n")
    bhs.rebuild_index(hb_dir)

    # ── end checks (mechanical; red goes to the report, never blocks) ─────────
    chk = {"sha_mismatch": [], "entry_without_card": [], "card_without_entry": []}
    mapped: set[str] = set()
    for sid, st in (mapping.get("stages") or {}).items():
        for m in st.get("members") or []:
            if m.get("type") not in ("function", "region") or not m.get("line_range"):
                continue
            q = m["qualname"]
            mapped.add(q)
            fname = m["file"]
            old_q = next((a for a, b in renames.items() if b == q), q)
            if fname in bad_files or verdict.get(old_q) == "unparsable":
                continue
            a, b = m["line_range"]
            cur = lines_new.get(fname)
            if cur is None or b > len(cur) \
                    or (m.get("sha1") and _sha1("\n".join(cur[a - 1:b])) != m["sha1"]):
                chk["sha_mismatch"].append(f"{q} at {fname}:{a}-{b}")
    card_quals = set()
    for f in _stage_files(hb_dir):
        for mm in re.finditer(r"<summary><b>([\w.]+)</b>", f.read_text()):
            card_quals.add(mm.group(1))
    frozen = {q for q, v in verdict.items() if v == "unparsable"}
    pending = set(report["cards_pending"])      # translation OFF: cards owed, not bugs
    chk["entry_without_card"] = sorted(mapped - card_quals - frozen - pending)
    chk["card_without_entry"] = sorted(card_quals - mapped - frozen)
    chk["ok"] = not any(v for k, v in chk.items() if k != "ok")
    report["check"] = chk
    return report
