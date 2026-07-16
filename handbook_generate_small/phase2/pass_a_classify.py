# -*- coding: utf-8 -*-
"""Pass A — Context-aware per-function classification with Engineer Critic.

For each function in `invalidated`:
  1. Build a rich Actor prompt:
       function source + metadata + skeleton stages
       + caller list with their CURRENT stage assignments (if any)
       + callee list with their CURRENT stage assignments
       + the target candidate stages' current members (high-level)
  2. Run Actor → Critic (Engineer role) → ≤1 revise round.
  3. If Critic approved → apply.apply_classification to mapping_doc.

Key difference from the old llm_analyze.py:
  - Adds caller/callee/peer context to the Actor prompt.
  - Routes through critic.actor_critic_loop instead of single LLM call.
  - Reads mapping_doc as input (uses current state as context).
"""
from __future__ import annotations

import copy
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import apply  # noqa: E402
from api_client import Api  # noqa: E402
from critic import actor_critic_loop, summarize_result  # noqa: E402
from llm_analyze import (  # noqa: E402
    function_sha1,
    render_source_with_line_numbers,
)
from project_context import get_project_context  # noqa: E402
from skeleton_yaml import stage_short_descriptions  # noqa: E402

logger = logging.getLogger(__name__)


# ─── Context construction ────────────────────────────────────────────────────


def _build_caller_callee_context(
    qualname: str, graph: dict, mapping_doc: dict
) -> tuple[list[dict], list[dict]]:
    """Return (callers_info, callees_info) for the given qualname.

    Each entry: {qualname, stage_assignments (list), purpose (or None)}.
    """
    id_to_qualname = {nid: n.get("qualname") for nid, n in graph["nodes"].items()}
    qualname_to_id = {v: k for k, v in id_to_qualname.items() if v}

    # Reverse-map: qualname → set of stages where it appears
    qn_to_stages: dict[str, set[str]] = defaultdict(set)
    qn_to_purpose: dict[str, str] = {}
    for stage_id, info in mapping_doc.get("stages", {}).items():
        for m in info.get("members", []):
            qn = m["qualname"]
            qn_to_stages[qn].add(stage_id)
            if m.get("type") == "function" and m.get("purpose"):
                qn_to_purpose[qn] = m["purpose"]

    target_id = qualname_to_id.get(qualname)
    callers: list[dict] = []
    callees: list[dict] = []

    if not target_id:
        return callers, callees

    for edge in graph.get("edges", []):
        if edge.get("callee_id") == target_id:
            caller_id = edge.get("caller_id")
            caller_q = id_to_qualname.get(caller_id)
            if caller_q:
                callers.append({
                    "qualname": caller_q,
                    "stages": sorted(qn_to_stages.get(caller_q, set())),
                    "purpose": qn_to_purpose.get(caller_q, "")[:120],
                })
        if edge.get("caller_id") == target_id:
            callee_id = edge.get("callee_id")
            if callee_id.startswith("boundary:"):
                callees.append({"qualname": callee_id, "stages": [], "purpose": "(boundary)"})
            else:
                callee_q = id_to_qualname.get(callee_id)
                if callee_q:
                    callees.append({
                        "qualname": callee_q,
                        "stages": sorted(qn_to_stages.get(callee_q, set())),
                        "purpose": qn_to_purpose.get(callee_q, "")[:120],
                    })

    # Dedup
    def _dedup(lst):
        seen = set()
        out = []
        for item in lst:
            if item["qualname"] in seen:
                continue
            seen.add(item["qualname"])
            out.append(item)
        return out

    return _dedup(callers), _dedup(callees)


def _build_stage_overview(mapping_doc: dict, max_per_stage: int = 4) -> str:
    """For each stage, show a short list of its current members (qualname + 1-line purpose)."""
    lines = []
    for stage_id, info in mapping_doc.get("stages", {}).items():
        members = info.get("members", [])
        if not members:
            continue
        lines.append(f"  {stage_id} ({len(members)} member(s)):")
        # Show only function-type if available, plus 1 region preview
        function_members = [m for m in members if m.get("type") == "function"]
        region_members = [m for m in members if m.get("type") == "region"]
        shown = (function_members + region_members)[:max_per_stage]
        for m in shown:
            purpose_excerpt = (m.get("purpose") or "").split(".")[0][:90]
            type_str = "[F]" if m.get("type") == "function" else "[R]"
            lines.append(f"    {type_str} {m['qualname']:<50} — {purpose_excerpt}")
        if len(members) > max_per_stage:
            lines.append(f"    ... ({len(members) - max_per_stage} more)")
    return "\n".join(lines) if lines else "  (mapping is empty)"


# ─── Prompts ─────────────────────────────────────────────────────────────────


ACTOR_RULES = """You are analyzing one function from the codebase, and classifying it into one or more stages of a hand-authored data-flow skeleton.

GRANULARITY
- "function": the entire function is a single narrative unit. Use for short, cohesive functions (≤30 lines or with a single clear purpose).
- "region": the function contains multiple distinct narrative steps and should be split into 2–10 regions. Use for large functions with multiple decision points or sequential phases.

REGION RULES (when granularity == "region")
- Each region MUST be contiguous and end at a complete statement boundary (not mid-statement).
- Provide first_line and last_line as the EXACT text content of the first and last lines of the region.
- Each region has its own stage_id.

STAGE ASSIGNMENT
- Pick stage IDs ONLY from the list provided.
- CROSS-CUTTING utility functions (small helpers called from many places across the codebase — e.g. counting/measuring helpers, formatting/capping utilities, logging/recording markers) go to a `crosscut-*` stage ONLY. Do NOT also add them to every consuming stage.
- A function that merely calls a shared logging/metrics helper is NOT itself cross-cutting — assign it by its primary identity (the real work it does).
- For trivial public API-surface methods (tiny accessors like `name()`, `version()`, getters with no business logic): set function_assignments=[] and granularity="function" (recorded as unmapped api_surface).
- For subsystem-internal helpers (functions in a `subsys-*` file/module that are not the subsystem's public entrypoint): assign them to the main-flow stage that drives their execution, unless the skeleton has a dedicated `subsys-*` stage for them.

PURPOSE FIELD (60–150 words, structured across 5 aspects)
1. ACTION: what the function does, concretely (no "handles" / "manages")
2. INPUTS / STATE READ: arguments + instance/global state that determine behavior
3. OUTPUTS / STATE WRITTEN: return value + state mutated
4. WHEN INVOKED: who calls it, under what condition
5. NON-OBVIOUS: retry logic, fallback paths, design choices a reader would miss

CONSISTENCY WITH CONTEXT
Look at the caller/callee context provided — your classification should be consistent with their stage assignments. A function that's only called from one stage's region typically belongs near that stage.

OUTPUT
Return ONLY a JSON object inside a ```json fenced block:

{
  "qualname": "<exact qualname>",
  "purpose": "<60–150 word 5-aspect description>",
  "granularity": "function" | "region",
  "function_assignments": ["stage-X", ...],
  "regions": [
    {"line_range":[a,b], "first_line":"...", "last_line":"...",
     "purpose":"<30-80 word>", "stage_id":"..."}, ...
  ] | null,
  "file": "<file>",
  "line_range": [<start>, <end>]
}

When granularity == "function", regions = null."""


def build_actor_prompt(
    node: dict,
    source_block: str,
    skeleton_doc: dict,
    callers: list[dict],
    callees: list[dict],
    mapping_overview: str,
) -> str:
    stages_md = "\n".join(
        f"  - {sid}: {desc}"
        for sid, desc in stage_short_descriptions(skeleton_doc).items()
    )

    callers_md = "\n".join(
        f"    {c['qualname']:<50}  stages={c['stages']}  — {c['purpose']}"
        for c in callers
    ) or "    (none)"
    callees_md = "\n".join(
        f"    {c['qualname']:<50}  stages={c['stages']}  — {c['purpose']}"
        for c in callees
    ) or "    (none)"

    parts = [
        get_project_context().block("en"),
        "",
        ACTOR_RULES,
        "",
        "## Available stages (use these IDs exactly)",
        stages_md,
        "",
        "## Function metadata",
        f"  qualname:    {node['qualname']}",
        f"  file:        {node['file']}",
        f"  line_range:  [{node['line_start']}, {node['line_end']}]",
        f"  line_count:  {node['line_end'] - node['line_start']}",
        f"  is_async:    {node.get('is_async', False)}",
        f"  is_method:   {node.get('is_method', False)}",
        f"  class_name:  {node.get('class_name', '')}",
        f"  decorators:  {node.get('decorators', [])}",
        f"  n_callers:   {node.get('n_callers', 0)}",
        f"  n_callees:   {node.get('n_callees', 0)}",
        f"  reads attrs: {node.get('used_self_attrs_read', [])}",
        f"  writes attrs:{node.get('used_self_attrs_written', [])}",
        f"  signature:   {node.get('signature', '')}",
        "",
        "## Callers (functions that call THIS one, with their current stage assignments)",
        callers_md,
        "",
        "## Callees (functions THIS one calls, with their current stage assignments)",
        callees_md,
        "",
        "## Current mapping overview (already-classified members per stage)",
        mapping_overview,
        "",
        "## Function source (line-numbered)",
        "```python",
        source_block,
        "```",
        "",
        "Return only the JSON block.",
    ]
    return "\n".join(parts)


def build_review_evidence(
    node: dict,
    source_block: str,
    callers: list[dict],
    callees: list[dict],
    skeleton_doc: dict,
) -> str:
    """Same ground-truth the Actor used; given to Critic so it can verify the
    proposal against the actual code rather than just check schema."""
    stages_md = "\n".join(
        f"  - {sid}: {desc}"
        for sid, desc in stage_short_descriptions(skeleton_doc).items()
    )
    def _fmt(items: list[dict]) -> str:
        if not items:
            return "    (none)"
        lines = []
        for c in items:
            stages = c.get("stages") or []
            purpose = (c.get("purpose") or "")[:120]
            tag = f"stages={stages}" if stages else "stages=[]  (not yet classified)"
            lines.append(f"    {c['qualname']:<50}  {tag}")
            if purpose:
                lines.append(f"      └ {purpose}")
        return "\n".join(lines)

    callers_md = _fmt(callers)
    callees_md = _fmt(callees)
    parts = [
        f"Function: {node['qualname']}  ({node['file']}:{node['line_start']}-{node['line_end']})",
        f"signature: {node.get('signature', '')}",
        f"reads attrs: {node.get('used_self_attrs_read', [])}",
        f"writes attrs:{node.get('used_self_attrs_written', [])}",
        f"n_callers / n_callees: {node.get('n_callers', 0)} / {node.get('n_callees', 0)}",
        "",
        "Callers (with current stage assignments):",
        callers_md,
        "",
        "Callees (with current stage assignments):",
        callees_md,
        "",
        "Skeleton stage menu (valid stage IDs):",
        stages_md,
        "",
        "Function source (line-numbered):",
        "```python",
        source_block,
        "```",
    ]
    return "\n".join(parts)


_PROPOSAL_SCHEMA_HINT = """The proposal must follow this JSON schema:

{
  "qualname": str,
  "purpose": str (60-150 words),
  "granularity": "function" | "region",
  "function_assignments": list[str],   # stage IDs
  "regions": list[{line_range:[int,int], first_line:str, last_line:str, purpose:str, stage_id:str}] | null,
  "file": str,
  "line_range": [int, int]
}"""


# ─── Single-function classify ────────────────────────────────────────────────


def classify_one(
    api: Api,
    node: dict,
    graph: dict,
    skeleton_doc: dict,
    mapping_doc: dict,
    source_root: Path,
) -> dict:
    """Returns dict {qualname, accepted: bool, summary: str, result: ActorCriticResult}."""
    file_path = source_root / node["file"]
    src_block = render_source_with_line_numbers(
        file_path, node["line_start"], node["line_end"]
    )
    callers, callees = _build_caller_callee_context(
        node["qualname"], graph, mapping_doc
    )
    mapping_overview = _build_stage_overview(mapping_doc)

    actor_prompt = build_actor_prompt(
        node, src_block, skeleton_doc, callers, callees, mapping_overview
    )

    task_context = (
        f"Classifying function `{node['qualname']}` from file `{node['file']}` "
        f"(lines {node['line_start']}-{node['line_end']}). "
        f"Current skeleton has {len(skeleton_doc.get('stages', []))} stages."
    )
    review_evidence = build_review_evidence(
        node, src_block, callers, callees, skeleton_doc
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

    if result.accepted and result.final_proposal:
        # Ensure file + line_range are present (LLM might omit)
        prop = result.final_proposal
        prop.setdefault("file", node["file"])
        prop.setdefault("line_range", [node["line_start"], node["line_end"]])
        valid_stage_ids = {s["id"] for s in skeleton_doc.get("stages", [])}
        apply.apply_classification(
            mapping_doc, prop, source_root,
            valid_stage_ids=valid_stage_ids,
        )

    return {
        "qualname": node["qualname"],
        "accepted": result.accepted,
        "summary": summarize_result(result, f"PassA[{node['qualname']}]"),
        "result": result,
    }


# ─── Top-level entry point for the iteration driver ───────────────────────────


def run_pass_a(
    api: Api,
    graph: dict,
    skeleton_doc: dict,
    mapping_doc: dict,
    source_root: Path,
    invalidated: list[str],
    max_workers: int = 6,
) -> list[dict]:
    """Run Pass A over the invalidated function set. Returns per-function summaries.

    Note: mapping_doc is mutated by apply_classification inside classify_one.
    For concurrency, each thread classifies its own node based on a *snapshot* of
    mapping_doc context; then applies sequentially to avoid race conditions.
    """
    import concurrent.futures as cf

    nodes_by_qualname = {n["qualname"]: n for n in graph["nodes"].values()
                         if n.get("kind") == "internal" and not n.get("synthetic")
                         and n.get("line_start") is not None}

    targets = [nodes_by_qualname[qn] for qn in invalidated if qn in nodes_by_qualname]
    logger.info("Pass A: classifying %d function(s)", len(targets))

    # Workers read mapping_doc concurrently while the main thread mutates it
    # via apply.apply_classification after each future completes. Without a
    # snapshot, a worker iterating `mapping_doc["stages"].items()` (in
    # `_build_stage_overview`) at the moment main inserts a new stage key
    # (via `_ensure_stage` inside apply_classification) hits CPython's
    # `RuntimeError: dictionary changed size during iteration`. The race
    # window is brief but real once ThreadPool slots start rolling over —
    # a worker just picked off the queue runs context-building while a
    # different worker's apply is in flight on the main thread.
    #
    # Fix: one deepcopy at submit time, shared across all workers. They
    # see a consistent (stale) baseline; main thread keeps mutating the
    # live `mapping_doc` serially as futures complete. Trade-off: later-
    # scheduled workers don't benefit from earlier workers' applied
    # classifications — which is exactly the "all threads see the same
    # baseline" semantics the original comment claimed but never enforced.
    mapping_snapshot = copy.deepcopy(mapping_doc)

    summaries: list[dict] = [None] * len(targets)

    def _classify(idx_node):
        idx, node = idx_node
        # classify_one calls apply_classification at the end, mutating mapping_doc.
        # To avoid race conditions across threads, we DON'T apply here — instead
        # we return the proposal and apply serially in the main thread.
        from llm_analyze import render_source_with_line_numbers as _src
        file_path = source_root / node["file"]
        src_block = _src(file_path, node["line_start"], node["line_end"])
        callers, callees = _build_caller_callee_context(
            node["qualname"], graph, mapping_snapshot
        )
        overview = _build_stage_overview(mapping_snapshot)
        actor_prompt = build_actor_prompt(
            node, src_block, skeleton_doc, callers, callees, overview
        )
        task_context = (
            f"Classifying function `{node['qualname']}` from `{node['file']}` "
            f"(lines {node['line_start']}-{node['line_end']})."
        )
        # Critic must see the same code + context the Actor saw, so it can
        # actually judge whether the proposal matches reality.
        review_evidence = build_review_evidence(
            node, src_block, callers, callees, skeleton_doc
        )
        from critic import actor_critic_loop, summarize_result
        result = actor_critic_loop(
            api=api,
            actor_prompt=actor_prompt,
            critic_role="engineer",
            task_context=task_context,
            proposal_schema_hint=_PROPOSAL_SCHEMA_HINT,
            max_revise_rounds=1,
            review_evidence=review_evidence,
        )
        return idx, node, result

    valid_stage_ids = {s["id"] for s in skeleton_doc.get("stages", [])}
    from critic import summarize_result

    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(_classify, (i, node)): i
            for i, node in enumerate(targets)
        }
        for fut in cf.as_completed(future_to_idx):
            idx = future_to_idx[fut]
            node = targets[idx]
            try:
                _, _, result = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "Pass A worker for %s raised: %s", node["qualname"], e
                )
                summaries[idx] = {
                    "qualname": node["qualname"],
                    "accepted": False,
                    "summary": (
                        f"PassA[{node['qualname']}]: WORKER_CRASH "
                        f"({type(e).__name__})"
                    ),
                    "result": None,
                    "error": str(e),
                }
                continue

            # The LLM occasionally hallucinates the qualname field — typically
            # by copy-erroring across functions in the same class (e.g., when
            # asked about `Foo._bar` it returns `qualname: "Foo._baz"`
            # because that was the previous function it processed). Without
            # this override, apply_classification would mutate the WRONG
            # function's mapping, silently corrupting state. We trust the
            # `node` we sent in over whatever the LLM echoes back. Also
            # backfill `file` and `line_range` from `node` for the same
            # reason — these fields are pure metadata the LLM has no
            # business inventing.
            if result.accepted and result.final_proposal:
                prop = result.final_proposal
                if prop.get("qualname") != node["qualname"]:
                    logger.warning(
                        "Pass A: LLM returned qualname=%r but we asked about "
                        "%r — overriding to expected",
                        prop.get("qualname"), node["qualname"],
                    )
                    prop["qualname"] = node["qualname"]
                prop.setdefault("file", node["file"])
                prop.setdefault("line_range", [node["line_start"], node["line_end"]])
                apply.apply_classification(
                    mapping_doc, prop, source_root,
                    valid_stage_ids=valid_stage_ids,
                )
            summaries[idx] = {
                "qualname": node["qualname"],
                "accepted": result.accepted,
                "summary": summarize_result(result, f"PassA[{node['qualname']}]"),
                "result": result,
            }
            logger.info(
                "  [%d/%d] %s — %s",
                idx + 1, len(targets), node["qualname"],
                "OK" if result.accepted else "DROP",
            )

    # Backfill any leftover None slots (defensive — shouldn't trigger).
    for i in range(len(summaries)):
        if summaries[i] is None:
            summaries[i] = {
                "qualname": targets[i]["qualname"],
                "accepted": False,
                "summary": (
                    f"PassA[{targets[i]['qualname']}]: missing_summary"
                ),
                "result": None,
            }

    return summaries
