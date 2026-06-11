# -*- coding: utf-8 -*-
"""apply.py — Mechanical layer that applies Critic-approved proposals to state.

LLM decides WHAT to change. This module mechanically applies the change to:
  - mapping_doc (in-memory dict, will be serialized to mapping.yaml)
  - skeleton_doc (in-memory dict, will be serialized to skeleton.yaml)

Returns: list of `qualname` strings that are now invalidated (next iteration's
Pass A should re-classify them with updated context).

No LLM calls here. Pure dict/list mutation + derived-field recomputation.
"""
from __future__ import annotations

import hashlib
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ast_snap import (  # noqa: E402
    DEFAULT_SNAP_THRESHOLD,
    find_function_statements,
    snap_range,
    verify_first_last_lines,
)

logger = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _sha1_of_range(file_path: Path, start: int, end: int) -> str:
    text = file_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    snippet = "\n".join(lines[start - 1 : end])
    return hashlib.sha1(snippet.encode("utf-8")).hexdigest()


def _ensure_stage(mapping_doc: dict, stage_id: str) -> dict:
    """Ensure mapping_doc['stages'][stage_id] exists with default shape."""
    stages = mapping_doc.setdefault("stages", {})
    if stage_id not in stages:
        stages[stage_id] = {"members": [], "uses_crosscuts": [], "subsystem_refs": []}
    return stages[stage_id]


def _remove_member(stage: dict, qualname: str, member_type: str | None = None,
                   line_range: list | None = None) -> int:
    """Remove a specific member; returns count removed."""
    before = len(stage.get("members", []))
    def _matches(m):
        if m.get("qualname") != qualname:
            return False
        if member_type and m.get("type") != member_type:
            return False
        if line_range and m.get("line_range") != line_range:
            return False
        return True
    stage["members"] = [m for m in stage.get("members", []) if not _matches(m)]
    return before - len(stage["members"])


def _build_id_to_qualname(graph: dict) -> dict[str, str]:
    return {nid: n.get("qualname") for nid, n in graph["nodes"].items()}


# ─── Derived fields ───────────────────────────────────────────────────────────


CROSSCUT_PREFIX = "crosscut-"
SUBSYS_FILE_TO_ID = {
    "tmux_session.py": "subsys-tmux",
    "terminus_json_plain_parser.py": "subsys-parser-json",
    "terminus_xml_plain_parser.py": "subsys-parser-xml",
    "asciinema_handler.py": "subsys-asciinema",
}


def rederive_uses_crosscuts_and_subsystem_refs(
    mapping_doc: dict, graph: dict
) -> None:
    """Recompute uses_crosscuts and subsystem_refs for every stage.

    Cheap (O(edges + members)); we just redo it after any change instead of
    tracking deltas.
    """
    nodes = graph["nodes"]
    id_to_qualname = _build_id_to_qualname(graph)

    # qualname → set of crosscut-X* stages it lives in
    crosscut_home: dict[str, set[str]] = defaultdict(set)
    for stage_id, info in mapping_doc.get("stages", {}).items():
        if not stage_id.startswith(CROSSCUT_PREFIX):
            continue
        for m in info.get("members", []):
            crosscut_home[m["qualname"]].add(stage_id)

    # caller_qualname → set of callee_qualnames
    callees_by_qualname: dict[str, set[str]] = defaultdict(set)
    edges_by_caller_q: dict[str, list[dict]] = defaultdict(list)
    for edge in graph.get("edges", []):
        caller_q = id_to_qualname.get(edge.get("caller_id"))
        if not caller_q:
            continue
        edges_by_caller_q[caller_q].append(edge)
        callee_id = edge.get("callee_id", "")
        if not callee_id.startswith("boundary:"):
            callee_q = id_to_qualname.get(callee_id)
            if callee_q:
                callees_by_qualname[caller_q].add(callee_q)

    for stage_id, info in mapping_doc.get("stages", {}).items():
        if stage_id.startswith(CROSSCUT_PREFIX):
            info["uses_crosscuts"] = []
            info["subsystem_refs"] = []
            continue

        crosscuts: set[str] = set()
        refs: set[str] = set()
        for m in info.get("members", []):
            qn = m["qualname"]
            caller_file = m.get("file")
            for callee_q in callees_by_qualname.get(qn, set()):
                for c_stage in crosscut_home.get(callee_q, set()):
                    crosscuts.add(c_stage)
            for edge in edges_by_caller_q.get(qn, []):
                callee_id = edge.get("callee_id", "")
                if callee_id.startswith("boundary:"):
                    if "harbor.llms" in callee_id:
                        refs.add("subsys-llm")
                else:
                    callee_node = nodes.get(callee_id)
                    if callee_node:
                        f = callee_node.get("file")
                        if f in SUBSYS_FILE_TO_ID and f != caller_file:
                            refs.add(SUBSYS_FILE_TO_ID[f])
        info["uses_crosscuts"] = sorted(crosscuts)
        info["subsystem_refs"] = sorted(refs)


# ─── Apply: Classification (Pass A) ───────────────────────────────────────────


def apply_classification(
    mapping_doc: dict,
    proposal: dict,
    source_root: Path,
    valid_stage_ids: set[str] | None = None,
) -> list[str]:
    """Apply Pass A's classification proposal for one function.

    Schema:
      proposal = {
        "qualname": "...",
        "purpose": "...",
        "granularity": "function" | "region",
        "function_assignments": [stage_id, ...],
        "regions": [
          {"line_range": [a, b], "first_line": "...", "last_line": "...",
           "purpose": "...", "stage_id": "..."},
          ...
        ] | None,
        "file": "...", "line_range": [a, b]   (auxiliary, may be present)
      }

    Mutates mapping_doc in place. Returns list of qualnames invalidated.
    """
    qn = proposal["qualname"]
    purpose = proposal.get("purpose", "")
    granularity = proposal.get("granularity")
    raw_assignments = proposal.get("function_assignments") or []
    # LLM occasionally repeats a stage id in function_assignments (e.g.,
    # mentions stage-2 in both a primary and "also belongs to" position).
    # Dedup with `dict.fromkeys` keeps the first occurrence so the warning
    # below can show the original list verbatim for diagnosis.
    function_assignments = list(dict.fromkeys(raw_assignments))
    if len(function_assignments) < len(raw_assignments):
        logger.warning(
            "apply_classification: %s had duplicate function_assignments %r; "
            "deduped to %r", qn, raw_assignments, function_assignments,
        )
    regions = proposal.get("regions") or []
    file = proposal.get("file")
    func_range = proposal.get("line_range")

    invalidated: list[str] = []

    # Defensive guard against silent wipe. The downstream branches add entries
    # only when (function_assignments or regions) OR (granularity == "function"
    # and purpose). A proposal that matches NEITHER — e.g., granularity="region"
    # but regions=[], or granularity=None entirely — would erase every existing
    # entry for `qn` and add nothing. populate_unmapped then tags it
    # "missing_llm_output", but missing entries do NOT feed back into Pass A's
    # invalidated queue, so the qualname is permanently dropped. (Observed
    # symptom: 18 core TmuxSession / trajectory helper functions vanished
    # across a 5-iter run with this branch never re-entering.)
    #
    # Skip the wipe entirely when the proposal is shaped to produce no output.
    # Existing state (if any) is preserved; iteration driver will re-inject
    # the qualname into invalidated next iter so Pass A can try again.
    if not function_assignments and not regions and not (
        granularity == "function" and purpose
    ):
        logger.warning(
            "apply_classification: refusing to wipe %s — proposal has no "
            "function_assignments, no regions, and is not a function+purpose "
            "api_surface case (granularity=%r, purpose_len=%d). Leaving prior "
            "mapping state intact for retry.",
            qn, granularity, len(purpose or ""),
        )
        return invalidated

    # Remove existing entries for this qualname from ALL stages.
    for stage_id, info in mapping_doc.get("stages", {}).items():
        info["members"] = [m for m in info.get("members", []) if m.get("qualname") != qn]

    # Two branches based on whether the proposal contains real assignments:
    #   - With assignments (or regions): the function is now mapped, so any
    #     stale entry for it in `unmapped_functions` (e.g., from an earlier
    #     iter when it was treated as api_surface) must be cleaned up.
    #   - Without assignments: the LLM explicitly chose not to map it (api
    #     surface, dead, or otherwise out-of-scope). The Pass A prompt is
    #     designed to still produce a purpose for such functions; preserve
    #     that purpose by attaching it to the unmapped entry — handbook
    #     rendering uses it to describe the function in the "not in any
    #     stage" section instead of leaving it as an unannotated stub.
    ump = mapping_doc.get("unmapped_functions", [])
    if function_assignments or regions:
        mapping_doc["unmapped_functions"] = [u for u in ump if u.get("qualname") != qn]
    elif granularity == "function" and purpose:
        # No assignments → LLM treated this as api_surface. Stash the purpose
        # in unmapped_functions so handbook generation can still describe it.
        new_ump = []
        replaced = False
        for u in ump:
            if u.get("qualname") == qn:
                u = dict(u)
                u["purpose"] = purpose
                replaced = True
            new_ump.append(u)
        if not replaced:
            new_ump.append({
                "qualname": qn,
                "file": file,
                "reason": "api_surface",
                "purpose": purpose,
            })
        mapping_doc["unmapped_functions"] = new_ump

    # Warn if proposal is structurally inconsistent. We trust granularity as
    # the source of truth and silently drop regions if granularity=function.
    if granularity == "function" and regions:
        logger.warning(
            "apply_classification: %s has granularity=function but %d regions "
            "supplied; dropping regions",
            qn, len(regions),
        )
        regions = []
    if granularity == "region" and not regions:
        # The LLM declared `granularity=region` but forgot to populate the
        # `regions` list — typically a JSON-shape error where the model
        # confuses "this function spans multiple stages" with "split into
        # regions". Treating it as function-level (the most charitable
        # reading) keeps the classification useful instead of discarding the
        # whole proposal.
        logger.warning(
            "apply_classification: %s has granularity=region but no regions "
            "supplied; falling back to function-level", qn,
        )
        granularity = "function"

    # Add function-level entries.
    for stage_id in function_assignments:
        if valid_stage_ids is not None and stage_id not in valid_stage_ids:
            logger.warning(
                "apply_classification: dropping invalid stage_id %r for %s "
                "(not in skeleton)", stage_id, qn,
            )
            continue
        stage = _ensure_stage(mapping_doc, stage_id)
        if file and func_range:
            sha1 = _sha1_of_range(source_root / file, func_range[0], func_range[1])
        else:
            sha1 = ""
        stage["members"].append({
            "qualname": qn,
            "type": "function",
            "file": file,
            "line_range": list(func_range) if func_range else None,
            "sha1": sha1,
            "purpose": purpose,
        })

    # Add region entries. Each region's line_range is snapped to the nearest
    # legal AST statement boundary before storing — LLM commonly cuts off at
    # ±1-3 lines from a real statement edge.
    if granularity == "region":
        statements = (
            find_function_statements(source_root / file, qn)
            if file else None
        )
        for region in regions:
            stage_id = region.get("stage_id")
            if not stage_id:
                continue
            if valid_stage_ids is not None and stage_id not in valid_stage_ids:
                logger.warning(
                    "apply_classification: dropping region with invalid "
                    "stage_id %r for %s", stage_id, qn,
                )
                continue
            stage = _ensure_stage(mapping_doc, stage_id)

            llm_range = region.get("line_range")
            # Reject degenerate ranges where start > end. The downstream AST
            # snap would silently coerce these (snap_end < snap_start short-
            # circuits to a "needs_review" with the original range preserved),
            # and `_sha1_of_range`'s slice would return an empty snippet —
            # both eat the bad data without surfacing the real problem.
            if (
                isinstance(llm_range, (list, tuple))
                and len(llm_range) == 2
                and isinstance(llm_range[0], int)
                and isinstance(llm_range[1], int)
                and llm_range[0] > llm_range[1]
            ):
                logger.warning(
                    "apply_classification: dropping region with inverted "
                    "line_range %r for %s", llm_range, qn,
                )
                continue
            snap_status = "no_range"
            snap_distance = 0
            snap_note = ""
            final_range = list(llm_range) if llm_range else None

            if llm_range and statements:
                snap = snap_range(
                    llm_range[0], llm_range[1], statements,
                    snap_threshold=DEFAULT_SNAP_THRESHOLD,
                )
                final_range = [snap.start, snap.end]
                snap_status = snap.status
                snap_distance = snap.distance
                snap_note = snap.note or ""

                # Cross-check first_line / last_line.
                first_line = region.get("first_line")
                last_line = region.get("last_line")
                if first_line or last_line:
                    ok, mismatch_note = verify_first_last_lines(
                        source_root / file, (snap.start, snap.end),
                        first_line, last_line,
                    )
                    if not ok:
                        snap_note = (snap_note + "; " + mismatch_note).strip("; ")
                        if snap_status == "ok":
                            snap_status = "snapped"
                        if (
                            "first_line mismatch" in mismatch_note
                            and "last_line mismatch" in mismatch_note
                        ):
                            snap_status = "needs_review"
            elif llm_range and not statements:
                snap_status = "needs_review"
                snap_note = "could not locate function in AST"

            if file and final_range:
                sha1 = _sha1_of_range(
                    source_root / file, final_range[0], final_range[1]
                )
            else:
                sha1 = ""

            member = {
                "qualname": qn,
                "type": "region",
                "file": file,
                "line_range": final_range,
                "sha1": sha1,
                "purpose": region.get("purpose", ""),
                "original_llm_range": list(llm_range) if llm_range else None,
                "snap_status": snap_status,
                "snap_distance": snap_distance,
            }
            if snap_note:
                member["snap_note"] = snap_note
            for k in ("first_line", "last_line"):
                if k in region:
                    member[k] = region[k]
            stage["members"].append(member)

    return invalidated  # Pass A classification doesn't invalidate others


# ─── Apply: Reassignment (Pass B / E) ─────────────────────────────────────────


def apply_reassignment(
    mapping_doc: dict,
    qualname: str,
    from_stages: list[str],
    to_stages: list[str],
) -> list[str]:
    """Move all entries for `qualname` from from_stages to to_stages.

    Preserves all member properties (purpose, line_range, sha1, regions, etc.).
    """
    # Collect all members for this qualname across from_stages (deduped).
    members_to_move: list[dict] = []
    seen_keys: set[tuple] = set()
    for sid in from_stages:
        stage = mapping_doc.get("stages", {}).get(sid)
        if not stage:
            continue
        for m in stage.get("members", []):
            if m.get("qualname") != qualname:
                continue
            key = (m.get("type"), tuple(m.get("line_range") or []))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            members_to_move.append(dict(m))

    # Remove from from_stages
    for sid in from_stages:
        stage = mapping_doc.get("stages", {}).get(sid)
        if stage:
            _remove_member(stage, qualname)

    # Add to to_stages (skip if already present)
    for sid in to_stages:
        stage = _ensure_stage(mapping_doc, sid)
        existing_keys = {
            (m.get("type"), tuple(m.get("line_range") or []))
            for m in stage["members"]
            if m.get("qualname") == qualname
        }
        for m in members_to_move:
            key = (m.get("type"), tuple(m.get("line_range") or []))
            if key not in existing_keys:
                stage["members"].append(dict(m))

    return [qualname]


# ─── Apply: Skeleton change (Pass C) ──────────────────────────────────────────


def apply_skeleton_add_stage(
    skeleton_doc: dict,
    mapping_doc: dict,
    proposal: dict,
) -> list[str]:
    """Add a new stage; optionally move existing members into it.

    Schema:
      proposal = {
        "action": "add_stage",
        "new_stage": {"id": "...", "title": "...", "description": "...",
                      "parent": "..." | null, "children": [...]},
        "move_members": [
          {"qualname": "...", "from_stage": "..."},   # type unspecified → all entries
          ...
        ]
      }
    """
    new_stage_spec = proposal["new_stage"]
    new_id = new_stage_spec["id"]

    # If already present, skip add (idempotent).
    if not any(s["id"] == new_id for s in skeleton_doc.get("stages", [])):
        # The LLM sometimes proposes a children list referencing stages that
        # don't actually exist yet (e.g., copy-pasted from a different
        # add_stage proposal in the same batch that was rejected, or
        # imagining future siblings). Filter to children that resolve to
        # real stage ids — otherwise downstream walkers get tripped up by
        # parent.children pointers that lead nowhere.
        existing_ids = {s["id"] for s in skeleton_doc.get("stages", [])}
        raw_children = new_stage_spec.get("children", []) or []
        valid_children = [c for c in raw_children if c in existing_ids]
        dropped_children = [c for c in raw_children if c not in existing_ids]
        if dropped_children:
            logger.warning(
                "apply_skeleton_add_stage: new stage %r references missing "
                "children %r; dropped",
                new_id, dropped_children,
            )
        skeleton_doc.setdefault("stages", []).append({
            "id": new_id,
            "title": new_stage_spec.get("title", new_id),
            "description": new_stage_spec.get("description", ""),
            "parent": new_stage_spec.get("parent"),
            "children": valid_children,
        })
        # Update parent's children list so the tree stays consistent in both
        # directions (child.parent → parent, parent.children → child). The
        # `parent_found` guard catches LLM proposals naming a parent that
        # doesn't exist; in that case we log and proceed with parent=null
        # rather than silently leaving the child orphaned.
        parent_id = new_stage_spec.get("parent")
        if parent_id:
            parent_found = False
            for s in skeleton_doc["stages"]:
                if s["id"] == parent_id:
                    parent_found = True
                    if new_id not in s.get("children", []):
                        s.setdefault("children", []).append(new_id)
                    break
            if not parent_found:
                logger.warning(
                    "apply_skeleton_add_stage: new stage %r references parent "
                    "%r which does not exist in skeleton; clearing parent to null",
                    new_id, parent_id,
                )
                # Find the just-added new stage and null its parent.
                for s in skeleton_doc["stages"]:
                    if s["id"] == new_id:
                        s["parent"] = None
                        break

    # Ensure mapping_doc has the new stage.
    _ensure_stage(mapping_doc, new_id)

    invalidated: list[str] = []
    for mv in proposal.get("move_members", []):
        qn = mv["qualname"]
        from_sid = mv["from_stage"]
        moved = apply_reassignment(mapping_doc, qn, [from_sid], [new_id])
        invalidated.extend(moved)
    return invalidated


def apply_skeleton_remove_stage(
    skeleton_doc: dict,
    mapping_doc: dict,
    proposal: dict,
) -> list[str]:
    """Remove a stage; move its members to a target stage (or unmapped).

    Schema:
      proposal = {
        "action": "remove_stage",
        "stage_id": "...",
        "move_to": "..." | null  (null → drop members, mark as unmapped)
      }
    """
    target_id = proposal["stage_id"]
    move_to = proposal.get("move_to")

    invalidated: list[str] = []
    if target_id in mapping_doc.get("stages", {}):
        members = list(mapping_doc["stages"][target_id].get("members", []))
        for m in members:
            qn = m["qualname"]
            if move_to:
                apply_reassignment(mapping_doc, qn, [target_id], [move_to])
            else:
                # No target — just remove from this stage. The qualname is
                # invalidated below so next iter's Pass A will re-classify it.
                # We deliberately do NOT add to unmapped_functions, because the
                # function is transiently homeless, not permanently unmapped.
                _remove_member(mapping_doc["stages"][target_id], qn)
            invalidated.append(qn)
        del mapping_doc["stages"][target_id]

    # Remove from skeleton.
    skeleton_doc["stages"] = [
        s for s in skeleton_doc.get("stages", []) if s["id"] != target_id
    ]
    # Strip from any parent's children list.
    for s in skeleton_doc.get("stages", []):
        if target_id in s.get("children", []):
            s["children"].remove(target_id)

    return invalidated


def apply_skeleton_merge_stages(
    skeleton_doc: dict,
    mapping_doc: dict,
    proposal: dict,
) -> list[str]:
    """Merge several stages into one.

    Schema:
      proposal = {
        "action": "merge_stages",
        "stages_to_merge": ["side-S1.1", "side-S1.2", "side-S1.3"],
        "into": "side-S1"
      }
    """
    sources = proposal["stages_to_merge"]
    target = proposal["into"]
    invalidated: list[str] = []

    # Ensure target exists in skeleton (if not, synthesize it from the first
    # source so the resulting skeleton is consistent).
    skel_ids = {s["id"] for s in skeleton_doc.get("stages", [])}
    if target not in skel_ids:
        if sources and sources[0] in skel_ids:
            template = next(s for s in skeleton_doc["stages"] if s["id"] == sources[0])
            skeleton_doc.setdefault("stages", []).append({
                "id": target,
                "title": template.get("title", target),
                "description": (
                    f"Merged stage formed from {sources}."
                ),
                "parent": template.get("parent"),
                "children": [],
            })
            logger.info(
                "apply_skeleton_merge_stages: synthesized target stage %r "
                "(was not in skeleton)", target,
            )
        else:
            logger.warning(
                "apply_skeleton_merge_stages: target %r missing from skeleton "
                "and no template source to clone from; aborting",
                target,
            )
            return invalidated

    for src in sources:
        if src == target:
            continue
        if src not in mapping_doc.get("stages", {}):
            continue
        members = list(mapping_doc["stages"][src].get("members", []))
        for m in members:
            apply_reassignment(mapping_doc, m["qualname"], [src], [target])
            invalidated.append(m["qualname"])
        del mapping_doc["stages"][src]
        skeleton_doc["stages"] = [
            s for s in skeleton_doc.get("stages", []) if s["id"] != src
        ]
        for s in skeleton_doc.get("stages", []):
            if src in s.get("children", []):
                s["children"].remove(src)
    return invalidated


def _first_claim_of(new_stages: list[dict], qualname: str) -> str:
    for spec in new_stages:
        if qualname in spec.get("members", []):
            return spec.get("id", "?")
    return "?"


def apply_skeleton_split_stage(
    skeleton_doc: dict,
    mapping_doc: dict,
    proposal: dict,
) -> list[str]:
    """Split a stage into N new stages with explicit member assignments.

    Schema:
      proposal = {
        "action": "split_stage",
        "source_stage": "stage-4.3",
        "new_stages": [
          {"id": "stage-4.3", "title": "...", "description": "...", "members": [qualname, ...]},
          {"id": "subsys-parser-internal", "title": "...", ...}
        ]
      }
    """
    source = proposal["source_stage"]
    invalidated: list[str] = []

    # Original members list, snapshot.
    original_members = list(
        mapping_doc.get("stages", {}).get(source, {}).get("members", [])
    )

    # For each new stage, ensure skeleton entry, ensure mapping entry, populate.
    for new_stage_spec in proposal["new_stages"]:
        nid = new_stage_spec["id"]
        if not any(s["id"] == nid for s in skeleton_doc.get("stages", [])):
            skeleton_doc.setdefault("stages", []).append({
                "id": nid,
                "title": new_stage_spec.get("title", nid),
                "description": new_stage_spec.get("description", ""),
                "parent": new_stage_spec.get("parent"),
                "children": new_stage_spec.get("children", []),
            })
        _ensure_stage(mapping_doc, nid)

    # Move members per spec. Warn if a qualname is claimed by multiple
    # new_stages (only the first claim moves; subsequent ones silently miss).
    claims_so_far: set[str] = set()
    for new_stage_spec in proposal["new_stages"]:
        nid = new_stage_spec["id"]
        for qn in new_stage_spec.get("members", []):
            if qn in claims_so_far:
                logger.warning(
                    "split_stage: qualname %r claimed by multiple new_stages; "
                    "only the first claim ('%s') is applied",
                    qn, _first_claim_of(proposal["new_stages"], qn),
                )
                continue
            claims_so_far.add(qn)
            apply_reassignment(mapping_doc, qn, [source], [nid])
            invalidated.append(qn)

    # If source stage no longer in new_stages list, remove it.
    new_ids = {s["id"] for s in proposal["new_stages"]}
    if source not in new_ids:
        leftover = mapping_doc.get("stages", {}).get(source, {}).get("members", [])
        if leftover:
            logger.warning(
                "split_stage left %d members in source %s; keeping source stage",
                len(leftover), source,
            )
        else:
            mapping_doc["stages"].pop(source, None)
            skeleton_doc["stages"] = [
                s for s in skeleton_doc.get("stages", []) if s["id"] != source
            ]
            # Splitting a stage removes the source from skeleton.stages, but
            # parent stages may still list it in their `children` array. If
            # we don't prune those back-references the skeleton becomes
            # inconsistent — a parent pointing to a child that no longer
            # exists — and downstream tools that walk the tree (Phase 3
            # renderer, validators) hit dangling pointers.
            for s in skeleton_doc.get("stages", []):
                if source in s.get("children", []):
                    s["children"].remove(source)

    return invalidated


def apply_skeleton_change(
    skeleton_doc: dict,
    mapping_doc: dict,
    proposal: dict,
) -> list[str]:
    """Dispatch on proposal['action']."""
    action = proposal.get("action")
    dispatch = {
        "add_stage": apply_skeleton_add_stage,
        "remove_stage": apply_skeleton_remove_stage,
        "merge_stages": apply_skeleton_merge_stages,
        "split_stage": apply_skeleton_split_stage,
    }
    fn = dispatch.get(action)
    if fn is None:
        logger.warning("apply_skeleton_change: unknown action %s", action)
        return []
    return fn(skeleton_doc, mapping_doc, proposal)


# ─── Apply: Region revision (Pass D) ──────────────────────────────────────────


def apply_region_revision(
    mapping_doc: dict,
    proposal: dict,
    source_root: Path,
) -> list[str]:
    """Apply region modifications for one function.

    Schema:
      proposal = {
        "qualname": "...",
        "actions": [
          {"action": "merge", "region_indices": [i, j], ...},
          {"action": "split", "region_index": i, "at_line": N, ...},
          {"action": "reassign_stage", "region_index": i, "new_stage": "..."},
          {"action": "drop", "region_index": i},
        ]
      }
    Note: indices refer to the regions in source-order under this qualname.
    """
    qn = proposal["qualname"]
    # Gather all region-type members for this qualname, with their stage.
    items: list[tuple[str, dict]] = []  # (stage_id, member dict)
    for sid, info in mapping_doc.get("stages", {}).items():
        for m in info.get("members", []):
            if m.get("qualname") == qn and m.get("type") == "region":
                items.append((sid, m))
    items.sort(key=lambda p: (p[1].get("line_range") or [0])[0])

    # Build a working copy with a STABLE identity key (original_index) per
    # region. LLM gives us indices that refer to this original list; as we
    # merge/split, the position in `work` drifts but original_index stays the
    # same. We always resolve "region_indices" against the original keys.
    work = [
        {
            "_orig_idx": orig_idx,
            "stage_id": sid,
            "line_range": list(m.get("line_range") or []),
            "purpose": m.get("purpose", ""),
            "file": m.get("file"),
            "_alive": True,
        }
        for orig_idx, (sid, m) in enumerate(items)
    ]

    def _resolve(orig_idx: int) -> int | None:
        """Map an LLM-given original index to its current position in work."""
        for pos, w in enumerate(work):
            if w.get("_orig_idx") == orig_idx and w.get("_alive"):
                return pos
        return None

    for action in proposal.get("actions", []):
        kind = action.get("action")
        if kind == "merge":
            orig_idxs = sorted(action.get("region_indices", []))
            positions = [p for p in (_resolve(i) for i in orig_idxs) if p is not None]
            if len(positions) >= 2:
                low = positions[0]
                # New combined range
                new_start = min(work[p]["line_range"][0] for p in positions)
                new_end = max(work[p]["line_range"][1] for p in positions)
                work[low]["line_range"] = [new_start, new_end]
                work[low]["purpose"] = action.get("purpose", work[low]["purpose"])
                # Mark higher ones dead (don't pop, to preserve _orig_idx lookups).
                for p in positions[1:]:
                    work[p]["_alive"] = False
        elif kind == "split":
            orig_i = action.get("region_index")
            at = action.get("at_line")
            pos = _resolve(orig_i) if orig_i is not None else None
            if pos is not None and at:
                old = work[pos]
                start, end = old["line_range"]
                new_left = {k: v for k, v in old.items() if k != "_orig_idx"}
                new_right = {k: v for k, v in old.items() if k != "_orig_idx"}
                new_left["line_range"] = [start, at]
                new_right["line_range"] = [at + 1, end]
                if "left_stage" in action:
                    new_left["stage_id"] = action["left_stage"]
                if "right_stage" in action:
                    new_right["stage_id"] = action["right_stage"]
                if "left_purpose" in action:
                    new_left["purpose"] = action["left_purpose"]
                if "right_purpose" in action:
                    new_right["purpose"] = action["right_purpose"]
                # Mark the old slot dead, append both halves with new identities.
                work[pos]["_alive"] = False
                # New synthetic identities use negative ints so they don't
                # collide with LLM-given originals.
                next_synth = -len([w for w in work if w["_orig_idx"] < 0]) - 1
                new_left["_orig_idx"] = next_synth
                new_left["_alive"] = True
                new_right["_orig_idx"] = next_synth - 1
                new_right["_alive"] = True
                work.append(new_left)
                work.append(new_right)
        elif kind == "reassign_stage":
            orig_i = action.get("region_index")
            new_stage = action.get("new_stage")
            pos = _resolve(orig_i) if orig_i is not None else None
            if pos is not None and new_stage:
                work[pos]["stage_id"] = new_stage
        elif kind == "drop":
            orig_i = action.get("region_index")
            pos = _resolve(orig_i) if orig_i is not None else None
            if pos is not None:
                work[pos]["_alive"] = False

    # Strip dead entries and the bookkeeping key.
    work = [
        {k: v for k, v in w.items() if not k.startswith("_")}
        for w in work
        if w.get("_alive")
    ]
    # Sort by line_range start for stable final order.
    work.sort(key=lambda w: w.get("line_range") or [0])

    # Wipe original region members for this qualname.
    for sid, info in mapping_doc.get("stages", {}).items():
        info["members"] = [
            m for m in info.get("members", [])
            if not (m.get("qualname") == qn and m.get("type") == "region")
        ]

    # Re-add from work
    for w in work:
        sid = w["stage_id"]
        stage = _ensure_stage(mapping_doc, sid)
        file = w.get("file")
        lr = w["line_range"]
        sha1 = _sha1_of_range(source_root / file, lr[0], lr[1]) if file and lr else ""
        stage["members"].append({
            "qualname": qn,
            "type": "region",
            "file": file,
            "line_range": lr,
            "sha1": sha1,
            "purpose": w.get("purpose", ""),
        })

    return [qn]


# ─── Apply: Outlier removal (Pass E) ──────────────────────────────────────────


def apply_outlier_reassign(
    mapping_doc: dict,
    proposal: dict,
) -> list[str]:
    """Move a member out of its current stage into a new one.

    Schema:
      proposal = {
        "qualname": "...",
        "from_stage": "...",
        "to_stage": "..."
      }
    """
    return apply_reassignment(
        mapping_doc,
        proposal["qualname"],
        [proposal["from_stage"]],
        [proposal["to_stage"]],
    )


# ─── State hashing (convergence detection) ────────────────────────────────────


def populate_unmapped(mapping_doc: dict, graph: dict) -> None:
    """Compute and write mapping_doc['unmapped_functions'].

    Categories assigned by simple heuristics:
      - synthetic_dataclass: graph nodes flagged ``synthetic`` (e.g. dataclass
        auto-__init__).
      - api_surface: short, public-named methods (≤5 lines, no leading underscore).
      - dead: internal function with n_callers == 0 inside our codebase.
      - missing_llm_output: anything else that's not currently in any stage.
    """
    assigned: set[str] = set()
    for info in mapping_doc.get("stages", {}).values():
        for m in info.get("members", []):
            assigned.add(m["qualname"])

    # Preserve existing purposes from the prior unmapped list, keyed by qualname.
    prior_purposes: dict[str, str] = {
        u.get("qualname"): u.get("purpose", "")
        for u in mapping_doc.get("unmapped_functions", [])
        if u.get("qualname") and u.get("purpose")
    }

    unmapped: list[dict] = []
    for node in graph.get("nodes", {}).values():
        if node.get("kind") != "internal":
            continue
        qn = node.get("qualname")
        if not qn:
            continue
        # Synthetic dataclasses are always unmapped; they have no real code.
        if node.get("synthetic"):
            unmapped.append({
                "qualname": qn,
                "file": node.get("file"),
                "reason": "synthetic_dataclass",
            })
            continue
        if qn in assigned:
            continue
        name = node.get("name", "")
        line_count = (node.get("line_end") or 0) - (node.get("line_start") or 0)
        n_callers = node.get("n_callers", 0)
        if not name.startswith("_") and line_count <= 5:
            reason = "api_surface"
        elif n_callers == 0:
            reason = "dead"
        else:
            reason = "missing_llm_output"
        entry = {
            "qualname": qn,
            "file": node.get("file"),
            "reason": reason,
        }
        if qn in prior_purposes:
            entry["purpose"] = prior_purposes[qn]
        unmapped.append(entry)

    mapping_doc["unmapped_functions"] = sorted(
        unmapped, key=lambda u: (u["qualname"], u.get("file") or "")
    )


def dedup_members(mapping_doc: dict) -> int:
    """Within each stage, if (qualname, type=function) coexists with one or more
    (qualname, type=region), drop the function-level entry. Returns count
    dropped.

    Rationale: when a function is split into regions for a stage, the function-
    level entry is redundant (the regions cover it). Function-level entries
    may still exist in OTHER stages (e.g. a parent stage) — only same-stage
    overlap is removed.
    """
    dropped = 0
    for stage_id, info in mapping_doc.get("stages", {}).items():
        members = info.get("members", [])
        regioned_qualnames = {
            m["qualname"]
            for m in members
            if m.get("type") == "region"
        }
        new_members = []
        for m in members:
            if m.get("type") == "function" and m["qualname"] in regioned_qualnames:
                dropped += 1
                continue
            new_members.append(m)
        info["members"] = new_members
    return dropped


def state_hash(skeleton_doc: dict, mapping_doc: dict) -> str:
    """Stable hash over (skeleton structure, mapping member identities).

    Used for convergence detection. Order-insensitive over members within a
    stage, but sensitive to which stages exist and which qualnames are in each.
    """
    skel_part = sorted(s["id"] for s in skeleton_doc.get("stages", []))
    map_part = []
    for stage_id, info in sorted(mapping_doc.get("stages", {}).items()):
        member_keys = sorted(
            (m["qualname"], m.get("type"), tuple(m.get("line_range") or []))
            for m in info.get("members", [])
        )
        map_part.append((stage_id, member_keys))
    combined = (tuple(skel_part), tuple(map_part))
    return hashlib.sha1(repr(combined).encode("utf-8")).hexdigest()
