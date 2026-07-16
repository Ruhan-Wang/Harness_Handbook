# -*- coding: utf-8 -*-
"""Pass C — Skeleton doctor with 3-critic approval.

Looks at the current mapping's distribution (sizes per stage, file diversity,
orphan sub-stages) and proposes structured changes to skeleton.yaml:
  - add_stage   (e.g. introduce subsys-tmux-internal when stage-2 has 27 members from tmux_session.py)
  - remove_stage
  - merge_stages (e.g. fold side-S1.1/.2/.3 into side-S1 if each has 1 member)
  - split_stage  (e.g. stage-4.3 has 42 members; split parser internals out)

Each proposal goes through 3 Critics (Engineer + Architect + Reader). ALL must
approve before apply.apply_skeleton_change runs.
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import apply  # noqa: E402
from api_client import Api  # noqa: E402
from critic import actor_multi_critic_loop, summarize_result  # noqa: E402
from project_context import get_project_context  # noqa: E402

logger = logging.getLogger(__name__)


# ─── Mapping statistics ───────────────────────────────────────────────────────


def compute_mapping_stats(mapping_doc: dict) -> dict:
    """Compute size + file-distribution stats per stage for the Actor prompt."""
    stats = {}
    for stage_id, info in mapping_doc.get("stages", {}).items():
        members = info.get("members", [])
        file_counts = Counter(m.get("file") for m in members)
        most_common_file = file_counts.most_common(1)[0] if file_counts else ("", 0)
        stats[stage_id] = {
            "n_members": len(members),
            "n_functions": sum(1 for m in members if m.get("type") == "function"),
            "n_regions": sum(1 for m in members if m.get("type") == "region"),
            "file_distribution": dict(file_counts),
            "dominant_file": most_common_file[0],
            "dominant_file_share": (
                most_common_file[1] / max(len(members), 1)
            ),
        }
    return stats


# ─── Actor prompt ─────────────────────────────────────────────────────────────


ACTOR_RULES = """You are the SKELETON DOCTOR for a codebase handbook. Look at the current `skeleton.yaml` and the current `mapping.yaml` distribution. Propose **at most 3** structural changes to the skeleton that would improve clarity.

WHAT TO LOOK FOR
1. STAGE OVERLOAD: a stage with >20 members; especially if most come from one source file/module.
   → Propose `split_stage` to extract a subsystem-internal stage.
2. STAGE STARVATION: sub-stages with only 1 member that share a logical parent.
   → Propose `merge_stages` into the parent or sibling.
3. MISSING SUBSYSTEM STAGE: a source file/module whose internal helpers are scattered across consuming main-flow stages.
   → Propose `add_stage` for those subsystem internals.
4. DEAD STAGES: stages with 0 members for reasons OTHER than the known logger-inheritance pattern (crosscut-X3 is expected empty).
   → Propose `remove_stage`.

WHAT NOT TO DO
- Do NOT propose changes that improve nothing concrete.
- Do NOT split or merge for purely cosmetic reasons.
- Do NOT touch stages that already have 3-15 well-distributed members.
- Do NOT propose more than 3 changes per invocation.

CAUTION — PARTIAL MAPPING
The mapping may be in mid-iteration and only partially populated. Many stages legitimately end up at 0 or 1 members because the relevant code hasn't been classified yet (NOT because the stage is wrong). Therefore:
  - Do NOT propose `remove_stage` solely because a stage is currently empty or has only 1-2 members.
  - Only propose `remove_stage` if the stage's role is genuinely obsolete (e.g., the responsibility has been re-homed via a separate change in the same proposal).
  - When proposing `remove_stage`, you MUST supply a non-null `move_to` if the stage currently has any members.

ACTIONS (output schema)
Each change is one of:

{
  "action": "add_stage",
  "new_stage": {
    "id": "<unique stage id, e.g. subsys-<name>-internal>",
    "title": "<short title>",
    "description": "<2-3 sentence description>",
    "parent": "<parent stage id or null>",
    "children": []
  },
  "move_members": [
    {"qualname": "...", "from_stage": "..."},
    ...
  ]
}

{
  "action": "remove_stage",
  "stage_id": "...",
  "move_to": "<target stage id or null>"
}

{
  "action": "merge_stages",
  "stages_to_merge": ["sid1", "sid2", ...],
  "into": "<target stage id, may be one of stages_to_merge or new>"
}

{
  "action": "split_stage",
  "source_stage": "...",
  "new_stages": [
    {"id": "...", "title": "...", "description": "...", "parent": "...",
     "members": ["qualname1", "qualname2", ...]},   // REQUIRED for every new stage whose id != source_stage
    ...
  ]
}
NOTE: `split_stage` MUST move at least one qualname into a new (non-source) stage.
If you only want to introduce a stage without committing qualnames, use `add_stage`
with empty `move_members` instead. A split with no member reassignment will be rejected.

OUTPUT
Return ONLY a single JSON block:

```json
{
  "changes": [<change>, <change>, ...],   // 0..3 changes, sorted by impact
  "rationale": "<one paragraph explaining why these changes>"
}
```

If no changes are needed (skeleton is healthy), return:
```json
{"changes": [], "rationale": "Skeleton is balanced; no changes proposed."}
```"""


def build_review_evidence(mapping_doc: dict, skeleton_doc: dict) -> str:
    """Same ground truth Actor saw — given to Critics so they can verify
    proposed skeleton changes against the actual mapping distribution."""
    stats = compute_mapping_stats(mapping_doc)
    stat_lines = []
    for stage_id, s in sorted(stats.items()):
        dom_share = s["dominant_file_share"]
        dom_str = (
            f"dom file: {s['dominant_file']} ({s['file_distribution'].get(s['dominant_file'], 0)}/"
            f"{s['n_members']} = {dom_share:.0%})"
            if s["dominant_file"] else "no dominant file"
        )
        stat_lines.append(
            f"  {stage_id:<25} members={s['n_members']:>3}  "
            f"(F={s['n_functions']}, R={s['n_regions']})  {dom_str}"
        )

    skel_lines = []
    for s in skeleton_doc.get("stages", []):
        parent = s.get("parent") or "(top)"
        kids = s.get("children", [])
        desc1 = (s.get("description") or "").split(". ")[0][:80]
        skel_lines.append(
            f"  {s['id']:<25} parent={parent:<20} children={len(kids)} — {desc1}"
        )

    total_members = sum(
        len(info.get("members", []))
        for info in mapping_doc.get("stages", {}).values()
    )
    parts = [
        f"Current skeleton: {len(skeleton_doc.get('stages', []))} stages, "
        f"{total_members} total member entries across all stages.",
        "",
        "## Current skeleton",
        "\n".join(skel_lines),
        "",
        "## Current mapping distribution (per stage)",
        "\n".join(stat_lines) or "  (no stages in mapping yet)",
    ]
    return "\n".join(parts)


def build_actor_prompt(mapping_doc: dict, skeleton_doc: dict) -> str:
    stats = compute_mapping_stats(mapping_doc)
    stat_lines = []
    for stage_id, s in sorted(stats.items()):
        dom_share = s["dominant_file_share"]
        dom_str = (
            f"dom file: {s['dominant_file']} ({s['file_distribution'].get(s['dominant_file'], 0)} / "
            f"{s['n_members']} = {dom_share:.0%})"
            if s["dominant_file"]
            else "no dominant file"
        )
        stat_lines.append(
            f"  {stage_id:<25}  members={s['n_members']:>3}  "
            f"(functions={s['n_functions']}, regions={s['n_regions']})  {dom_str}"
        )

    # Skeleton summary
    skel_lines = []
    for s in skeleton_doc.get("stages", []):
        parent = s.get("parent") or "(top)"
        kids = s.get("children", [])
        desc1 = (s.get("description") or "").split(". ")[0][:80]
        skel_lines.append(
            f"  {s['id']:<25}  parent={parent:<20}  children={len(kids)}  — {desc1}"
        )

    parts = [
        get_project_context().block("en"),
        "",
        ACTOR_RULES,
        "",
        "## Current skeleton",
        "\n".join(skel_lines),
        "",
        "## Current mapping distribution",
        "\n".join(stat_lines) or "  (no stages in mapping yet)",
        "",
        "Return the JSON block.",
    ]
    return "\n".join(parts)


_PROPOSAL_SCHEMA_HINT = """The proposal must be:
{
  "changes": [
    {"action": "add_stage" | "remove_stage" | "merge_stages" | "split_stage", ...},
    ... (at most 3 changes)
  ],
  "rationale": "..."
}"""


# ─── Per-change validation (mechanical) ──────────────────────────────────────


def _validate_change(change: dict, skeleton_doc: dict, mapping_doc: dict) -> str | None:
    """Lightweight sanity check before applying. Returns error string or None."""
    action = change.get("action")
    skel_ids = {s["id"] for s in skeleton_doc.get("stages", [])}
    map_ids = set(mapping_doc.get("stages", {}).keys())

    if action == "add_stage":
        spec = change.get("new_stage", {})
        if not spec.get("id"):
            return "add_stage missing new_stage.id"
        if spec["id"] in skel_ids:
            return f"add_stage id '{spec['id']}' already exists"
        for mv in change.get("move_members", []):
            if mv.get("from_stage") not in skel_ids:
                return f"move_members.from_stage '{mv.get('from_stage')}' unknown"
            # Validate qualname is a real member of from_stage (matches the
            # existing check in split_stage). Without this, the LLM can hand
            # in descriptive placeholder strings (observed:
            # "terminus_xml_plain_parser.py::<parser-internal-members-currently
            # -mapped-to-stage-4.4>") which apply_skeleton_add_stage silently
            # fails to move — producing an empty new stage AND injecting
            # phantom qualnames into the next iter's invalidated queue.
            qn = mv.get("qualname")
            if not isinstance(qn, str) or not qn:
                return "add_stage move_members.qualname must be a non-empty string"
            from_stage_info = mapping_doc.get("stages", {}).get(mv["from_stage"], {})
            src_qns = {
                m.get("qualname") for m in from_stage_info.get("members", [])
                if m.get("qualname")
            }
            if qn not in src_qns:
                return (
                    f"add_stage move_members qualname '{qn}' is not a member "
                    f"of from_stage '{mv['from_stage']}'"
                )

    elif action == "remove_stage":
        sid = change.get("stage_id")
        if sid not in skel_ids:
            return f"remove_stage id '{sid}' not in skeleton"
        mt = change.get("move_to")
        if mt and mt not in skel_ids:
            return f"remove_stage move_to '{mt}' unknown"
        # Reject `move_to == stage_id`. apply.py's reassignment pipeline would
        # happily move the members from `sid` back into `sid` (a no-op), and
        # then the subsequent delete would orphan every one of them. The LLM
        # occasionally proposes this when it confuses "delete this stage" with
        # "delete this stage and replace it with a renamed copy"; catch it
        # here rather than letting the apply step lose data silently.
        if mt is not None and mt == sid:
            return (
                f"remove_stage move_to == stage_id ('{sid}'); cannot redirect "
                f"members into the stage being deleted"
            )
        # Don't allow destroying non-empty stages without an explicit target.
        n_members = len(mapping_doc.get("stages", {}).get(sid, {}).get("members", []))
        if n_members > 0 and not mt:
            return (
                f"remove_stage of '{sid}' (has {n_members} members) requires "
                f"non-null 'move_to'"
            )

    elif action == "merge_stages":
        srcs = change.get("stages_to_merge", [])
        target = change.get("into")
        if not target:
            return "merge_stages missing 'into'"
        # `stages_to_merge` must contain at least one source. apply.py iterates
        # the list and is a no-op when it's empty, so this would slip through
        # silently and the iter would still report the change as "applied" —
        # corrupting change_count and triggering a spurious carry-over of
        # invalidated qualnames downstream.
        if not isinstance(srcs, list) or not srcs:
            return "merge_stages stages_to_merge must be a non-empty list"
        for s in srcs:
            if s not in skel_ids:
                return f"merge_stages source '{s}' unknown"

    elif action == "split_stage":
        src = change.get("source_stage")
        if src not in skel_ids:
            return f"split_stage source '{src}' unknown"
        new_stages = change.get("new_stages", [])
        if not new_stages:
            return "split_stage needs at least one new_stage"
        # Each qualname the LLM names in a new_stages spec must already live
        # in the source stage being split. apply.py would otherwise silently
        # skip an unrecognized qualname while still emitting it in its
        # `invalidated` return — so iterate_phase2 would queue a phantom
        # qualname for Pass A that has no graph entry, and the loop would
        # waste an iter trying to classify nothing.
        src_members = mapping_doc.get("stages", {}).get(src, {}).get("members", [])
        src_qualnames = {m.get("qualname") for m in src_members if m.get("qualname")}
        for spec in new_stages:
            if not isinstance(spec, dict):
                return "split_stage new_stages entries must be dicts"
            for qn in spec.get("members", []) or []:
                if qn not in src_qualnames:
                    return (
                        f"split_stage new_stage '{spec.get('id')}' references "
                        f"qualname '{qn}' which is not a member of source "
                        f"stage '{src}'"
                    )
        # A split that moves zero qualnames is equivalent to a bare add_stage
        # but bypasses add_stage's `move_members` requirement. Observed in
        # iter_2: subsys-parser-internal was split out of stage-4.4 with no
        # `members` listed, leaving an empty new stage and 27 parser-internal
        # functions stranded in stage-4.4 across every subsequent iteration.
        # Require at least one non-source new_stage to carry members so the
        # split actually performs a reassignment.
        non_source_new = [s for s in new_stages if s.get("id") != src]
        if non_source_new and not any(s.get("members") for s in non_source_new):
            return (
                f"split_stage on '{src}' creates new stage(s) "
                f"{[s.get('id') for s in non_source_new]} but assigns no "
                f"members to any of them; use add_stage with explicit "
                f"move_members instead, or list the qualnames to move"
            )

    else:
        return f"unknown action: {action}"

    return None


# ─── Entry point ─────────────────────────────────────────────────────────────


def run_pass_c(
    api: Api,
    skeleton_doc: dict,
    mapping_doc: dict,
) -> dict:
    """Run Pass C once. Returns:
       {
         "changes_applied": list of applied change dicts,
         "changes_proposed": int,
         "changes_rejected": int,
         "invalidated": list[qualname],
         "summary": str
       }
    Mutates skeleton_doc and mapping_doc in place when changes pass critics.
    """
    actor_prompt = build_actor_prompt(mapping_doc, skeleton_doc)
    task_context = (
        f"Skeleton doctor for the {get_project_context().name} codebase. "
        f"Current: {len(skeleton_doc.get('stages', []))} stages, "
        f"{sum(len(info.get('members', [])) for info in mapping_doc.get('stages', {}).values())} total member entries."
    )
    review_evidence = build_review_evidence(mapping_doc, skeleton_doc)

    result = actor_multi_critic_loop(
        api=api,
        actor_prompt=actor_prompt,
        critic_roles=["engineer", "architect", "reader"],
        task_context=task_context,
        proposal_schema_hint=_PROPOSAL_SCHEMA_HINT,
        max_revise_rounds=1,
        review_evidence=review_evidence,
    )

    applied: list[dict] = []
    invalidated: list[str] = []
    rejected = 0

    if not result.accepted or not result.final_proposal:
        return {
            "changes_applied": [],
            "changes_proposed": 0,
            "changes_rejected": 0,
            "invalidated": [],
            "summary": summarize_result(result, "PassC"),
        }

    changes = (result.final_proposal or {}).get("changes", [])
    proposed = len(changes)

    for change in changes:
        err = _validate_change(change, skeleton_doc, mapping_doc)
        if err:
            logger.warning("Pass C rejecting change (failed validation): %s", err)
            rejected += 1
            continue
        try:
            ids = apply.apply_skeleton_change(skeleton_doc, mapping_doc, change)
            invalidated.extend(ids)
            applied.append(change)
        except Exception as e:  # noqa: BLE001
            logger.warning("Pass C apply failed: %s", e)
            rejected += 1

    return {
        "changes_applied": applied,
        "changes_proposed": proposed,
        "changes_rejected": rejected,
        "invalidated": invalidated,
        "summary": (
            f"PassC: proposed={proposed}, applied={len(applied)}, "
            f"rejected={rejected}, invalidated={len(invalidated)}"
        ),
    }
