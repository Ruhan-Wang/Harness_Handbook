# -*- coding: utf-8 -*-
"""Step 3.5 — Stage member narrative ordering.

For each stage with ≥2 members, ask the LLM to:
  1. Decide structure: "linear" | "branched" | "unordered"
  2. Provide an ordering compatible with that structure
  3. Editor Critic reviews

Then mechanically reorder the stage's members list and annotate each member
with a ``narrative_section`` (for branched/unordered).

Caching: per-stage fingerprint based on member identity (not purpose/order).
Re-runs skip LLM when stage's membership is unchanged.

Run as a standalone tool on an existing mapping.yaml, or invoke
``order_all_stages(...)`` from iterate_phase2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_client import Api  # noqa: E402
from critic import actor_critic_loop, summarize_result  # noqa: E402
from skeleton_yaml import stage_short_descriptions  # noqa: E402

logger = logging.getLogger(__name__)


# ─── Actor prompt ─────────────────────────────────────────────────────────────


ACTOR_RULES = """You are deciding the NARRATIVE READING ORDER of members within one stage of a software handbook.

The members of this stage are listed below. Choose how they should be presented to a reader.

STRUCTURE TYPES
- "linear" — members have a clear execution-flow sequence (orchestrator first, then helpers in call order). Most stages fit this.
- "branched" — there's a primary path plus parallel alternative paths (e.g., happy path + 2 fallback recovery paths). Use only when branches are genuinely independent alternatives, not sequential steps.
- "unordered" — members are independent utilities with no narrative sequence (typical for crosscut stages). Use only when an artificial order would mislead readers.

Default to "linear" unless the content clearly fits branched or unordered.

OUTPUT FORMAT
Return ONLY a single JSON object inside a ```json fenced block:

{
  "structure": "linear" | "branched" | "unordered",
  "rationale": "<one-sentence summary of the narrative this order creates>",

  // exactly ONE of the next three groups, matching structure:

  // when structure == "linear":
  "order": [<1-based member indices, every input index exactly once>]

  // when structure == "branched":
  "spine":    [<indices forming the primary path>],
  "branches": [
    {"label": "<branch name>", "members": [<indices>]},
    ...
  ]

  // when structure == "unordered":
  "groups": [
    {"label": "<group name>", "members": [<indices>]},
    ...
  ]
}

INVARIANTS
- Every input member index appears exactly once across the relevant field(s) for the chosen structure.
- For "branched": spine ∪ branches.members covers all indices, with no overlaps.
- For "unordered": groups.members covers all indices, with no overlaps.
"""


def build_actor_prompt(
    stage_id: str,
    stage_title: str,
    stage_desc: str,
    members: list[dict],
    inner_calls: list[tuple[int, int]],
) -> str:
    member_lines = []
    for i, m in enumerate(members, start=1):
        qn = m["qualname"]
        tp = m.get("type", "")
        lr = m.get("line_range") or []
        purpose = (m.get("purpose") or "").replace("\n", " ")[:200]
        member_lines.append(
            f"{i}. {qn:<50}  type={tp:<8} line_range={lr}\n"
            f"   purpose: {purpose}"
        )

    if inner_calls:
        edge_lines = "\n".join(f"  ({a}) → ({b})" for a, b in inner_calls)
        edges_block = (
            "Call relationships among these members (caller → callee):\n"
            + edge_lines
        )
    else:
        edges_block = "Call relationships among these members: (none observed)"

    parts = [
        ACTOR_RULES,
        "",
        f"## Stage being ordered",
        f"id:    {stage_id}",
        f"title: {stage_title}",
        f"desc:  {stage_desc}",
        "",
        f"## Members ({len(members)} total, numbered 1..{len(members)})",
        "",
        "\n\n".join(member_lines),
        "",
        edges_block,
        "",
        "Return only the JSON block.",
    ]
    return "\n".join(parts)


def build_review_evidence(
    stage_id: str,
    stage_title: str,
    stage_desc: str,
    members: list[dict],
    inner_calls: list[tuple[int, int]],
) -> str:
    """Same payload the Actor sees — Critic uses it to verify the ordering matches the narrative."""
    member_lines = []
    for i, m in enumerate(members, start=1):
        purpose = (m.get("purpose") or "").replace("\n", " ")[:180]
        member_lines.append(
            f"{i:>2}. {m['qualname']:<48}  type={m.get('type','')}  "
            f"line_range={m.get('line_range')}\n      purpose: {purpose}"
        )
    edges_md = (
        "\n".join(f"  ({a}) → ({b})" for a, b in inner_calls)
        if inner_calls else "  (none)"
    )
    parts = [
        f"Stage: {stage_id} — {stage_title}",
        f"Description: {stage_desc}",
        "",
        f"Members ({len(members)}):",
        "\n".join(member_lines),
        "",
        "Inner call relationships:",
        edges_md,
    ]
    return "\n".join(parts)


_PROPOSAL_SCHEMA_HINT = """The proposal must follow:
{
  "structure": "linear" | "branched" | "unordered",
  "rationale": "...",
  "order":     [int, ...]                # iff structure == "linear"
  "spine":     [int, ...],               # iff structure == "branched"
  "branches":  [{"label": str, "members": [int, ...]}, ...],
  "groups":    [{"label": str, "members": [int, ...]}, ...]   # iff structure == "unordered"
}
Every input index appears exactly once in the structure-specific field(s)."""


# ─── Inner call edges (kept short — just same-stage caller→callee pairs) ──────


def _build_inner_calls(
    members: list[dict], graph: dict
) -> list[tuple[int, int]]:
    qualname_indices: dict[str, list[int]] = {}
    for i, m in enumerate(members, start=1):
        qualname_indices.setdefault(m["qualname"], []).append(i)

    id_to_qualname = {nid: n.get("qualname") for nid, n in graph["nodes"].items()}
    pairs: set[tuple[int, int]] = set()
    for edge in graph.get("edges", []):
        caller_q = id_to_qualname.get(edge.get("caller_id"))
        callee_q = id_to_qualname.get(edge.get("callee_id"))
        if not caller_q or not callee_q:
            continue
        if caller_q not in qualname_indices or callee_q not in qualname_indices:
            continue
        for ci in qualname_indices[caller_q]:
            for ki in qualname_indices[callee_q]:
                if ci != ki:
                    pairs.add((ci, ki))
    return sorted(pairs)


# ─── Permutation validation ───────────────────────────────────────────────────


def _validate_ordering(parsed: dict | None, n: int) -> tuple[str, list[int], list[tuple[str, list[int]]]] | None:
    """Returns (structure, flat_order, sectioned) on success, else None.

      flat_order: a flat 1..N permutation in narrative-reading order
      sectioned:  list of (section_label, [indices in that section])
    For linear, sectioned = [("", flat_order)].
    """
    if not isinstance(parsed, dict):
        return None
    structure = parsed.get("structure")
    if structure not in ("linear", "branched", "unordered"):
        return None

    if structure == "linear":
        order = parsed.get("order")
        if not isinstance(order, list) or len(order) != n:
            return None
        try:
            cleaned = [int(x) for x in order]
        except (TypeError, ValueError):
            return None
        if sorted(cleaned) != list(range(1, n + 1)):
            return None
        return structure, cleaned, [("", cleaned)]

    if structure == "branched":
        spine = parsed.get("spine") or []
        branches = parsed.get("branches") or []
        if not isinstance(spine, list) or not isinstance(branches, list):
            return None
        try:
            spine = [int(x) for x in spine]
        except (TypeError, ValueError):
            return None
        # Branched without a primary path collapses to "unordered" — reject so
        # the LLM is asked to pick a real structure.
        if not spine:
            return None
        if not branches:
            return None
        flat: list[int] = list(spine)
        sectioned: list[tuple[str, list[int]]] = [("spine", spine)]
        for br in branches:
            if not isinstance(br, dict):
                return None
            label = str(br.get("label") or "")
            try:
                m = [int(x) for x in (br.get("members") or [])]
            except (TypeError, ValueError):
                return None
            flat.extend(m)
            sectioned.append((f"branch: {label}", m))
        if sorted(flat) != list(range(1, n + 1)):
            return None
        return structure, flat, sectioned

    # unordered
    groups = parsed.get("groups") or []
    if not isinstance(groups, list):
        return None
    flat: list[int] = []
    sectioned: list[tuple[str, list[int]]] = []
    for grp in groups:
        if not isinstance(grp, dict):
            return None
        label = str(grp.get("label") or "")
        try:
            m = [int(x) for x in (grp.get("members") or [])]
        except (TypeError, ValueError):
            return None
        flat.extend(m)
        sectioned.append((f"group: {label}", m))
    if sorted(flat) != list(range(1, n + 1)):
        return None
    return structure, flat, sectioned


# ─── Fallback ─────────────────────────────────────────────────────────────────


def _line_range_pair(m: dict) -> tuple[int, int]:
    """Return (start, end) line numbers, tolerating missing or short line_range."""
    lr = m.get("line_range") or []
    if not isinstance(lr, (list, tuple)):
        return (0, 0)
    start = lr[0] if len(lr) >= 1 and isinstance(lr[0], int) else 0
    end = lr[1] if len(lr) >= 2 and isinstance(lr[1], int) else start
    return (start, end)


def _fallback_order(members: list[dict]) -> tuple[str, list[int], list[tuple[str, list[int]]]]:
    """Mechanical fallback when LLM/Critic fails: order by (file, line_start, type).

    Returns a *linear* structure — the canonical set is "linear"|"branched"|
    "unordered", and downstream consumers should not have to special-case a
    fourth value. Degraded status is recorded separately in the ``source``
    field as "fallback".
    """
    type_rank = {"function": 0, "region": 1}
    def _key(pair):
        m = pair[1]
        start, end = _line_range_pair(m)
        return (
            m.get("file") or "",
            start,
            type_rank.get(m.get("type"), 2),
            end,
        )
    indexed = sorted(enumerate(members, start=1), key=_key)
    order = [i for i, _ in indexed]
    return "linear", order, [("", order)]


# ─── Cache ────────────────────────────────────────────────────────────────────


def _member_identity(m: dict) -> tuple:
    return (m["qualname"], m.get("type"), tuple(m.get("line_range") or []))


def _identity_to_dict(ident: tuple) -> dict:
    return {"qualname": ident[0], "type": ident[1], "line_range": list(ident[2])}


def _dict_to_identity(d: dict) -> tuple:
    return (d["qualname"], d.get("type"), tuple(d.get("line_range") or []))


def _fingerprint(stage_id: str, members: list[dict]) -> str:
    items = sorted(_member_identity(m) for m in members)
    return hashlib.sha1(
        json.dumps([stage_id, items], ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _cache_path(cache_dir: Path, stage_id: str) -> Path:
    safe = stage_id.replace("/", "_")
    return cache_dir / f"{safe}.json"


def _load_cache(cache_dir: Path, stage_id: str, fp: str) -> dict | None:
    p = _cache_path(cache_dir, stage_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if data.get("fingerprint") != fp:
        return None
    # Reject legacy positional-index cache entries (schema migration), and
    # reject entries where the identity fields are present but malformed
    # (e.g. set to null by a hand-edit or a half-written file).
    if not isinstance(data.get("order_identities"), list):
        return None
    if not isinstance(data.get("section_identities"), list):
        return None
    return data


def _save_cache(
    cache_dir: Path,
    stage_id: str,
    fp: str,
    structure: str,
    flat: list[int],
    sectioned: list[tuple[str, list[int]]],
    members: list[dict],
    rationale: str,
    source: str,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    order_identities = [_identity_to_dict(_member_identity(members[i - 1])) for i in flat]
    section_identities = [
        {
            "label": lbl,
            "identities": [
                _identity_to_dict(_member_identity(members[i - 1])) for i in idxs
            ],
        }
        for lbl, idxs in sectioned
    ]
    payload = {
        "stage_id": stage_id,
        "fingerprint": fp,
        "structure": structure,
        "order_identities": order_identities,
        "section_identities": section_identities,
        "rationale": rationale,
        "source": source,  # "llm" | "fallback" | "cache"
    }
    _cache_path(cache_dir, stage_id).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _cache_to_indices(
    cached: dict, members: list[dict]
) -> tuple[list[int], list[tuple[str, list[int]]]] | None:
    """Map cached identities back to 1-based indices into the current members list.

    Returns ``(flat, sectioned)`` or ``None`` if any identity is missing
    (shouldn't happen when fingerprints match, but defensive).
    """
    identity_to_idx: dict[tuple, int] = {}
    for i, m in enumerate(members, start=1):
        identity_to_idx[_member_identity(m)] = i

    try:
        flat = [identity_to_idx[_dict_to_identity(d)] for d in cached["order_identities"]]
        sectioned = []
        for s in cached["section_identities"]:
            if not isinstance(s, dict) or not isinstance(s.get("identities"), list):
                return None
            sectioned.append(
                (s.get("label", ""),
                 [identity_to_idx[_dict_to_identity(d)] for d in s["identities"]])
            )
    except (KeyError, TypeError):
        return None

    n = len(members)
    if sorted(flat) != list(range(1, n + 1)):
        return None
    return flat, sectioned


# ─── Apply ordering ───────────────────────────────────────────────────────────


def _apply_ordering(
    members: list[dict],
    flat: list[int],
    sectioned: list[tuple[str, list[int]]],
) -> list[dict]:
    """Reorder members per the flat 1-based index list; attach narrative_section
    field when sectioning is non-trivial.
    """
    index_to_label: dict[int, str] = {}
    has_sections = len(sectioned) > 1 or (sectioned and sectioned[0][0])
    if has_sections:
        for label, idxs in sectioned:
            for i in idxs:
                index_to_label[i] = label

    out = []
    for i in flat:
        m = dict(members[i - 1])
        # Always clear any stale narrative_section from a prior ordering pass
        # before deciding whether to write a fresh one — a stage that used to
        # be branched/unordered but is now linear must shed its labels.
        m.pop("narrative_section", None)
        if i in index_to_label and index_to_label[i]:
            m["narrative_section"] = index_to_label[i]
        out.append(m)
    return out


# ─── Top-level: order one stage ───────────────────────────────────────────────


def order_one_stage(
    api: Api,
    stage_id: str,
    stage_title: str,
    stage_desc: str,
    members: list[dict],
    graph: dict,
    cache_dir: Path,
    force: bool = False,
) -> dict:
    """Returns:
        {
          "structure": str,
          "rationale": str,
          "members": [reordered member dicts],
          "source": "llm" | "fallback" | "cache" | "trivial",
        }
    """
    n = len(members)
    if n == 0:
        return {"structure": "empty", "rationale": "", "members": [], "source": "trivial"}
    if n == 1:
        m = dict(members[0])
        # A solo member carries no narrative section — strip any label that
        # leaked in from a prior pass when this stage had more members.
        m.pop("narrative_section", None)
        return {
            "structure": "linear", "rationale": "Single-member stage.",
            "members": [m], "source": "trivial",
        }

    fp = _fingerprint(stage_id, members)
    if not force:
        cached = _load_cache(cache_dir, stage_id, fp)
        if cached:
            decoded = _cache_to_indices(cached, members)
            if decoded is not None:
                flat, sectioned = decoded
                return {
                    "structure": cached["structure"],
                    "rationale": cached["rationale"],
                    "members": _apply_ordering(members, flat, sectioned),
                    "source": "cache",
                }
            logger.warning(
                "Cache for %s matched fingerprint but identities did not decode; re-running LLM.",
                stage_id,
            )

    inner_calls = _build_inner_calls(members, graph)
    actor_prompt = build_actor_prompt(
        stage_id, stage_title, stage_desc, members, inner_calls
    )
    review_evidence = build_review_evidence(
        stage_id, stage_title, stage_desc, members, inner_calls
    )
    task_context = (
        f"Ordering stage `{stage_id}` ({stage_title}) — {n} members."
    )

    result = actor_critic_loop(
        api=api,
        actor_prompt=actor_prompt,
        critic_role="editor",
        task_context=task_context,
        proposal_schema_hint=_PROPOSAL_SCHEMA_HINT,
        max_revise_rounds=1,
        review_evidence=review_evidence,
    )

    validated = None
    if result.accepted and result.final_proposal:
        validated = _validate_ordering(result.final_proposal, n)

    if validated is None:
        logger.warning(
            "Ordering for %s fell back to line_start: %s",
            stage_id, summarize_result(result, "Order"),
        )
        structure, flat, sectioned = _fallback_order(members)
        rationale = "Fallback to line-start order (LLM unavailable or invalid)."
        source = "fallback"
    else:
        structure, flat, sectioned = validated
        rationale = (result.final_proposal or {}).get("rationale", "")
        source = "llm"

    # Only cache the LLM-blessed result. Fallback is best-effort and may have
    # been triggered by a transient failure — future runs should retry rather
    # than perpetually serve the degraded ordering from cache.
    if source == "llm":
        _save_cache(cache_dir, stage_id, fp, structure, flat, sectioned, members, rationale, source)
    return {
        "structure": structure,
        "rationale": rationale,
        "members": _apply_ordering(members, flat, sectioned),
        "source": source,
    }


# ─── Top-level: order all stages in a mapping_doc ─────────────────────────────


def order_all_stages(
    api: Api,
    mapping_doc: dict,
    skeleton_doc: dict,
    graph: dict,
    cache_dir: Path,
    force: bool = False,
) -> dict[str, str]:
    """Reorder every stage's members in mapping_doc in place. Adds
    ``stage["structure"]`` and ``stage["narrative_rationale"]`` keys.

    Returns ``{stage_id: source}`` summary.
    """
    summary: dict[str, str] = {}

    # Build stage description lookup from skeleton.
    title_by_id: dict[str, str] = {}
    desc_by_id: dict[str, str] = {}
    for s in skeleton_doc.get("stages", []):
        title_by_id[s["id"]] = s.get("title", s["id"])
        desc_by_id[s["id"]] = (s.get("description") or "").split(". ")[0]

    stage_items = list(mapping_doc.get("stages", {}).items())
    total = len(stage_items)
    for idx, (stage_id, info) in enumerate(stage_items, start=1):
        members = info.get("members", [])
        title = title_by_id.get(stage_id, stage_id)
        desc = desc_by_id.get(stage_id, "")
        logger.info("  [%d/%d] %s  (n=%d)", idx, total, stage_id, len(members))
        result = order_one_stage(
            api=api,
            stage_id=stage_id,
            stage_title=title,
            stage_desc=desc,
            members=members,
            graph=graph,
            cache_dir=cache_dir,
            force=force,
        )
        info["members"] = result["members"]
        info["structure"] = result["structure"]
        info["narrative_rationale"] = result["rationale"]
        summary[stage_id] = result["source"]
        logger.info(
            "      → structure=%s  source=%s",
            result["structure"], result["source"],
        )

    return summary


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
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
    ap.add_argument("--cache-dir", type=Path,
                    default=phase2 / "cache/stage_orders")
    ap.add_argument("--force", action="store_true",
                    help="re-run LLM even when cache hit")
    args = ap.parse_args(argv)

    from skeleton_yaml import load_yaml
    skeleton_doc = load_yaml(args.skeleton)
    mapping_doc = _yaml.safe_load(args.mapping.read_text(encoding="utf-8"))
    graph = json.loads(args.graph.read_text(encoding="utf-8"))

    api = Api()
    summary = order_all_stages(
        api, mapping_doc, skeleton_doc, graph, args.cache_dir, force=args.force,
    )

    # Write back. Use the iterate_phase2 dumper for consistent style.
    from iterate_phase2 import _dump_yaml
    args.mapping.write_text(_dump_yaml(mapping_doc), encoding="utf-8")

    for stage_id, source in summary.items():
        logger.info("  %-25s  %s", stage_id, source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
