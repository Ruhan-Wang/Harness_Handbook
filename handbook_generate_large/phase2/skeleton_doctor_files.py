# -*- coding: utf-8 -*-
"""skeleton_doctor_files.py — file-level skeleton doctor (Phase 2b Step B surgeon).

A FILE-LEVEL port of phase2/pass_c_skeleton_doctor.py. The function-level doctor
looks at a per-function `mapping_doc` and proposes structural skeleton changes
(split / merge / add / remove stage) through a 3-critic approval gate. Here the
bucket members are whole FILES (from file_assign), not functions, and there is an
extra, computable convergence signal the function-level version lacks: the
`coverage.unassigned` list. The doctor's job each round is to evolve the skeleton
so that list empties out and no stage is overloaded.

What this module does, all on the api_client `Api` (same endpoint as the rest of
Phase 2):
  - compute_file_stage_stats   — per-stage size + dominant-dir + global unassigned
  - run_doctor_files           — one actor-multi-critic round; applies approved,
                                 validated structural changes to skeleton_doc
  - apply_change_files         — pure-dict edits to skeleton_doc's stage list;
                                 returns the set of files whose assignment is now
                                 stale and must be re-assigned
  - reassign_subset            — re-run file_assign for just those files and merge
                                 back into the previous assign_result

It REUSES phase2/critic.py's actor_multi_critic_loop verbatim, and
file_assign's internal batch helpers for the subset re-assignment. It does NOT
touch mapping_doc or phase2/apply.py — the file-level bucket model is rebuilt by
re-running file_assign, which is simpler than threading member moves.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import os
import sys
import threading
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "shared"))

from api_client import Api  # noqa: E402
from critic import (  # noqa: E402
    Verdict,
    _normalize_vacuous_revise,
    build_revise_prompt,
    call_actor,
    call_critic,
)

import file_assign  # noqa: E402
import nav_pack as navmod  # noqa: E402
import synth_stages  # noqa: E402
from skeleton_yaml import stage_short_descriptions  # noqa: E402

logger = logging.getLogger(__name__)

# A stage with more files than this (and dominated by one directory) is flagged
# as an overload candidate in the actor prompt. This is a HINT, not a hard gate —
# the actor decides; the buckets are also shown in full so it can judge.
_OVERLOAD_HINT = 20

# Critic roles applied to every actor-critic exchange in this module.
_CRITIC_ROLES = ["engineer", "architect", "reader"]

# Global cap on concurrent LLM calls from THIS module. The doctor nests two
# thread pools — the per-round diagnosis pool (doctor_workers tasks) and, inside
# each task, the critic pool (len(roles) critics). Without a shared cap the peak
# concurrency is doctor_workers × len(roles) (e.g. 8 × 3 = 24), which silently
# triples whatever the user set and can saturate the endpoint / trip rate limits.
# Every api.call() in this module goes through _llm_call(), which acquires this
# semaphore, so the true ceiling is _LLM_CONCURRENCY regardless of pool nesting.
# Override via set_llm_concurrency() (run.py wires it to --doctor-llm-workers).
_LLM_CONCURRENCY = 12
_llm_semaphore = threading.BoundedSemaphore(_LLM_CONCURRENCY)


def set_llm_concurrency(n: int) -> None:
    """Reset the global cap on concurrent LLM calls made by this module.

    Called once before a run (single-threaded) to size the shared semaphore.
    """
    global _LLM_CONCURRENCY, _llm_semaphore
    _LLM_CONCURRENCY = max(1, int(n))
    _llm_semaphore = threading.BoundedSemaphore(_LLM_CONCURRENCY)


def _with_llm_cap(fn):
    """Wrap a 0-arg LLM-calling thunk so it holds the global semaphore while it
    runs. Keeps total concurrent api.call()s across both nested pools <= the cap.

    Captures the semaphore object once, so acquire and release always hit the
    SAME instance even if set_llm_concurrency() rebinds the module global
    mid-flight — otherwise the release would land on a different semaphore than
    the acquire (over-releasing the new one, leaking a permit on the old).
    """
    sem = _llm_semaphore
    sem.acquire()
    try:
        return fn()
    finally:
        sem.release()


def _dirname(file_path: str) -> str:
    d = os.path.dirname(file_path)
    return d or "."


# ─── Statistics ──────────────────────────────────────────────────────────────


def compute_file_stage_stats(skeleton_doc: dict, assign_result: dict) -> dict:
    """Per-stage size + dominant-directory share, plus the global unassigned set.

    assign_result is what file_assign.assign_files returns:
        {"file_stage": {f: {stage, also}}, "buckets": {sid: [f,...]},
         "coverage": {n_files, n_assigned, unassigned: [...]}}

    A stage is flagged `overloaded` when its file count is BOTH absolutely large
    (> _OVERLOAD_HINT) and relatively large for this codebase (> 2.5x the mean
    non-empty bucket). The relative term is what makes this scale: on a big repo
    every bucket can exceed 20 files, so a fixed cutoff would flag almost all of
    them; the multiple-of-mean keeps the flag on the genuine outliers. A high
    single-directory share is reported (dominant_dir_share) as a hint for the
    actor's split decision, but is NOT required for the flag — a 200-file bucket
    spread across many dirs is still overloaded.
    """
    buckets = assign_result.get("buckets", {})
    sizes = [len(buckets.get(s["id"], [])) for s in skeleton_doc.get("stages", [])]
    nonempty = [n for n in sizes if n > 0]
    mean_bucket = (sum(nonempty) / len(nonempty)) if nonempty else 0.0
    overload_floor = max(_OVERLOAD_HINT, 2.5 * mean_bucket)
    per_stage: dict[str, dict] = {}
    for sid in (s["id"] for s in skeleton_doc.get("stages", [])):
        files = buckets.get(sid, [])
        dir_counts = Counter(_dirname(f) for f in files)
        dom_dir, dom_n = dir_counts.most_common(1)[0] if dir_counts else ("", 0)
        per_stage[sid] = {
            "n_files": len(files),
            "dir_distribution": dict(dir_counts),
            "dominant_dir": dom_dir,
            "dominant_dir_share": (dom_n / max(len(files), 1)),
            "overloaded": len(files) > overload_floor,
        }
    coverage = assign_result.get("coverage", {})
    return {
        "per_stage": per_stage,
        "n_unassigned": len(coverage.get("unassigned", [])),
        "unassigned": list(coverage.get("unassigned", [])),
        "n_files": coverage.get("n_files", 0),
    }


def _render_unassigned(unassigned: list[str], purposes: dict[str, dict] | None,
                       *, cap: int = 40) -> str:
    if not unassigned:
        return "  (none — every file is assigned)"
    lines = []
    for f in unassigned[:cap]:
        p = (purposes or {}).get(f, {})
        purpose = p.get("purpose", "")
        lines.append(f"  - {f}" + (f"  — {purpose}" if purpose else ""))
    if len(unassigned) > cap:
        lines.append(f"  ... and {len(unassigned) - cap} more")
    return "\n".join(lines)


def _render_stats(skeleton_doc: dict, stats: dict) -> str:
    """Shared ground-truth block: skeleton tree + per-stage file distribution +
    the unassigned tail. Given to BOTH the actor and the critics."""
    skel_lines = []
    for s in skeleton_doc.get("stages", []):
        parent = s.get("parent") or "(top)"
        kids = len(s.get("children", []))
        cc = " [crosscut]" if s.get("crosscut") else ""
        desc1 = (s.get("description") or "").split(". ")[0][:80]
        skel_lines.append(
            f"  {s['id']:<22} parent={parent:<18} children={kids}{cc} — {desc1}"
        )

    stat_lines = []
    for sid, s in sorted(stats["per_stage"].items()):
        dom = (f"dom dir: {s['dominant_dir']} "
               f"({s['dir_distribution'].get(s['dominant_dir'], 0)}/{s['n_files']} "
               f"= {s['dominant_dir_share']:.0%})") if s["dominant_dir"] else "no files"
        flag = "  <OVERLOAD?>" if s["overloaded"] else ""
        stat_lines.append(f"  {sid:<22} files={s['n_files']:>3}  {dom}{flag}")

    return "\n".join([
        f"Skeleton: {len(skeleton_doc.get('stages', []))} stages, "
        f"{stats['n_files']} files total, {stats['n_unassigned']} UNASSIGNED.",
        "",
        "## Current skeleton",
        "\n".join(skel_lines) or "  (no stages)",
        "",
        "## File distribution per stage",
        "\n".join(stat_lines) or "  (no stages)",
        "",
        "## Unassigned files (these MUST be given a home)",
        _render_unassigned(stats["unassigned"], stats.get("_purposes")),
    ])


# ─── Actor prompt ─────────────────────────────────────────────────────────────


# Appended to any actor prompt when lang="zh": the structural rules/schema stay
# English (they drive validation), but any stage title/description the model
# WRITES must be Chinese, since those strings show up verbatim in the handbook.
_ZH_NOTE = ("\n\nIMPORTANT (language): write every new stage's \"title\" and "
            "\"description\" in CHINESE (中文). Keep all JSON keys, stage ids, "
            "action names, and file paths exactly as-is (English/unchanged).")


_ACTOR_RULES = """You are the SKELETON DOCTOR for a system handbook. The handbook's leaf node is
the SOURCE FILE: every file is assigned to exactly one stage. You see the current
skeleton, how many files landed in each stage, and the files that landed NOWHERE
(unassigned). Propose **at most 3** structural changes to the skeleton that make
the partition correct and balanced.

WHAT TO LOOK FOR (in priority order)
1. UNASSIGNED FILES: any file in the unassigned list has no home. This is the
   primary problem to fix.
   -> Propose `add_stage` for a coherent group of them (e.g. a subsystem or a
      crosscut like config/types/utils), OR widen an existing stage by changing
      its description/scope so it clearly covers them. Look at the unassigned
      files' paths and purposes to find the natural grouping.
2. STAGE OVERLOAD: a stage carrying far more files than its siblings — look for
   the <OVERLOAD?> flag (set when a bucket is much larger than the average), and
   for a high single-directory share ("dom dir").
   -> Propose `split_stage` to extract a subsystem-internal substage.
3. STAGE STARVATION: sibling substages with 0-1 files that share a parent.
   -> Propose `merge_stages` into the parent or a sibling.
4. DEAD STAGES: a stage with 0 files whose role is genuinely redundant.
   -> Propose `remove_stage` (with move_to if it somehow still has files).

WHAT NOT TO DO
- Do NOT split or merge for purely cosmetic reasons.
- Do NOT touch a stage that already has a healthy, well-distributed set of files
  unless it is overloaded.
- Do NOT propose more than 3 changes per invocation.
- Prefer widening a stage's scope (re-describe) over adding a near-duplicate
  stage, when the unassigned files plausibly belong to an existing stage.

ACTIONS (output schema). Members are FILE PATHS.

{"action": "add_stage",
  "new_stage": {"id": "<unique id, e.g. crosscut-config or stage-7.1>",
                 "title": "...", "description": "...",
                 "parent": "<parent id or null>", "crosscut": false},
  "move_files": [{"file": "<exact path>", "from_stage": "<id|unassigned>"}, ...]}

{"action": "remove_stage", "stage_id": "...", "move_to": "<target id or null>"}

{"action": "merge_stages", "stages_to_merge": ["sid1","sid2",...],
  "into": "<target id, may be one of stages_to_merge>"}

{"action": "split_stage", "source_stage": "...",
  "new_stages": [{"id": "...", "title": "...", "description": "...",
                   "parent": "<usually source_stage>",
                   "files": ["<path1>","<path2>",...]}, ...]}

NOTES
- `add_stage` move_files may pull from "unassigned" or from any existing stage.
  Every named file must currently be unassigned or a member of the named
  from_stage.
- `split_stage` MUST move at least one file into a new (non-source) stage; every
  file listed must currently be a member of source_stage.
- Use "stage-N.M" ids for substages and set their parent.

OUTPUT — return ONLY a single JSON block:
```json
{"changes": [<change>, ...], "rationale": "<one paragraph>"}
```
If the skeleton is already healthy AND nothing is unassigned, return:
```json
{"changes": [], "rationale": "All files assigned; distribution balanced."}
```"""


_PROPOSAL_SCHEMA_HINT = """{
  "changes": [
    {"action": "add_stage|remove_stage|merge_stages|split_stage", ...},
    ... (at most 3)
  ],
  "rationale": "..."
}"""


def _build_actor_prompt(skeleton_doc: dict, stats: dict, lang: str = "en") -> str:
    return "\n".join([
        _ACTOR_RULES + (_ZH_NOTE if lang == "zh" else ""),
        "",
        _render_stats(skeleton_doc, stats),
        "",
        "Return the JSON block.",
    ])


# ─── Focused prompts for parallel diagnosis ──────────────────────────────────
#
# When run_doctor_files fans out over a thread pool, each worker owns a DISJOINT
# slice of the problem so the proposals can never collide:
#   - one worker per overloaded stage, allowed ONLY to split THAT stage;
#   - one global worker, allowed only to add/merge/remove to absorb the
#     unassigned tail and clean up starved/dead stages (never a split).
# Disjoint scopes mean the parallel proposals touch disjoint files/ids, so they
# can all be applied serially afterward without conflicting.


_SPLIT_RULES = """You are the SKELETON DOCTOR for a system handbook, focused on ONE overloaded
stage. The handbook's leaf node is the SOURCE FILE; this stage carries far more
files than its siblings. Propose how to SPLIT it into the source stage plus one
or more new substages, so each resulting stage is a coherent, right-sized group.

RULES
- You may ONLY propose `split_stage` changes whose source_stage is the target
  stage named below. Do NOT touch any other stage. Do NOT add/merge/remove.
- Every new substage id must be of the form "<source>.N" (e.g. "stage-16.1") and
  must NOT collide with any existing stage id.
- Every file you list in a new substage MUST currently be a member of the target
  stage (they are listed below). At least one file must move to a new substage.
- Group files by what they do / their directory, using the purposes shown.
- Propose 0 changes (empty list) if the stage is actually coherent and shouldn't
  be split.

OUTPUT — return ONLY a single JSON block:
```json
{"changes": [
  {"action": "split_stage", "source_stage": "<target>",
   "new_stages": [{"id": "<target>.1", "title": "...", "description": "...",
                   "parent": "<target>", "files": ["<path>", ...]}, ...]}
], "rationale": "<one paragraph>"}
```"""


_GLOBAL_RULES = """You are the SKELETON DOCTOR for a system handbook, focused on COVERAGE and
cleanup (NOT splitting). The handbook's leaf node is the SOURCE FILE. Your job:
give every UNASSIGNED file a home, and clean up starved/dead stages.

RULES
- You may propose `add_stage`, `merge_stages`, and `remove_stage` ONLY. Do NOT
  propose `split_stage` (a separate pass handles overloaded stages).
- UNASSIGNED files (listed below) are the priority: propose `add_stage` for a
  coherent group of them (e.g. a subsystem or a crosscut like config/types/
  utils), pulling them from "unassigned" via move_files.
- STARVATION: sibling substages with 0-1 files that share a parent -> merge.
- DEAD: a stage with 0 files whose role is genuinely redundant -> remove (with
  move_to if it still has files).
- New stage ids must not collide with existing ids. At most 3 changes.

OUTPUT — return ONLY a single JSON block:
```json
{"changes": [<add_stage|merge_stages|remove_stage>, ...], "rationale": "..."}
```
If nothing is unassigned and no stage is starved/dead, return:
```json
{"changes": [], "rationale": "Coverage complete; no cleanup needed."}
```"""


def _build_split_prompt(skeleton_doc: dict, stats: dict, sid: str,
                        bucket_files: list[str],
                        purposes: dict[str, dict] | None, lang: str = "en") -> str:
    stage = next((s for s in skeleton_doc["stages"] if s["id"] == sid), None)
    title = stage["title"] if stage else sid
    desc = (stage["description"] if stage else "")
    lines = [f"- {f}" + (f"  — {(purposes or {}).get(f, {}).get('purpose', '')}"
                         if purposes else "") for f in bucket_files]
    return "\n".join([
        _SPLIT_RULES + (_ZH_NOTE if lang == "zh" else ""),
        "",
        f"## Target stage to split: {sid} — {title}",
        f"Description: {desc}",
        f"It holds {len(bucket_files)} files (far above the average bucket).",
        "",
        "## Files currently in this stage (split among new substages)",
        "\n".join(lines) or "  (none)",
        "",
        "Return the JSON block.",
    ])


def _build_global_prompt(skeleton_doc: dict, stats: dict, lang: str = "en") -> str:
    return "\n".join([
        _GLOBAL_RULES + (_ZH_NOTE if lang == "zh" else ""),
        "",
        _render_stats(skeleton_doc, stats),
        "",
        "Return the JSON block.",
    ])


# ─── Per-change validation (mechanical) ──────────────────────────────────────


def _bucket_files(assign_result: dict, sid: str) -> set[str]:
    return set(assign_result.get("buckets", {}).get(sid, []))


def _unassigned_set(assign_result: dict) -> set[str]:
    return set(assign_result.get("coverage", {}).get("unassigned", []))


def _validate_change(change: dict, skeleton_doc: dict, assign_result: dict,
                     protected_stages: set[str] | None = None) -> str | None:
    """Mechanical sanity check before applying. Returns an error string or None.

    Mirrors the function-level doctor's guardrails (the comments there document
    the exact failure modes each check prevents): no phantom file references, a
    split must actually move files, a non-empty stage can't be removed without a
    target, move_to != stage_id, ids unique.

    `protected_stages` (parallel diagnosis only): stage ids that another concurrent
    task owns this round (e.g. an overloaded stage being split). A change must not
    remove/merge-away or split a protected stage — this enforces the disjoint-scope
    guarantee the parallel orchestration relies on, since apply order across tasks
    is nondeterministic and two tasks editing the same stage would corrupt it.
    """
    action = change.get("action")
    skel_ids = {s["id"] for s in skeleton_doc.get("stages", [])}
    protected = protected_stages or set()

    if action == "add_stage":
        spec = change.get("new_stage", {})
        if not isinstance(spec, dict) or not spec.get("id"):
            return "add_stage missing new_stage.id"
        if spec["id"] in skel_ids:
            return f"add_stage id '{spec['id']}' already exists"
        for mv in change.get("move_files", []) or []:
            if not isinstance(mv, dict):
                return "add_stage move_files entries must be dicts"
            f = mv.get("file")
            frm = mv.get("from_stage")
            if not isinstance(f, str) or not f:
                return "add_stage move_files.file must be a non-empty string"
            if frm in protected:
                return (f"add_stage move_files pulls from protected stage '{frm}' "
                        f"(owned by a concurrent split this round)")
            if frm == "unassigned":
                if f not in _unassigned_set(assign_result):
                    return f"add_stage move_files file '{f}' is not unassigned"
            elif frm in skel_ids:
                if f not in _bucket_files(assign_result, frm):
                    return (f"add_stage move_files file '{f}' is not a member of "
                            f"from_stage '{frm}'")
            else:
                return f"add_stage move_files.from_stage '{frm}' unknown"

    elif action == "remove_stage":
        sid = change.get("stage_id")
        if sid not in skel_ids:
            return f"remove_stage id '{sid}' not in skeleton"
        if sid in protected:
            return (f"remove_stage target '{sid}' is protected (a concurrent split "
                    f"owns it this round)")
        mt = change.get("move_to")
        if mt is not None and mt not in skel_ids:
            return f"remove_stage move_to '{mt}' unknown"
        if mt is not None and mt == sid:
            return f"remove_stage move_to == stage_id ('{sid}')"
        n_files = len(_bucket_files(assign_result, sid))
        if n_files > 0 and not mt:
            return (f"remove_stage of '{sid}' (has {n_files} files) requires "
                    f"non-null 'move_to'")

    elif action == "merge_stages":
        srcs = change.get("stages_to_merge", [])
        target = change.get("into")
        if not target:
            return "merge_stages missing 'into'"
        if not isinstance(srcs, list) or not srcs:
            return "merge_stages stages_to_merge must be a non-empty list"
        for s in srcs:
            if s not in skel_ids:
                return f"merge_stages source '{s}' unknown"
            if s in protected:
                return (f"merge_stages source '{s}' is protected (a concurrent "
                        f"split owns it this round)")
        if target in protected:
            return (f"merge_stages target '{target}' is protected (a concurrent "
                    f"split owns it this round)")
        if target not in skel_ids and target not in srcs:
            return f"merge_stages 'into' target '{target}' unknown"

    elif action == "split_stage":
        src = change.get("source_stage")
        if src not in skel_ids:
            return f"split_stage source '{src}' unknown"
        new_stages = change.get("new_stages", [])
        if not new_stages:
            return "split_stage needs at least one new_stage"
        src_files = _bucket_files(assign_result, src)
        seen_new: set[str] = set()
        for spec in new_stages:
            if not isinstance(spec, dict):
                return "split_stage new_stages entries must be dicts"
            nid = spec.get("id")
            if not nid:
                return "split_stage new_stage missing 'id'"
            # A new (non-source) substage id must not collide with an existing
            # stage or another new one — otherwise _normalize silently renames it
            # (e.g. 'stage-2' -> 'stage-2-3') and the files routed to the intended
            # id never land there. (add_stage has the same guard.)
            if nid != src:
                if nid in skel_ids:
                    return (f"split_stage new_stage id '{nid}' already exists in "
                            f"the skeleton; pick a fresh id (e.g. '{src}.1')")
                if nid in seen_new:
                    return f"split_stage repeats new_stage id '{nid}'"
                seen_new.add(nid)
            for f in spec.get("files", []) or []:
                if f not in src_files:
                    return (f"split_stage new_stage '{nid}' references "
                            f"file '{f}' not in source stage '{src}'")
        non_source_new = [s for s in new_stages if s.get("id") != src]
        if non_source_new and not any(s.get("files") for s in non_source_new):
            return (f"split_stage on '{src}' creates new stage(s) "
                    f"{[s.get('id') for s in non_source_new]} but moves no files")

    else:
        return f"unknown action: {action}"

    return None


# ─── Apply: pure-dict edits to the skeleton stage list ───────────────────────


def _stage_index(skeleton_doc: dict, sid: str) -> int:
    for i, s in enumerate(skeleton_doc["stages"]):
        if s["id"] == sid:
            return i
    return -1


def apply_change_files(skeleton_doc: dict, change: dict, assign_result: dict
                       ) -> set[str]:
    """Apply one validated structural change to skeleton_doc's stage list IN PLACE.

    Returns the set of files whose stage assignment is now stale and must be
    re-assigned next: files in removed/merged-away/split buckets, plus any files
    the change explicitly names. The buckets themselves are NOT edited here — the
    caller re-derives them by re-running file_assign on the affected files.

    Note on `remove_stage`/`merge_stages`: the displaced files go into `affected`
    and are re-assigned purpose-aware against the new skeleton, so a `move_to`
    target on a remove_stage is advisory only (it gates validation — a non-empty
    stage can't be dropped without one — but the files are re-classified, not
    blindly redirected). This is intentional: re-running file_assign places each
    file by what it does, which is more accurate than a bulk redirect.

    Pure dict manipulation; calls synth_stages._normalize at the end to re-wire
    children/parents and guarantee canonical shape.
    """
    action = change.get("action")
    affected: set[str] = set()
    stages = skeleton_doc["stages"]

    if action == "add_stage":
        spec = change["new_stage"]
        stages.append({
            "id": spec["id"],
            "title": spec.get("title") or spec["id"],
            "description": (spec.get("description") or spec.get("title") or spec["id"]),
            "parent": spec.get("parent"),
            "children": [],
            "crosscut": bool(spec.get("crosscut")),
        })
        for mv in change.get("move_files", []) or []:
            affected.add(mv["file"])

    elif action == "remove_stage":
        sid = change["stage_id"]
        affected |= _bucket_files(assign_result, sid)
        idx = _stage_index(skeleton_doc, sid)
        if idx >= 0:
            stages.pop(idx)
        # re-parent any children of the removed stage to top-level
        for s in stages:
            if s.get("parent") == sid:
                s["parent"] = None

    elif action == "merge_stages":
        srcs = change["stages_to_merge"]
        target = change["into"]
        for s in srcs:
            affected |= _bucket_files(assign_result, s)
        # Drop the merged-away stages (everything except the target).
        drop = {s for s in srcs if s != target}
        skeleton_doc["stages"] = [s for s in stages if s["id"] not in drop]
        for s in skeleton_doc["stages"]:
            if s.get("parent") in drop:
                s["parent"] = target

    elif action == "split_stage":
        src = change["source_stage"]
        affected |= _bucket_files(assign_result, src)
        for spec in change.get("new_stages", []) or []:
            if spec.get("id") == src:
                continue  # source stage stays; its description may be refined below
            stages.append({
                "id": spec["id"],
                "title": spec.get("title") or spec["id"],
                "description": (spec.get("description") or spec.get("title") or spec["id"]),
                "parent": spec.get("parent") or src,
                "children": [],
                "crosscut": False,
            })
            for f in spec.get("files", []) or []:
                affected.add(f)
        # If the actor re-described the source stage in new_stages, apply it.
        for spec in change.get("new_stages", []) or []:
            if spec.get("id") == src and spec.get("description"):
                si = _stage_index(skeleton_doc, src)
                if si >= 0:
                    skeleton_doc["stages"][si]["description"] = spec["description"]

    # Re-normalize: re-wire children from parents, keep canonical shape. Preserve
    # metadata (don't let _normalize stamp drafted_by over the agent's metadata).
    meta = dict(skeleton_doc.get("metadata", {}))
    renorm = synth_stages._normalize({"metadata": meta, "stages": skeleton_doc["stages"]})
    skeleton_doc["stages"] = renorm["stages"]
    skeleton_doc["metadata"] = {**meta, **renorm["metadata"], "drafted_by": meta.get("drafted_by", "synth_agent")}
    return affected


# ─── Parallel actor-critic (does NOT touch phase2/critic.py) ─────────────────


def _run_critics_parallel(api: Api, roles: list[str], task_context: str,
                          proposal: dict, proposal_schema_hint: str,
                          review_evidence: str,
                          prev_verdicts: list[Verdict] | None = None,
                          ) -> list[Verdict]:
    """Run every critic in `roles` concurrently and return their verdicts in role
    order. Mirrors the per-critic logic of phase2/critic.py.actor_multi_critic_loop
    (broken critic -> conservative REJECT; vacuous REVISE -> APPROVE) but fans the
    calls out over a thread pool instead of the serial `for role` loop.

    `prev_verdicts` (round 2 only) gives each critic its own round-1 verdict so it
    can judge whether the revision addressed its concerns — same context string
    the serial loop builds.
    """
    def _one(idx: int, role: str) -> tuple[int, Verdict]:
        # Any failure in here MUST resolve to a conservative REJECT, never an
        # exception: a raised exception would re-raise at fut.result(), abort the
        # as_completed loop, abandon the other futures, and crash the whole round.
        # The serial loop treats a broken critic as REJECT; we do the same for any
        # error (bad prev_verdicts index, normalize failure, etc.), so the result
        # list always has exactly len(roles) verdicts.
        try:
            ctx = task_context
            if prev_verdicts is not None:
                pv = prev_verdicts[idx]
                ctx = task_context + (
                    f"\n\nNote: this is round 2 of review. In round 1 you (role={role}) "
                    f"returned: decision={pv.decision}; concerns={pv.concerns!r}. The "
                    f"Actor revised in response. Now judge whether the revised proposal "
                    f"addresses these concerns."
                )
            v = _with_llm_cap(lambda: call_critic(api, role, ctx, proposal,
                                                  proposal_schema_hint, review_evidence))
            if v is None:
                v = Verdict(decision="REJECT",
                            concerns=[f"Critic with role={role} failed to respond"],
                            suggested_revision=None, rationale="critic_call_failed")
            return idx, _normalize_vacuous_revise(v, role)
        except Exception as e:  # noqa: BLE001
            logger.warning("critic(%s) raised in _one: %s — treating as REJECT",
                           role, e)
            return idx, Verdict(decision="REJECT",
                                concerns=[f"Critic {role} raised: {e}"],
                                suggested_revision=None, rationale="critic_exception")

    verdicts: list[Verdict | None] = [None] * len(roles)
    with cf.ThreadPoolExecutor(max_workers=max(1, len(roles))) as pool:
        for fut in cf.as_completed(
                pool.submit(_one, i, r) for i, r in enumerate(roles)):
            # _one never raises, so fut.result() is safe; guard anyway so an
            # executor-level failure (e.g. thread spawn) can't abort collection.
            try:
                idx, v = fut.result()
                verdicts[idx] = v
            except Exception as e:  # noqa: BLE001
                logger.warning("critic future failed unexpectedly: %s", e)
    # Backfill any slot a future somehow left empty with a conservative REJECT, so
    # the caller ALWAYS sees exactly len(roles) verdicts — never a short list that
    # could let `all(is_approve)` pass on a partially-reviewed proposal.
    return [v if v is not None else Verdict(
                decision="REJECT", concerns=[f"Critic {roles[i]} produced no verdict"],
                suggested_revision=None, rationale="missing_verdict")
            for i, v in enumerate(verdicts)]


def parallel_actor_critic(api: Api, actor_prompt: str, *, task_context: str,
                          proposal_schema_hint: str = "",
                          review_evidence: str = "",
                          roles: list[str] | None = None,
                          max_revise_rounds: int = 1) -> dict | None:
    """A parallel-critic equivalent of critic.actor_multi_critic_loop.

    Returns the accepted proposal dict, or None if discarded. Logic matches the
    serial version exactly — actor proposes; all critics review (here, in
    parallel); all-APPROVE accepts; otherwise concerns are aggregated, the actor
    revises once, critics re-review in parallel, and the revision is accepted as
    long as no critic REJECTs. Only the critic fan-out is parallelized;
    phase2/critic.py is untouched.
    """
    roles = roles or _CRITIC_ROLES

    p1 = _with_llm_cap(lambda: call_actor(api, actor_prompt))
    if p1 is None:
        return None

    r1 = _run_critics_parallel(api, roles, task_context, p1,
                               proposal_schema_hint, review_evidence)
    if all(v.is_approve for v in r1):
        return p1
    if any(v.is_reject for v in r1) and max_revise_rounds < 1:
        return None

    # Aggregate concerns and ask the actor to revise once.
    aggregated = Verdict(
        decision="REVISE",
        concerns=[f"[{role}] {c}" for role, v in zip(roles, r1) for c in v.concerns],
        suggested_revision=None,
        rationale="aggregated concerns from multiple critics",
    )
    p2 = _with_llm_cap(lambda: call_actor(api, build_revise_prompt(actor_prompt, p1, aggregated)))
    if p2 is None:
        return None

    r2 = _run_critics_parallel(api, roles, task_context, p2,
                               proposal_schema_hint, review_evidence,
                               prev_verdicts=r1)
    # After 2 rounds: accept as long as no critic REJECTs (lingering REVISE OK).
    return p2 if not any(v.is_reject for v in r2) else None


# ─── Entry point: one doctor round ───────────────────────────────────────────


def _normalize_change_shape(change: dict, assign_result: dict) -> dict:
    """Loss-lessly reshape an `add_stage` change into the canonical form
    _validate_change / apply_change_files expect, tolerating the field drift the
    LLM actually emits. Returns a (possibly new) change dict; non-add_stage
    actions are returned unchanged.

    Observed GPT-5.4 drift on add_stage (verified live):
      - fields FLATTENED onto the change instead of nested under `new_stage`
        (id/title/description/parent/crosscut at the top level);
      - id under the alias `stage_id`; title under the alias `name`;
      - `parent: "top"` used as a literal for top-level (should be null);
      - `move_files` as a bare list of path STRINGS instead of
        {"file":..., "from_stage":...} objects.

    Only add_stage is normalized — split/merge/remove have shown no drift, so
    they pass through untouched (scope kept tight on purpose). The reshape is
    semantic-preserving: it only relocates/renames keys and fills `from_stage`
    from each file's CURRENT assignment, so a normalized change means exactly
    what the model proposed.
    """
    if not isinstance(change, dict) or change.get("action") != "add_stage":
        return change
    # Already canonical with a usable id → nothing to do.
    spec = change.get("new_stage")
    if isinstance(spec, dict) and spec.get("id"):
        out = dict(change)
        out["new_stage"] = _normalize_parent(dict(spec))
        out["move_files"] = _normalize_move_files(change.get("move_files"),
                                                  assign_result)
        return out

    # Reconstruct new_stage from flattened / aliased top-level fields.
    src = spec if isinstance(spec, dict) else change
    new_stage = {
        "id": src.get("id") or src.get("stage_id") or src.get("new_stage_id"),
        "title": src.get("title") or src.get("name") or "",
        "description": src.get("description") or "",
        "parent": src.get("parent"),
        "crosscut": bool(src.get("crosscut")),
    }
    out = dict(change)
    out["new_stage"] = _normalize_parent(new_stage)
    out["move_files"] = _normalize_move_files(change.get("move_files"),
                                              assign_result)
    return out


def _normalize_parent(spec: dict) -> dict:
    """`parent: "top"`/"none"/"" (literal top-level markers) → None."""
    p = spec.get("parent")
    if isinstance(p, str) and p.strip().lower() in ("top", "none", "null", ""):
        spec["parent"] = None
    return spec


def _normalize_move_files(move_files, assign_result: dict) -> list:
    """Coerce move_files into [{"file":..., "from_stage":...}] objects.

    When the model emits bare path strings (observed), `from_stage` is filled
    from each file's CURRENT assignment (its bucket id, or "unassigned"), which
    is exactly what the validator checks against — so the inferred value is
    correct by construction, not a guess. Entries already in object form keep
    their stated from_stage (only filling it in when absent)."""
    if not isinstance(move_files, list):
        return []
    file_stage = assign_result.get("file_stage", {})

    def _cur_stage(f: str) -> str:
        return (file_stage.get(f, {}) or {}).get("stage", "unassigned")

    out: list = []
    for mv in move_files:
        if isinstance(mv, str):
            out.append({"file": mv, "from_stage": _cur_stage(mv)})
        elif isinstance(mv, dict) and isinstance(mv.get("file"), str):
            frm = mv.get("from_stage") or _cur_stage(mv["file"])
            out.append({"file": mv["file"], "from_stage": frm})
        # anything else (malformed entry) is dropped — validator would reject it
    return out


def _apply_changes(skeleton_doc: dict, assign_result: dict, changes: list,
                   protected_stages: set[str] | None = None,
                   ) -> tuple[set[str], int, int]:
    """Validate + apply a list of proposed changes IN ORDER. Returns
    (affected_files, n_applied, n_rejected). Each change is validated against the
    skeleton as mutated by the changes before it (so id-collision guards still
    fire), and against the original bucket snapshot for file membership.

    `protected_stages` is forwarded to _validate_change to reject changes that
    touch a stage another concurrent task owns this round.
    """
    if not isinstance(changes, list):
        logger.warning("doctor: 'changes' is %s, expected list — treating as no "
                       "change", type(changes).__name__)
        return set(), 0, 0
    affected: set[str] = set()
    n_applied = n_rejected = 0
    for change in changes:
        if not isinstance(change, dict):
            logger.warning("doctor rejecting non-dict change: %r", change)
            n_rejected += 1
            continue
        # Reshape LLM field-drift (flattened/aliased add_stage) into the
        # canonical form before validating, so a semantically-valid proposal
        # isn't rejected on shape alone.
        change = _normalize_change_shape(change, assign_result)
        err = _validate_change(change, skeleton_doc, assign_result, protected_stages)
        if err:
            # Include the change's keys/action so persistent LLM schema drift is
            # diagnosable without a full raw dump (kept short on purpose).
            logger.warning("doctor rejecting change (validation): %s  [action=%s "
                           "keys=%s]", err, change.get("action"),
                           sorted(change.keys()))
            n_rejected += 1
            continue
        try:
            affected |= apply_change_files(skeleton_doc, change, assign_result)
            n_applied += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("doctor apply failed: %s", e)
            n_rejected += 1
    return affected, n_applied, n_rejected


def run_doctor_files(api: Api, skeleton_doc: dict, assign_result: dict,
                     *, purposes: dict[str, dict] | None = None,
                     max_revise_rounds: int = 1, doctor_workers: int = 1,
                     lang: str = "en") -> dict:
    """Run one file-level skeleton-doctor round.

    Mutates skeleton_doc in place when changes pass validation+critics. Returns:
        {"skeleton_changed": bool, "affected_files": set[str],
         "n_applied": int, "n_proposed": int, "n_rejected": int, "summary": str}

    doctor_workers:
      - 1 (default): a single global actor proposes up to 3 changes of any kind
        (split/merge/add/remove), reviewed by 3 critics. The critics run in
        parallel; the actor is one call. Equivalent in scope to the original
        single-actor round.
      - >1: PARALLEL diagnosis. The work is split into disjoint scopes that run
        concurrently — one split-only actor-critic per overloaded stage, plus one
        global add/merge/remove actor-critic for the unassigned tail and cleanup.
        Disjoint scopes (each overloaded bucket is distinct; the global pass never
        splits) guarantee the proposals touch disjoint files/ids, so the slow LLM
        calls fan out while the fast dict edits are applied serially afterward.
    """
    stats = compute_file_stage_stats(skeleton_doc, assign_result)
    if purposes is not None:
        stats["_purposes"] = purposes  # for the unassigned render
    task_context = (
        f"File-level skeleton doctor. {len(skeleton_doc.get('stages', []))} stages, "
        f"{stats['n_files']} files, {stats['n_unassigned']} unassigned."
    )

    if doctor_workers <= 1:
        # Single global actor, parallel critics.
        proposal = parallel_actor_critic(
            api, _build_actor_prompt(skeleton_doc, stats, lang),
            task_context=task_context,
            proposal_schema_hint=_PROPOSAL_SCHEMA_HINT,
            review_evidence=_render_stats(skeleton_doc, stats),
            max_revise_rounds=max_revise_rounds)
        changes = (proposal or {}).get("changes", []) if proposal else []
        affected, n_applied, n_rejected = _apply_changes(
            skeleton_doc, assign_result, changes)
        return {
            "skeleton_changed": n_applied > 0, "affected_files": affected,
            "n_applied": n_applied, "n_proposed": len(changes) if isinstance(changes, list) else 0,
            "n_rejected": n_rejected,
            "summary": (f"DoctorFiles[serial]: applied={n_applied}, "
                        f"rejected={n_rejected}, affected_files={len(affected)}"),
        }

    # ── Parallel diagnosis: disjoint scopes fanned out over a thread pool ──
    buckets = assign_result.get("buckets", {})
    overloaded = [sid for sid, s in stats["per_stage"].items() if s["overloaded"]]
    review_evidence = _render_stats(skeleton_doc, stats)

    # Each task is a (label, prompt, schema_hint) the worker runs an actor-critic
    # on; we collect their proposals first, then apply serially.
    def _split_task(sid: str):
        prompt = _build_split_prompt(skeleton_doc, stats, sid,
                                     buckets.get(sid, []), purposes, lang)
        return parallel_actor_critic(
            api, prompt, task_context=task_context,
            proposal_schema_hint=_PROPOSAL_SCHEMA_HINT,
            review_evidence=review_evidence, max_revise_rounds=max_revise_rounds)

    def _global_task():
        prompt = _build_global_prompt(skeleton_doc, stats, lang)
        return parallel_actor_critic(
            api, prompt, task_context=task_context,
            proposal_schema_hint=_PROPOSAL_SCHEMA_HINT,
            review_evidence=review_evidence, max_revise_rounds=max_revise_rounds)

    proposals: dict[str, dict] = {}   # label -> proposal
    with cf.ThreadPoolExecutor(max_workers=doctor_workers) as pool:
        futs = {pool.submit(_global_task): "global"}
        for sid in overloaded:
            futs[pool.submit(_split_task, sid)] = sid
        for fut in cf.as_completed(futs):
            label = futs[fut]
            try:
                p = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.warning("doctor parallel task %s failed: %s", label, e)
                p = None
            if p and isinstance(p.get("changes"), list):
                proposals[label] = p

    # Apply in a DETERMINISTIC order that enforces disjoint scope:
    #   1. splits first (each owns one overloaded stage; ids are "<sid>.N", so
    #      split proposals never collide with each other);
    #   2. then the global add/merge/remove pass, but with the overloaded stages
    #      PROTECTED — a global merge/remove that would swallow a just-split stage
    #      is rejected. (The scopes were meant to be disjoint, but a global pass
    #      CAN target a stage a split owns; protection makes that explicit instead
    #      of leaving the outcome to nondeterministic apply order.)
    affected: set[str] = set()
    n_applied = n_rejected = 0
    split_changes = [c for sid in overloaded if sid in proposals
                     for c in proposals[sid].get("changes", [])]
    a, ap, rj = _apply_changes(skeleton_doc, assign_result, split_changes)
    affected |= a
    n_applied += ap
    n_rejected += rj

    global_changes = proposals.get("global", {}).get("changes", [])
    a, ap, rj = _apply_changes(skeleton_doc, assign_result, global_changes,
                               protected_stages=set(overloaded))
    affected |= a
    n_applied += ap
    n_rejected += rj

    n_proposed = len(split_changes) + len(global_changes)
    return {
        "skeleton_changed": n_applied > 0, "affected_files": affected,
        "n_applied": n_applied, "n_proposed": n_proposed,
        "n_rejected": n_rejected,
        "summary": (f"DoctorFiles[parallel x{doctor_workers}: "
                    f"{len(overloaded)} split + 1 global]: applied={n_applied}, "
                    f"rejected={n_rejected}, affected_files={len(affected)}"),
    }


# ─── Subset re-assignment ────────────────────────────────────────────────────


def reassign_subset(api: Api, graph: dict, skeleton_doc: dict,
                    files_subset: set[str], prev_assign: dict,
                    *, purposes: dict[str, dict] | None = None,
                    batch_size: int = 25, max_workers: int = 6) -> dict:
    """Re-assign only `files_subset` against the (edited) skeleton and merge into
    `prev_assign`. Files outside the subset keep their previous assignment.

    Reuses file_assign's internal batch helpers so the assignment prompt/parse
    logic stays identical to the full pass. Returns a fresh assign_result dict
    ({file_stage, buckets, coverage}) with buckets/coverage recomputed.

    Even when `files_subset` is empty this rebuilds buckets/coverage against the
    current skeleton's stage ids, so a structural change that removed/merged a
    stage with no files to reassign still drops the now-stale bucket (otherwise a
    phantom bucket for the deleted stage would survive into file_stage.json).
    """
    import concurrent.futures as cf

    nav = navmod.build_nav_pack(graph)
    valid_ids = {s["id"] for s in skeleton_doc.get("stages", [])}

    if not files_subset:
        # Nothing to re-assign, but still reconcile buckets/coverage to the
        # current valid stage ids (a removed/merged empty stage must not linger).
        return _rebuild_assign(dict(prev_assign.get("file_stage", {})),
                               valid_ids, graph, nav)

    all_files = {f["file"]: f for f in navmod.all_file_descriptors(graph, nav)}
    descriptors = [all_files[f] for f in files_subset if f in all_files]
    stage_menu = "\n".join(
        f"  - {sid}: {desc}"
        for sid, desc in stage_short_descriptions(skeleton_doc).items()
    )

    batches = [descriptors[i:i + batch_size]
               for i in range(0, len(descriptors), batch_size)]
    logger.info("reassign_subset: %d file(s) in %d batch(es)",
                len(descriptors), len(batches))

    file_stage: dict[str, dict] = dict(prev_assign.get("file_stage", {}))
    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(file_assign._assign_batch, api, stage_menu,
                            valid_ids, b, purposes) for b in batches]
        for fut in cf.as_completed(futs):
            try:
                file_stage.update(fut.result())
            except Exception as e:  # noqa: BLE001
                logger.warning("reassign_subset batch failed: %s", e)

    # Files in the subset the LLM dropped or stale-mapped to a now-gone stage
    # become unassigned (honest coverage). Files outside the subset keep theirs,
    # but if their stage was removed/renamed we also drop them to unassigned.
    return _rebuild_assign(file_stage, valid_ids, graph, nav)


def _rebuild_assign(file_stage: dict[str, dict], valid_ids: set[str],
                    graph: dict, nav: dict) -> dict:
    """Recompute buckets + coverage from a file_stage map, honoring valid_ids."""
    files = navmod.all_file_descriptors(graph, nav)
    buckets: dict[str, list[str]] = {sid: [] for sid in valid_ids}
    unassigned: list[str] = []
    clean_file_stage: dict[str, dict] = {}
    for f in files:
        fpath = f["file"]
        entry = file_stage.get(fpath)
        stage = entry.get("stage") if entry else None
        if not stage or stage == "unassigned" or stage not in valid_ids:
            clean_file_stage[fpath] = {"stage": "unassigned", "also": []}
            unassigned.append(fpath)
            continue
        also = [s for s in (entry.get("also") or []) if s in valid_ids]
        clean_file_stage[fpath] = {"stage": stage, "also": also}
        buckets[stage].append(fpath)
    coverage = {
        "n_files": len(files),
        "n_assigned": len(files) - len(unassigned),
        "unassigned": sorted(unassigned),
    }
    return {"file_stage": clean_file_stage, "buckets": buckets, "coverage": coverage}
