# -*- coding: utf-8 -*-
"""Pass B — Global self-consistency check.

For each non-trivial stage, ask the LLM whether any of its pure-function
members would be a better fit in a *different* stage given everyone's purpose
descriptions. The LLM sees:
  - This stage's full member list (with purposes).
  - The skeleton menu: every stage's id/title/one-line description.
  - A size hint for each candidate stage.

Reviewers: Architect + Engineer (both must approve).

Granularity: function-type only. Region-cluttered qualnames are handled by
Pass D. Crosscut stages are read-only as a *source* — i.e., Pass B will not
propose moving a member *out of* `crosscut-*` (Pass A's rule that crosscut
utilities live in crosscut-X* is honored). Moving *into* crosscut is allowed
(corrects a Pass A miss).

Cache: per-stage fingerprint over (qualname, purpose) of all pure-function
members. Re-runs skip LLM when no purpose has changed since last run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply  # noqa: E402
from api_client import Api  # noqa: E402
from critic import actor_multi_critic_loop, summarize_result  # noqa: E402

logger = logging.getLogger(__name__)


# ─── Helpers: stage / membership classification ──────────────────────────────


def _qualname_has_regions(mapping_doc: dict, qualname: str) -> bool:
    for info in mapping_doc.get("stages", {}).values():
        for m in info.get("members", []):
            if m.get("qualname") == qualname and m.get("type") == "region":
                return True
    return False


def _pure_function_members(mapping_doc: dict, stage_id: str) -> list[dict]:
    """Members of `stage_id` that are function-type AND have no region entries
    anywhere in the mapping (i.e., not split). Pass B only audits these."""
    stage = mapping_doc.get("stages", {}).get(stage_id) or {}
    out: list[dict] = []
    for m in stage.get("members", []):
        if m.get("type") != "function":
            continue
        if _qualname_has_regions(mapping_doc, m["qualname"]):
            continue
        out.append(m)
    return out


def _is_crosscut(stage_id: str) -> bool:
    return stage_id.startswith("crosscut-")


# ─── Cache (purpose-sensitive fingerprint) ───────────────────────────────────


def _stage_fingerprint(stage_id: str, members: list[dict]) -> str:
    items = sorted(
        (m["qualname"], m.get("purpose", "") or "")
        for m in members
    )
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
    # A hand-edited or half-written cache file could contain a non-dict
    # top-level value (list, scalar). Treat as cache miss rather than crash.
    if not isinstance(data, dict):
        return None
    if data.get("fingerprint") != fp:
        return None
    return data


def _save_cache(
    cache_dir: Path,
    stage_id: str,
    fp: str,
    applied: list[dict],
    proposed: int,
    rejected: int,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "stage_id": stage_id,
        "fingerprint": fp,
        "proposed": proposed,
        "applied": applied,
        "rejected": rejected,
    }
    _cache_path(cache_dir, stage_id).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ─── Prompts ─────────────────────────────────────────────────────────────────


_MAX_PROPOSALS = 3

ACTOR_RULES = """You are auditing ONE stage of a Agent System Harness Handbook for misplaced members.

You will see the full member list of this stage (with each member's purpose),
plus a menu of all other stages and their descriptions. Decide whether any of
THIS stage's members would be a better fit in a different stage.

WHAT COUNTS AS A "BETTER FIT"
- The member's purpose clearly describes work that another stage's description
  covers, while this stage's description does not.
- The member is a small cross-cutting utility (token counting, length capping,
  recording markers, generic logging helpers) that belongs in a `crosscut-*`
  stage but ended up in a main-flow stage.
- The member is subsystem-internal (tmux, parser, asciinema, etc.) and there
  is a subsystem stage that fits it better.

WHAT DOES NOT COUNT
- Members that fit "approximately" here but might also be defensible elsewhere
  — leave them alone. Only propose a move if the destination is clearly better.
- Stylistic preferences ("would read nicer if grouped with X").
- Moves *out of* `crosscut-*` stages. Crosscut placement is authoritative;
  if you see a crosscut member, leave it.

DESTINATION RULES
- to_stage must exist in the stage menu below.
- to_stage MUST be different from from_stage.
- Do NOT propose a move to a stage that is more cross-cutting than the
  source (i.e., main-flow → crosscut is OK; crosscut → anywhere is forbidden).

CAP
- Propose AT MOST 3 reassignments per call, sorted by impact (most confident
  first). If nothing is misplaced, return an empty `proposals` list.

OUTPUT
Return ONLY a single JSON object inside a ```json fenced block:

{
  "proposals": [
    {
      "qualname": "<exact qualname of a member of this stage>",
      "from_stage": "<this stage's id>",
      "to_stage":   "<destination stage id>",
      "reason":     "<one-sentence justification grounded in the member's purpose>"
    },
    ...
  ],
  "rationale": "<one-paragraph summary; or 'no misplacements found'>"
}
"""


def _stage_menu(skeleton_doc: dict, mapping_doc: dict) -> str:
    """One line per stage: id, title, 1-sentence desc, current size."""
    sizes: dict[str, int] = {
        sid: len(info.get("members", []))
        for sid, info in mapping_doc.get("stages", {}).items()
    }
    lines = []
    for s in skeleton_doc.get("stages", []):
        sid = s["id"]
        title = s.get("title", "")
        desc1 = (s.get("description") or "").split(". ")[0][:90]
        lines.append(
            f"  {sid:<25} (n={sizes.get(sid, 0):>3})  {title} — {desc1}"
        )
    return "\n".join(lines) if lines else "  (no stages defined)"


def _members_block(members: list[dict]) -> str:
    lines = []
    for m in members:
        purpose = (m.get("purpose") or "").replace("\n", " ").strip()
        # Don't truncate aggressively — Pass B's whole job is to read these.
        if len(purpose) > 400:
            purpose = purpose[:400] + " …"
        lines.append(
            f"  - {m['qualname']:<50}  file={m.get('file','')}  "
            f"line_range={m.get('line_range')}\n      purpose: {purpose}"
        )
    return "\n".join(lines)


def build_actor_prompt(
    stage_id: str,
    stage_title: str,
    stage_desc: str,
    members: list[dict],
    skeleton_doc: dict,
    mapping_doc: dict,
) -> str:
    parts = [
        ACTOR_RULES,
        "",
        "## Stage being audited",
        f"  id:    {stage_id}",
        f"  title: {stage_title}",
        f"  desc:  {stage_desc}",
        f"  size:  {len(members)} pure-function member(s)",
        "",
        "## Members of this stage (each with their purpose)",
        _members_block(members),
        "",
        "## Stage menu (valid destination stage IDs)",
        _stage_menu(skeleton_doc, mapping_doc),
        "",
        f"At most {_MAX_PROPOSALS} reassignments. Return the JSON block only.",
    ]
    return "\n".join(parts)


def build_review_evidence(
    stage_id: str,
    stage_title: str,
    stage_desc: str,
    members: list[dict],
    skeleton_doc: dict,
    mapping_doc: dict,
) -> str:
    """The Critic must verify each proposed move against the actual stage
    descriptions and the moved member's purpose — give it the same ground
    truth the Actor saw."""
    parts = [
        f"Stage being audited: {stage_id} — {stage_title}",
        f"Description: {stage_desc}",
        f"Pure-function members ({len(members)}):",
        _members_block(members),
        "",
        "Full stage menu (id / size / title / one-line desc):",
        _stage_menu(skeleton_doc, mapping_doc),
    ]
    return "\n".join(parts)


_PROPOSAL_SCHEMA_HINT = """The proposal must be:
{
  "proposals": [
    {"qualname": str, "from_stage": str, "to_stage": str, "reason": str},
    ...
  ],
  "rationale": str
}
At most 3 entries in proposals. If nothing is misplaced, return an empty list
and rationale="no misplacements found"."""


# ─── Per-proposal mechanical validation ──────────────────────────────────────


def _validate_proposal(
    proposal: dict,
    stage_id: str,
    skeleton_doc: dict,
    mapping_doc: dict,
    member_qualnames: set[str],
) -> str | None:
    """Returns an error message or None if the move is acceptable."""
    qn = proposal.get("qualname")
    src = proposal.get("from_stage")
    dst = proposal.get("to_stage")

    if not qn or not src or not dst:
        return "missing qualname/from_stage/to_stage"
    # Type-guard before any operations that require hashing or string ops —
    # a list or dict value would TypeError downstream on the set membership
    # check and bubble out as a "crashed" stage instead of a clean rejection.
    if not isinstance(qn, str) or not isinstance(src, str) or not isinstance(dst, str):
        return "qualname/from_stage/to_stage must all be strings"
    if src != stage_id:
        return f"from_stage '{src}' does not match audited stage '{stage_id}'"
    if dst == src:
        return f"to_stage == from_stage ('{dst}')"
    if qn not in member_qualnames:
        return f"qualname '{qn}' is not a pure-function member of '{stage_id}'"
    skel_ids = {s["id"] for s in skeleton_doc.get("stages", [])}
    if dst not in skel_ids:
        return f"to_stage '{dst}' is not in the skeleton"
    if _is_crosscut(src):
        return f"refusing to move out of crosscut stage '{src}'"
    # The Architect could propose moving a function whose mapping entry
    # actually lives elsewhere right now — verify the entry is in src.
    src_stage = mapping_doc.get("stages", {}).get(src) or {}
    function_entries = [
        m for m in src_stage.get("members", [])
        if m.get("qualname") == qn and m.get("type") == "function"
    ]
    if not function_entries:
        return (
            f"'{qn}' has no function-type entry in '{src}' (may already have "
            f"moved or has regions)"
        )
    # Defensive: if regions appeared since the prompt was built, defer to Pass D.
    if _qualname_has_regions(mapping_doc, qn):
        return f"'{qn}' has region entries — defer to Pass D"
    return None


# ─── Per-stage audit ─────────────────────────────────────────────────────────


def audit_one_stage(
    api: Api,
    stage_id: str,
    stage_title: str,
    stage_desc: str,
    skeleton_doc: dict,
    mapping_doc: dict,
    cache_dir: Path,
    force: bool = False,
) -> dict:
    """Returns {applied: list[dict], proposed: int, rejected: int, source: str}.

    Mutates mapping_doc in place when proposals pass validation + critics.
    """
    # Check crosscut FIRST so the telemetry label is always honest — a
    # crosscut stage with 0–1 members should still be tagged "crosscut_skip",
    # not "trivial".
    if _is_crosscut(stage_id):
        return {"applied": [], "proposed": 0, "rejected": 0, "source": "crosscut_skip"}

    members = _pure_function_members(mapping_doc, stage_id)
    if len(members) < 2:
        # Empty / single-member: nothing to compare against.
        return {"applied": [], "proposed": 0, "rejected": 0, "source": "trivial"}

    fp = _stage_fingerprint(stage_id, members)
    if not force:
        cached = _load_cache(cache_dir, stage_id, fp)
        if cached is not None:
            return {
                "applied": [],
                "proposed": 0,
                "rejected": 0,
                "source": "cache",
            }

    actor_prompt = build_actor_prompt(
        stage_id, stage_title, stage_desc, members, skeleton_doc, mapping_doc,
    )
    review_evidence = build_review_evidence(
        stage_id, stage_title, stage_desc, members, skeleton_doc, mapping_doc,
    )
    task_context = (
        f"Pass B audit of stage `{stage_id}` ({stage_title}) — "
        f"{len(members)} pure-function members under review."
    )

    result = actor_multi_critic_loop(
        api=api,
        actor_prompt=actor_prompt,
        critic_roles=["architect", "engineer"],
        task_context=task_context,
        proposal_schema_hint=_PROPOSAL_SCHEMA_HINT,
        max_revise_rounds=1,
        review_evidence=review_evidence,
    )

    # A legitimate "no misplacements" response can be an empty dict, an empty
    # proposals list, or a dict missing the "proposals" key entirely — all of
    # which the defensive parsing below handles. Only reject the path when the
    # actor-critic loop itself failed (not accepted) or returned no payload at
    # all (None). Using `not result.final_proposal` would also reject the
    # truthy-falsy empty dict, costing an unnecessary re-ask each iteration.
    if not result.accepted or result.final_proposal is None:
        # No cache write — let next iter retry with fresh context.
        logger.info(
            "  %s: %s", stage_id, summarize_result(result, f"PassB[{stage_id}]")
        )
        return {
            "applied": [],
            "proposed": 0,
            "rejected": 0,
            "source": "llm_failed",
        }

    # The LLM is supposed to return {"proposals": [...]} but may hallucinate
    # a string, dict, or null. Defend against each shape so a malformed
    # response logs a rejection instead of crashing the iteration.
    raw_proposals = (
        result.final_proposal.get("proposals")
        if isinstance(result.final_proposal, dict)
        else None
    )
    if not isinstance(raw_proposals, list):
        raw_proposals = []
    proposals = raw_proposals[:_MAX_PROPOSALS]
    member_qualnames = {m["qualname"] for m in members}
    applied: list[dict] = []
    rejected = 0

    for p in proposals:
        if not isinstance(p, dict):
            logger.warning("  [%s] rejecting non-dict proposal: %r", stage_id, p)
            rejected += 1
            continue
        err = _validate_proposal(
            p, stage_id, skeleton_doc, mapping_doc, member_qualnames,
        )
        if err:
            logger.warning("  [%s] rejecting move: %s — %s", stage_id, p, err)
            rejected += 1
            continue
        try:
            apply.apply_reassignment(
                mapping_doc, p["qualname"], [p["from_stage"]], [p["to_stage"]],
            )
            applied.append({
                "qualname": p["qualname"],
                "from_stage": p["from_stage"],
                "to_stage": p["to_stage"],
                "reason": p.get("reason", ""),
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("  [%s] apply failed for %s: %s",
                           stage_id, p.get("qualname"), e)
            rejected += 1

    # Cache the audit (fingerprint→outcome). If nothing changed, next iter will
    # see the same fingerprint and skip; if any move was applied, the source
    # stage's member set changed so the fingerprint shifts naturally.
    _save_cache(cache_dir, stage_id, fp, applied, len(proposals), rejected)

    logger.info(
        "      → proposed=%d  applied=%d  rejected=%d",
        len(proposals), len(applied), rejected,
    )
    return {
        "applied": applied,
        "proposed": len(proposals),
        "rejected": rejected,
        "source": "llm",
    }


# ─── Top-level entry point ───────────────────────────────────────────────────


def run_pass_b(
    api: Api,
    skeleton_doc: dict,
    mapping_doc: dict,
    cache_dir: Path,
    force: bool = False,
) -> dict:
    """Audit every non-trivial stage. Returns:
        {
          "applied":   list[dict],   # all reassignments across all stages
          "proposed":  int,
          "rejected":  int,
          "invalidated": list[str],  # qualnames whose stage changed → Pass A re-runs
          "per_stage": {stage_id: source_label},
          "summary":   str,
        }
    """
    title_by_id: dict[str, str] = {}
    desc_by_id: dict[str, str] = {}
    for s in skeleton_doc.get("stages", []):
        title_by_id[s["id"]] = s.get("title", s["id"])
        desc_by_id[s["id"]] = (s.get("description") or "").split(". ")[0]

    applied_all: list[dict] = []
    proposed_total = 0
    rejected_total = 0
    per_stage: dict[str, str] = {}

    # Snapshot the stage list — we iterate over keys that exist BEFORE any
    # reassignment; mutating mapping_doc as we go is safe because we never
    # *delete* stages here.
    stage_ids = list(mapping_doc.get("stages", {}).keys())
    total = len(stage_ids)

    for idx, stage_id in enumerate(stage_ids, start=1):
        title = title_by_id.get(stage_id, stage_id)
        desc = desc_by_id.get(stage_id, "")
        logger.info("  [%d/%d] %s", idx, total, stage_id)
        try:
            res = audit_one_stage(
                api=api,
                stage_id=stage_id,
                stage_title=title,
                stage_desc=desc,
                skeleton_doc=skeleton_doc,
                mapping_doc=mapping_doc,
                cache_dir=cache_dir,
                force=force,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Pass B audit crashed for %s: %s", stage_id, e)
            per_stage[stage_id] = "crashed"
            logger.info("      → crashed")
            continue
        per_stage[stage_id] = res["source"]
        # Short-circuit paths (trivial / crosscut_skip / cache / llm_failed)
        # didn't print a per-stage `→` line from audit_one_stage; emit a
        # concise marker here so every iteration of the progress loop has a
        # status line, not just the LLM-success ones.
        if res["source"] != "llm":
            logger.info("      → %s", res["source"])
        applied_all.extend(res["applied"])
        proposed_total += res["proposed"]
        rejected_total += res["rejected"]

    invalidated = sorted({mv["qualname"] for mv in applied_all})
    return {
        "applied": applied_all,
        "proposed": proposed_total,
        "rejected": rejected_total,
        "invalidated": invalidated,
        "per_stage": per_stage,
        "summary": (
            f"PassB: stages_audited={len(per_stage)}, proposed={proposed_total}, "
            f"applied={len(applied_all)}, rejected={rejected_total}, "
            f"invalidated={len(invalidated)}"
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
    ap.add_argument("--cache-dir", type=Path,
                    default=phase2 / "cache/pass_b")
    ap.add_argument("--force", action="store_true",
                    help="re-run LLM even when cache hit")
    args = ap.parse_args(argv)

    from skeleton_yaml import load_yaml
    skeleton_doc = load_yaml(args.skeleton)
    mapping_doc = _yaml.safe_load(args.mapping.read_text(encoding="utf-8"))

    api = Api()
    summary = run_pass_b(
        api, skeleton_doc, mapping_doc, args.cache_dir, force=args.force,
    )

    from iterate_phase2 import _dump_yaml
    args.mapping.write_text(_dump_yaml(mapping_doc), encoding="utf-8")

    logger.info("%s", summary["summary"])
    for mv in summary["applied"]:
        logger.info(
            "  %-50s  %s → %s  reason: %s",
            mv["qualname"], mv["from_stage"], mv["to_stage"], mv["reason"][:80],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
