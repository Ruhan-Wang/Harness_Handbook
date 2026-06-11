# -*- coding: utf-8 -*-
"""Pass D — Region boundary and purpose revision.

For each function that Pass A split into multiple regions, re-examine the
current region structure with full source code + caller/callee context, and
let the LLM propose targeted corrections:

  - merge(region_indices, purpose):
      Adjacent regions that turn out to be one cohesive step.
  - split(region_index, at_line, left_*, right_*):
      A region that contains two narrative steps and should be cut further.
  - reassign_stage(region_index, new_stage):
      A region whose stage assignment turned out wrong given the surrounding
      mapping.

``drop`` is intentionally NOT exposed: dropping a region creates an orphan
line range (no stage owner) and violates the "every line belongs somewhere"
contract. If a region is truly garbage, that is a Pass C / Pass A concern.

Critic: single Engineer (same role as Pass A; this is detail work on code
boundaries, not high-impact global structure).

Cap: ≤3 actions per function call. If the LLM wants to do more, Pass A
should re-split from scratch — Pass D is meant for refinement, not rewrite.

Cache: per-function fingerprint over (qualname, source_sha1, sorted region
tuples). Stable input → cache hit → skip LLM.

Position in iter: AFTER Pass C, BEFORE Step 3.5. By then all stage-level
moves and skeleton mutations have settled, so region refinements happen on
a stable target.

Invalidation: Pass D does NOT add to ``invalidated``. Its revisions are
authoritative for the iter. If Pass C touches the surrounding stage in a
later iter and Pass A re-classifies the function, the regions get clobbered
and Pass D simply re-revises the next iter.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply  # noqa: E402
from api_client import Api  # noqa: E402
from critic import actor_critic_loop, summarize_result  # noqa: E402
from llm_analyze import (  # noqa: E402
    function_sha1,
    render_source_with_line_numbers,
)

logger = logging.getLogger(__name__)


# ─── Discovery: which qualnames have ≥2 region entries? ──────────────────────


def find_multi_region_qualnames(mapping_doc: dict) -> list[str]:
    """Return qualnames whose mapping contains 2+ region-type entries
    (possibly spread across multiple stages)."""
    counts: dict[str, int] = defaultdict(int)
    for info in mapping_doc.get("stages", {}).values():
        for m in info.get("members", []):
            if m.get("type") == "region":
                counts[m["qualname"]] += 1
    return sorted(qn for qn, n in counts.items() if n >= 2)


def gather_regions(mapping_doc: dict, qualname: str) -> list[tuple[str, dict]]:
    """All region members for ``qualname`` across the mapping, sorted by line_range start.
    Returns list of ``(stage_id, member_dict)`` — same shape apply_region_revision uses."""
    items: list[tuple[str, dict]] = []
    for stage_id, info in mapping_doc.get("stages", {}).items():
        for m in info.get("members", []):
            if m.get("qualname") == qualname and m.get("type") == "region":
                items.append((stage_id, m))
    items.sort(key=lambda p: (p[1].get("line_range") or [0])[0])
    return items


def _node_for(graph: dict, qualname: str) -> dict | None:
    for node in graph.get("nodes", {}).values():
        if node.get("qualname") == qualname:
            return node
    return None


# ─── Caller / callee context (mirrors Pass A's helper) ───────────────────────


def _build_caller_callee_context(
    qualname: str, graph: dict, mapping_doc: dict
) -> tuple[list[dict], list[dict]]:
    id_to_qualname = {nid: n.get("qualname") for nid, n in graph.get("nodes", {}).items()}
    qualname_to_id = {v: k for k, v in id_to_qualname.items() if v}

    qn_to_stages: dict[str, set[str]] = defaultdict(set)
    qn_to_purpose: dict[str, str] = {}
    for stage_id, info in mapping_doc.get("stages", {}).items():
        for m in info.get("members", []):
            qn_to_stages[m["qualname"]].add(stage_id)
            if m.get("type") == "function" and m.get("purpose"):
                qn_to_purpose[m["qualname"]] = m["purpose"]

    target_id = qualname_to_id.get(qualname)
    callers: list[dict] = []
    callees: list[dict] = []
    if not target_id:
        return callers, callees

    seen_caller: set[str] = set()
    seen_callee: set[str] = set()
    for edge in graph.get("edges", []):
        if edge.get("callee_id") == target_id:
            caller_q = id_to_qualname.get(edge.get("caller_id"))
            if caller_q and caller_q not in seen_caller:
                seen_caller.add(caller_q)
                callers.append({
                    "qualname": caller_q,
                    "stages": sorted(qn_to_stages.get(caller_q, set())),
                    "purpose": (qn_to_purpose.get(caller_q, "") or "")[:120],
                })
        if edge.get("caller_id") == target_id:
            callee_id = edge.get("callee_id", "")
            if isinstance(callee_id, str) and callee_id.startswith("boundary:"):
                if callee_id not in seen_callee:
                    seen_callee.add(callee_id)
                    callees.append({"qualname": callee_id, "stages": [], "purpose": "(boundary)"})
            else:
                callee_q = id_to_qualname.get(callee_id)
                if callee_q and callee_q not in seen_callee:
                    seen_callee.add(callee_q)
                    callees.append({
                        "qualname": callee_q,
                        "stages": sorted(qn_to_stages.get(callee_q, set())),
                        "purpose": (qn_to_purpose.get(callee_q, "") or "")[:120],
                    })
    return callers, callees


# ─── Cache (purpose- and structure-sensitive fingerprint) ────────────────────


def _fingerprint(qualname: str, source_sha: str, regions: list[tuple[str, dict]]) -> str:
    items = sorted(
        (stage_id, tuple(m.get("line_range") or []), m.get("purpose", "") or "")
        for stage_id, m in regions
    )
    return hashlib.sha1(
        json.dumps([qualname, source_sha, items], ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _cache_path(cache_dir: Path, qualname: str) -> Path:
    safe = qualname.replace("/", "_").replace(":", "_")
    return cache_dir / f"{safe}.json"


def _load_cache(cache_dir: Path, qualname: str, fp: str) -> dict | None:
    p = _cache_path(cache_dir, qualname)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("fingerprint") != fp:
        return None
    return data


def _save_cache(
    cache_dir: Path,
    qualname: str,
    fp: str,
    actions_applied: list[dict],
    proposed: int,
    rejected: int,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "qualname": qualname,
        "fingerprint": fp,
        "proposed": proposed,
        "applied": actions_applied,
        "rejected": rejected,
    }
    _cache_path(cache_dir, qualname).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ─── Prompts ─────────────────────────────────────────────────────────────────


_MAX_ACTIONS = 3

ACTOR_RULES = """You are revising the REGION SPLIT of one Python function from the Terminus 2 agent handbook.

You will see the function's full source (line-numbered), the current region boundaries (with their stage assignments and purposes), and the caller/callee context. Decide whether any of the following targeted refinements would IMPROVE the region structure.

ALLOWED ACTIONS (you may use any combination, total ≤ 3):

1. {"action": "merge", "region_indices": [i, j, ...], "purpose": "<combined purpose, 30–80 words>"}
   Use ONLY when two or more *adjacent* regions actually describe one cohesive narrative step that was over-split.

2. {"action": "split", "region_index": i, "at_line": N,
    "left_purpose": "...", "right_purpose": "...",
    "left_stage": "<stage_id>", "right_stage": "<stage_id>"}
   Use ONLY when a single region contains two distinct narrative steps separated at a clear statement boundary. ``at_line`` is the LAST line of the left half (so left = [start, N], right = [N+1, end]). It must lie strictly inside the region (not at either endpoint).

3. {"action": "reassign_stage", "region_index": i, "new_stage": "<stage_id>"}
   Use when a region's stage assignment is wrong given the surrounding mapping (e.g. callers and callees suggest a different stage).

INDEX SEMANTICS
- ``region_index`` and ``region_indices`` are 0-based indices into the
  CURRENT region list shown below (source-order, sorted by line_range start).
- An action that references an out-of-range index will be rejected.

CAP
- Propose AT MOST 3 actions per call.
- If the current region split is already correct, return an empty actions list.
- If you want to make MANY changes, the original split was wrong as a whole;
  do not try to fix it action-by-action — return at most 3 of the most
  impactful changes and rely on a later iteration to refine further.

WHAT NOT TO DO
- Do NOT propose a ``drop`` action. Every line of the function must belong
  to some stage.
- Do NOT propose ``split`` at ``at_line`` equal to the region's start or end.
- Do NOT propose ``merge`` on non-adjacent regions.
- Do NOT change a region's stage to one that is not in the skeleton menu.

OUTPUT
Return ONLY a single JSON object inside a ```json fenced block:

{
  "actions": [<action>, ...],   // 0..3 entries
  "rationale": "<one-paragraph summary; or 'current split looks correct'>"
}
"""


def _regions_block(regions: list[tuple[str, dict]]) -> str:
    lines = []
    for i, (stage_id, m) in enumerate(regions):
        lr = m.get("line_range") or [0, 0]
        purpose = (m.get("purpose") or "").replace("\n", " ").strip()
        if len(purpose) > 250:
            purpose = purpose[:250] + " …"
        lines.append(
            f"  [{i}] line_range={lr}  stage={stage_id}\n"
            f"      purpose: {purpose}"
        )
    return "\n".join(lines)


def _stage_menu(skeleton_doc: dict) -> str:
    lines = []
    for s in skeleton_doc.get("stages", []):
        sid = s["id"]
        title = s.get("title", "")
        desc1 = (s.get("description") or "").split(". ")[0][:90]
        lines.append(f"  {sid:<25} {title} — {desc1}")
    return "\n".join(lines) if lines else "  (no stages defined)"


def _callers_block(items: list[dict]) -> str:
    if not items:
        return "    (none)"
    out = []
    for c in items:
        out.append(f"    {c['qualname']:<50}  stages={c['stages']}  — {c['purpose']}")
    return "\n".join(out)


def build_actor_prompt(
    qualname: str,
    node: dict,
    source_block: str,
    regions: list[tuple[str, dict]],
    skeleton_doc: dict,
    callers: list[dict],
    callees: list[dict],
) -> str:
    parts = [
        ACTOR_RULES,
        "",
        "## Function being revised",
        f"  qualname:    {qualname}",
        f"  file:        {node.get('file')}",
        f"  line_range:  [{node.get('line_start')}, {node.get('line_end')}]",
        f"  region_count: {len(regions)}",
        "",
        "## Current regions (0-based indices)",
        _regions_block(regions),
        "",
        "## Skeleton stage menu (valid IDs for reassign_stage / split.*_stage)",
        _stage_menu(skeleton_doc),
        "",
        "## Callers (with their current stage assignments)",
        _callers_block(callers),
        "",
        "## Callees (with their current stage assignments)",
        _callers_block(callees),
        "",
        "## Function source (line-numbered)",
        "```python",
        source_block,
        "```",
        "",
        f"At most {_MAX_ACTIONS} actions. Return the JSON block only.",
    ]
    return "\n".join(parts)


def build_review_evidence(
    qualname: str,
    node: dict,
    source_block: str,
    regions: list[tuple[str, dict]],
    skeleton_doc: dict,
    callers: list[dict],
    callees: list[dict],
) -> str:
    """Same ground truth the Actor saw — Critic uses it to verify each
    proposed action against actual source line numbers and stage descriptions."""
    parts = [
        f"Function: {qualname}  ({node.get('file')}:{node.get('line_start')}-{node.get('line_end')})",
        f"Current region count: {len(regions)}",
        "",
        "Current regions:",
        _regions_block(regions),
        "",
        "Skeleton stage menu:",
        _stage_menu(skeleton_doc),
        "",
        "Callers:",
        _callers_block(callers),
        "",
        "Callees:",
        _callers_block(callees),
        "",
        "Function source (line-numbered):",
        "```python",
        source_block,
        "```",
    ]
    return "\n".join(parts)


_PROPOSAL_SCHEMA_HINT = """The proposal must be:
{
  "actions": [
    {"action": "merge"|"split"|"reassign_stage", ...},
    ...
  ],
  "rationale": str
}
At most 3 entries. ``drop`` is not allowed. If no changes are needed, return
an empty actions list and rationale describing why."""


# ─── Per-action mechanical validation ────────────────────────────────────────


def _validate_action(
    action: dict,
    regions: list[tuple[str, dict]],
    skel_ids: set[str],
) -> str | None:
    """Return error message or None. Indices are validated against the
    CURRENT region list (pre-apply order); apply_region_revision handles
    sequential resolution as actions land."""
    n = len(regions)
    kind = action.get("action")

    if kind == "drop":
        return "drop is not allowed in Pass D"

    if kind == "merge":
        idxs = action.get("region_indices") or []
        if not isinstance(idxs, list) or len(idxs) < 2:
            return "merge requires region_indices with at least 2 entries"
        try:
            idxs_int = [int(i) for i in idxs]
        except (TypeError, ValueError):
            return "merge region_indices must be integers"
        if len(set(idxs_int)) != len(idxs_int):
            return "merge region_indices must be distinct"
        if any(i < 0 or i >= n for i in idxs_int):
            return f"merge region_indices out of range [0,{n - 1}]"
        # Adjacency: when sorted, indices must be consecutive — merging
        # non-adjacent regions would create a gap or interleave with a
        # different stage's content.
        sorted_idxs = sorted(idxs_int)
        if any(b - a != 1 for a, b in zip(sorted_idxs, sorted_idxs[1:])):
            return "merge region_indices must be adjacent (consecutive)"
        purpose = action.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            return "merge requires a non-empty purpose"
        return None

    if kind == "split":
        try:
            i = int(action.get("region_index"))
        except (TypeError, ValueError):
            return "split region_index must be an int"
        if i < 0 or i >= n:
            return f"split region_index out of range [0,{n - 1}]"
        try:
            at = int(action.get("at_line"))
        except (TypeError, ValueError):
            return "split at_line must be an int"
        lr = regions[i][1].get("line_range") or [0, 0]
        if len(lr) < 2:
            return "split target region has malformed line_range"
        start, end = lr[0], lr[1]
        # at_line is the LAST line of the LEFT half; right half is [at+1, end].
        # Must lie strictly inside the region.
        if at <= start or at >= end:
            return (
                f"split at_line={at} must be strictly inside region's "
                f"line_range [{start}, {end}]"
            )
        # Stages and purposes are optional, but if the key is present in the
        # action dict at all we require a valid non-empty string — otherwise
        # apply_region_revision would write the raw value (e.g. `None`)
        # straight into the mapping and contaminate downstream consumers
        # that expect strings.
        for key in ("left_stage", "right_stage"):
            if key in action:
                v = action[key]
                if not isinstance(v, str) or not v:
                    return f"split {key}, if provided, must be a non-empty string"
                if v not in skel_ids:
                    return f"split {key}='{v}' is not in the skeleton"
        for key in ("left_purpose", "right_purpose"):
            if key in action:
                v = action[key]
                if not isinstance(v, str) or not v.strip():
                    return f"split {key}, if provided, must be a non-empty string"
        return None

    if kind == "reassign_stage":
        try:
            i = int(action.get("region_index"))
        except (TypeError, ValueError):
            return "reassign_stage region_index must be an int"
        if i < 0 or i >= n:
            return f"reassign_stage region_index out of range [0,{n - 1}]"
        new_stage = action.get("new_stage")
        # Type-check BEFORE testing set membership — set lookup hashes the
        # candidate value, which raises TypeError for unhashable types like
        # list/dict. Without this guard, a malformed LLM response surfaces
        # as a "crashed" qualname instead of a clean rejection.
        if not isinstance(new_stage, str) or not new_stage:
            return "reassign_stage new_stage must be a non-empty string"
        if new_stage not in skel_ids:
            return f"reassign_stage new_stage '{new_stage}' is not in the skeleton"
        if new_stage == regions[i][0]:
            return f"reassign_stage new_stage equals current stage ('{new_stage}')"
        return None

    return f"unknown action kind: {kind!r}"


def _action_refs(action: dict) -> set[int]:
    """Return the set of original region indices an action references."""
    kind = action.get("action")
    if kind == "merge":
        try:
            return {int(x) for x in (action.get("region_indices") or [])}
        except (TypeError, ValueError):
            return set()
    if kind in ("split", "reassign_stage"):
        try:
            return {int(action.get("region_index"))}
        except (TypeError, ValueError):
            return set()
    return set()


def _action_kills(action: dict) -> set[int]:
    """Return the set of original region indices that this action will make
    unresolvable for later actions in the same batch.

    - ``merge`` keeps the lowest index (it absorbs the others), kills the rest.
    - ``split`` replaces the original region with two synthetic halves
      (new ``_orig_idx`` values that LLM-given indices cannot reach), so the
      original index is dead.
    - ``reassign_stage`` doesn't kill anything; the region's _orig_idx survives.
    """
    kind = action.get("action")
    if kind == "merge":
        try:
            sorted_idxs = sorted(int(x) for x in (action.get("region_indices") or []))
        except (TypeError, ValueError):
            return set()
        return set(sorted_idxs[1:])  # everything except the absorbing index
    if kind == "split":
        try:
            return {int(action.get("region_index"))}
        except (TypeError, ValueError):
            return set()
    return set()


def _detect_batch_conflicts(actions: list[dict]) -> dict[int, str]:
    """For a sequence of actions, return ``{index: error_message}`` for each
    action whose referenced indices were killed by an earlier action in the
    same batch. Per-action shape errors are NOT covered here — they're caught
    by ``_validate_action`` first; this catches the inter-action conflicts
    that ``apply_region_revision``'s ``_resolve`` would silently no-op away.
    """
    killed: set[int] = set()
    errors: dict[int, str] = {}
    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        refs = _action_refs(action)
        overlap = refs & killed
        if overlap:
            errors[i] = (
                f"references region(s) {sorted(overlap)} that were already "
                f"killed by an earlier action in this batch"
            )
            continue
        killed |= _action_kills(action)
    return errors


# ─── Per-function revision ───────────────────────────────────────────────────


def revise_one_function(
    api: Api,
    qualname: str,
    skeleton_doc: dict,
    mapping_doc: dict,
    graph: dict,
    source_root: Path,
    cache_dir: Path,
    force: bool = False,
) -> dict:
    """Returns {actions: list[dict], proposed: int, rejected: int, source: str}.
    Mutates mapping_doc in place via apply_region_revision when actions pass."""
    regions = gather_regions(mapping_doc, qualname)
    if len(regions) < 2:
        return {"actions": [], "proposed": 0, "rejected": 0, "source": "trivial"}

    node = _node_for(graph, qualname)
    if (
        not node
        or not node.get("file")
        or node.get("line_start") is None
        or node.get("line_end") is None
    ):
        # render_source_with_line_numbers uses min(end, len(lines)) which
        # crashes with TypeError when end is None — surface that as "no_node"
        # explicitly instead of letting it bubble up to run_pass_d as a crash.
        return {"actions": [], "proposed": 0, "rejected": 0, "source": "no_node"}

    line_start = node["line_start"]
    line_end = node["line_end"]
    if not (isinstance(line_start, int) and isinstance(line_end, int)
            and line_start <= line_end):
        return {"actions": [], "proposed": 0, "rejected": 0, "source": "no_node"}

    src_path = source_root / node["file"]
    try:
        source_block = render_source_with_line_numbers(src_path, line_start, line_end)
        source_sha = function_sha1(src_path, line_start, line_end)
    except (FileNotFoundError, OSError) as e:
        logger.warning("Pass D: cannot read source for %s: %s", qualname, e)
        return {"actions": [], "proposed": 0, "rejected": 0, "source": "no_source"}

    fp = _fingerprint(qualname, source_sha, regions)
    if not force:
        cached = _load_cache(cache_dir, qualname, fp)
        if cached is not None:
            return {"actions": [], "proposed": 0, "rejected": 0, "source": "cache"}

    callers, callees = _build_caller_callee_context(qualname, graph, mapping_doc)
    actor_prompt = build_actor_prompt(
        qualname, node, source_block, regions, skeleton_doc, callers, callees,
    )
    review_evidence = build_review_evidence(
        qualname, node, source_block, regions, skeleton_doc, callers, callees,
    )
    task_context = (
        f"Pass D region revision of `{qualname}` "
        f"({node.get('file')}:{node.get('line_start')}-{node.get('line_end')}), "
        f"{len(regions)} current regions."
    )

    result = actor_critic_loop(
        api=api,
        actor_prompt=actor_prompt,
        critic_role="engineer",
        task_context=task_context,
        proposal_schema_hint=_PROPOSAL_SCHEMA_HINT,
        max_revise_rounds=1,
        review_evidence=review_evidence,
    )

    # Same lesson as Pass B: a legitimate "looks fine" response can be
    # final_proposal={} or {"actions": []}. Only failed loops have
    # final_proposal is None.
    if not result.accepted or result.final_proposal is None:
        logger.info(
            "  %s: %s", qualname, summarize_result(result, f"PassD[{qualname}]")
        )
        return {
            "actions": [], "proposed": 0, "rejected": 0, "source": "llm_failed",
        }

    raw_actions = (
        result.final_proposal.get("actions")
        if isinstance(result.final_proposal, dict) else None
    )
    if not isinstance(raw_actions, list):
        raw_actions = []
    actions = raw_actions[:_MAX_ACTIONS]
    skel_ids = {s["id"] for s in skeleton_doc.get("stages", [])}
    # Detect cross-action conflicts BEFORE per-action validation. We need the
    # full ordered batch to reason about kills (a later action referencing an
    # index that an earlier merge/split made unresolvable). Per-action
    # validation alone misses this because it only sees one action at a time.
    batch_conflicts = _detect_batch_conflicts(actions)
    accepted_actions: list[dict] = []
    rejected = 0

    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            logger.warning("  [%s] rejecting non-dict action: %r", qualname, action)
            rejected += 1
            continue
        err = _validate_action(action, regions, skel_ids)
        if err:
            logger.warning("  [%s] rejecting action: %s — %s", qualname, action, err)
            rejected += 1
            continue
        if i in batch_conflicts:
            logger.warning(
                "  [%s] rejecting action #%d: %s — %s",
                qualname, i, action, batch_conflicts[i],
            )
            rejected += 1
            continue
        accepted_actions.append(action)

    if accepted_actions:
        try:
            apply.apply_region_revision(
                mapping_doc,
                {"qualname": qualname, "actions": accepted_actions},
                source_root,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("  [%s] apply_region_revision crashed: %s", qualname, e)
            # Treat as fully rejected — don't cache, let next iter retry.
            return {
                "actions": [],
                "proposed": len(actions),
                "rejected": rejected + len(accepted_actions),
                "source": "apply_failed",
            }

    _save_cache(cache_dir, qualname, fp, accepted_actions, len(actions), rejected)
    logger.info(
        "      → regions=%d  proposed=%d  applied=%d  rejected=%d",
        len(regions), len(actions), len(accepted_actions), rejected,
    )
    return {
        "actions": accepted_actions,
        "proposed": len(actions),
        "rejected": rejected,
        "source": "llm",
    }


# ─── Top-level entry point ───────────────────────────────────────────────────


def run_pass_d(
    api: Api,
    skeleton_doc: dict,
    mapping_doc: dict,
    graph: dict,
    source_root: Path,
    cache_dir: Path,
    force: bool = False,
) -> dict:
    """Audit every multi-region function. Returns:
        {
          "applied":   list[dict],    # all per-function action lists
          "proposed":  int,
          "rejected":  int,
          "per_qn":    {qualname: source_label},
          "summary":   str,
        }
    Mutates mapping_doc in place. Does NOT add anything to invalidated.
    """
    qns = find_multi_region_qualnames(mapping_doc)
    total = len(qns)
    logger.info("Pass D: %d multi-region function(s) to review", total)

    applied_all: list[dict] = []
    proposed_total = 0
    rejected_total = 0
    per_qn: dict[str, str] = {}

    for i, qn in enumerate(qns, start=1):
        logger.info("  [%d/%d] %s", i, total, qn)
        try:
            res = revise_one_function(
                api=api,
                qualname=qn,
                skeleton_doc=skeleton_doc,
                mapping_doc=mapping_doc,
                graph=graph,
                source_root=source_root,
                cache_dir=cache_dir,
                force=force,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Pass D crashed for %s: %s", qn, e)
            per_qn[qn] = "crashed"
            logger.info("      → crashed")
            continue
        per_qn[qn] = res["source"]
        # Short-circuit cases (trivial / no_node / no_source / cache /
        # llm_failed / apply_failed) didn't print their own summary inside
        # `revise_one_function`; surface them here so the progress loop has
        # one line per function regardless of outcome.
        if res["source"] != "llm":
            logger.info("      → %s", res["source"])
        if res["actions"]:
            applied_all.append({
                "qualname": qn,
                "actions": res["actions"],
            })
        proposed_total += res["proposed"]
        rejected_total += res["rejected"]

    return {
        "applied": applied_all,
        "proposed": proposed_total,
        "rejected": rejected_total,
        "per_qn": per_qn,
        "summary": (
            f"PassD: functions_reviewed={len(per_qn)}, proposed={proposed_total}, "
            f"applied={sum(len(a['actions']) for a in applied_all)}, "
            f"rejected={rejected_total}"
        ),
    }


# ─── CLI (standalone use, mirrors order_stage_members.main) ──────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse
    import yaml as _yaml

    logging.basicConfig(
        format="[%(asctime)s][%(levelname)5s] %(message)s",
        level=logging.INFO,
    )
    here = Path(__file__).resolve()
    project = here.parents[3]
    phase2 = project / "handbook/phase2"

    ap = argparse.ArgumentParser()
    ap.add_argument("--mapping", type=Path, default=phase2 / "mapping.yaml")
    ap.add_argument("--skeleton", type=Path, default=phase2 / "skeleton.yaml")
    ap.add_argument("--graph", type=Path,
                    default=project / "handbook/phase1/graph.json")
    ap.add_argument("--source-root", type=Path,
                    default=project / "harbor/src/harbor/agents/terminus_2")
    ap.add_argument("--cache-dir", type=Path,
                    default=phase2 / "cache/pass_d")
    ap.add_argument("--force", action="store_true",
                    help="re-run LLM even when cache hit")
    args = ap.parse_args(argv)

    from skeleton_yaml import load_yaml
    skeleton_doc = load_yaml(args.skeleton)
    mapping_doc = _yaml.safe_load(args.mapping.read_text(encoding="utf-8"))
    graph = json.loads(args.graph.read_text(encoding="utf-8"))

    api = Api()
    summary = run_pass_d(
        api, skeleton_doc, mapping_doc, graph, args.source_root,
        args.cache_dir, force=args.force,
    )

    from iterate_phase2 import _dump_yaml
    args.mapping.write_text(_dump_yaml(mapping_doc), encoding="utf-8")

    logger.info("%s", summary["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
