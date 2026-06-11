# -*- coding: utf-8 -*-
"""iterate_phase2.py — Main driver for Critic-Actor iteration.

Workflow per iteration (Pass C runs FIRST so its structural changes settle
before classification, and the qualnames it invalidates are consumed by the
SAME iteration's Pass A — no cross-iter handoff, no last-iter special case):
    Pass C: skeleton doctor (Actor + 3 Critics) — SKIPPED on iter 0 (mapping
            empty, no distribution to reason about)
    Pass A: re-classify functions in `invalidated` (Actor + 1 Engineer Critic)
    Pass B: global self-consistency check (Actor + 1 Architect Critic) [stub for MVP]
    Pass D: region revision (Actor + 1 Engineer Critic) [stub for MVP]
    Mechanical post-pass: dedup / rederive crosscuts / populate unmapped
    Step 3.5: stage member narrative ordering (Actor + Editor Critic, cached)
  Save iter_N/ snapshot (mapping is already ordered). Check convergence.
  Break on convergence OR i == MAX_ITER (default 10).

Output:
  - phase2/skeleton.yaml         (latest skeleton)
  - phase2/mapping.yaml          (latest mapping)
  - phase2/iterations/iter_N/    (per-iter snapshots)
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import apply  # noqa: E402
import order_stage_members  # noqa: E402
import pass_a_classify  # noqa: E402
import pass_b_reassign  # noqa: E402
import pass_c_skeleton_doctor  # noqa: E402
import pass_d_region_revision  # noqa: E402
import skeleton_yaml  # noqa: E402
from api_client import Api  # noqa: E402

logger = logging.getLogger(__name__)


def _format_duration(secs: float) -> str:
    """Compact wall-clock formatter for iteration timings."""
    if secs < 60:
        return f"{secs:.1f}s"
    if secs < 3600:
        return f"{int(secs // 60)}m{int(secs % 60):02d}s"
    return f"{int(secs // 3600)}h{int((secs % 3600) // 60):02d}m"


class _CleanDumper(yaml.SafeDumper):
    """YAML dumper that disables anchors and uses flow style for short int lists
    (e.g. line_range: [994, 1176])."""

    def ignore_aliases(self, data):  # noqa: D401
        return True


def _represent_list(dumper, data):
    if data and len(data) <= 8 and all(
        isinstance(x, (int, float, bool)) or (isinstance(x, str) and len(x) <= 40)
        for x in data
    ):
        return dumper.represent_sequence(
            "tag:yaml.org,2002:seq", data, flow_style=True
        )
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=False)


def _represent_str(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar(
            "tag:yaml.org,2002:str", data.rstrip("\n") + "\n", style="|"
        )
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_CleanDumper.add_representer(list, _represent_list)
_CleanDumper.add_representer(str, _represent_str)


def _dump_yaml(doc) -> str:
    return yaml.dump(
        doc,
        Dumper=_CleanDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10000,
    )


# ─── State serialization ─────────────────────────────────────────────────────


def save_state(
    out_dir: Path,
    skeleton_doc: dict,
    mapping_doc: dict,
    changes_md: str,
    invalidated: list[str],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    skeleton_yaml.save_yaml(skeleton_doc, out_dir / "skeleton.yaml")
    # Save mapping as YAML (clean: no anchors, flow-style short lists)
    (out_dir / "mapping.yaml").write_text(
        _dump_yaml(mapping_doc), encoding="utf-8",
    )
    (out_dir / "changes.md").write_text(changes_md, encoding="utf-8")
    (out_dir / "invalidated.txt").write_text(
        "\n".join(invalidated), encoding="utf-8"
    )


def initial_mapping_doc(skeleton_doc: dict) -> dict:
    """Start mapping with all skeleton stages present, empty member lists."""
    return {
        "metadata": {
            "phase2_iteration_run": True,
        },
        "stages": {
            s["id"]: {"members": [], "uses_crosscuts": [], "subsystem_refs": []}
            for s in skeleton_doc.get("stages", [])
        },
        "unmapped_functions": [],
    }


def _clean_invalidated(
    invalidated: list[str],
    mapping_doc: dict,
    in_graph_qualnames: set[str],
) -> list[str]:
    """Per-iter hygiene on the Pass A work queue, run before Pass A consumes it.

    Three steps, each guarding a real failure mode, so the invariant the
    convergence check relies on holds: `invalidated is empty ⟺ no work remains`.

      1. Order-preserving dedup. Multiple Pass C ops in the prior iter (e.g.
         a split_stage + follow-up merge_stages) can each independently
         invalidate the same qualname; without dedup Pass A re-classifies it
         twice — wasting an LLM call and racing two workers on the same apply.
      2. Drop phantoms — qualnames Pass A can't classify (not in graph / no
         line info), e.g. leaked from a Pass C op referencing a renamed id.
         Left in, they sit in the queue forever, silently skipped, and the
         loop never converges for the right reason.
      3. Re-inject `missing_llm_output` — a qualname a prior Pass A failed to
         apply (apply_classification's silent-wipe guard, or a transient
         LLM/parse error) gets tagged `missing_llm_output` by
         populate_unmapped and would otherwise be dropped forever, so the loop
         "converges" with permanent coverage holes. Retries are bounded by
         max_iters.
    """
    # 1. order-preserving dedup
    invalidated = list(dict.fromkeys(invalidated))

    # 2. drop phantoms Pass A cannot classify
    phantom = [q for q in invalidated if q not in in_graph_qualnames]
    if phantom:
        logger.info(
            "dropping %d phantom qualname(s) from invalidated (not in graph): %s%s",
            len(phantom), phantom[:5],
            f"... and {len(phantom) - 5} more" if len(phantom) > 5 else "",
        )
        invalidated = [q for q in invalidated if q in in_graph_qualnames]

    # 3. re-inject prior failures so Pass A retries them
    existing_invalid = set(invalidated)
    missing_to_retry = [
        u["qualname"]
        for u in mapping_doc.get("unmapped_functions", [])
        if u.get("reason") == "missing_llm_output"
        and u.get("qualname") in in_graph_qualnames
        and u.get("qualname") not in existing_invalid
    ]
    if missing_to_retry:
        logger.info(
            "re-injecting %d missing_llm_output qualname(s) for Pass A retry: %s%s",
            len(missing_to_retry), missing_to_retry[:5],
            f"... and {len(missing_to_retry) - 5} more"
            if len(missing_to_retry) > 5 else "",
        )
        invalidated = invalidated + missing_to_retry

    return invalidated


def _initial_work_queue(
    graph: dict, limit_functions: int | None
) -> tuple[list[str], set[str]]:
    """Seed the Pass A work queue from the call graph.

    Returns ``(invalidated, in_graph_qualnames)``:
      - ``invalidated`` — every non-synthetic internal function with line info,
        sorted. This is the full iter-0 queue (the mapping starts empty, so
        Pass A classifies all of them).
      - ``in_graph_qualnames`` — the same set, kept for the lifetime of the
        run so each iter can drop phantom entries that leak into the queue
        (e.g. from a Pass C op referencing a missing/renamed id).
    """
    all_qualnames = sorted(
        n["qualname"]
        for n in graph["nodes"].values()
        if n.get("kind") == "internal"
        and not n.get("synthetic")
        and n.get("line_start") is not None
    )
    if limit_functions:
        all_qualnames = all_qualnames[:limit_functions]
    return list(all_qualnames), set(all_qualnames)


def _wipe_previous_snapshots(iterations_dir: Path) -> None:
    """Remove ``iter_N/`` and ``final/`` dirs from any previous run, then
    (re)create ``iterations_dir``.

    Iteration only ever WRITES these dirs, so a partial leftover from an
    aborted run would silently interleave with this run's snapshots and make
    post-hoc diagnosis ambiguous. The match is intentionally tight to the two
    known name patterns so any user-placed files alongside the snapshots are
    left untouched.
    """
    if iterations_dir.exists():
        for child in iterations_dir.iterdir():
            if child.is_dir() and (
                child.name.startswith("iter_") or child.name == "final"
            ):
                shutil.rmtree(child)
    iterations_dir.mkdir(parents=True, exist_ok=True)


# ─── Driver ───────────────────────────────────────────────────────────────────


def run(
    skeleton_yaml_path: Path,
    skeleton_md_path: Path,
    graph_json_path: Path,
    source_root: Path,
    mapping_yaml_path: Path,
    iterations_dir: Path,
    max_iters: int = 10,
    limit_functions: int | None = None,
    enable_pass_b: bool = True,
    enable_pass_c: bool = True,
    enable_pass_d: bool = True,
    enable_ordering: bool = True,
    ordering_cache_dir: Path | None = None,
    pass_b_cache_dir: Path | None = None,
    pass_d_cache_dir: Path | None = None,
) -> int:
    """Run the iteration loop. Returns 0 on convergence, 1 if max_iters hit."""
    if ordering_cache_dir is None:
        ordering_cache_dir = mapping_yaml_path.parent / "cache" / "stage_orders"
    if pass_b_cache_dir is None:
        pass_b_cache_dir = mapping_yaml_path.parent / "cache" / "pass_b"
    if pass_d_cache_dir is None:
        pass_d_cache_dir = mapping_yaml_path.parent / "cache" / "pass_d"
    skeleton_doc = skeleton_yaml.load_yaml(skeleton_yaml_path)
    graph = json.loads(graph_json_path.read_text(encoding="utf-8"))

    # Start from empty mapping; Pass A's first iteration will populate it.
    mapping_doc = initial_mapping_doc(skeleton_doc)

    # Seed the work queue with every classifiable function.
    invalidated, in_graph_qualnames = _initial_work_queue(graph, limit_functions)

    api = Api()
    prev_state_hash = None

    _wipe_previous_snapshots(iterations_dir)

    for iter_i in range(max_iters):
        t0 = time.time()
        logger.info(
            "══════════ Iteration %d/%d  (carry-in: %d) ══════════",
            iter_i + 1, max_iters, len(invalidated),
        )

        changes_log_lines: list[str] = [f"# Iteration {iter_i}", ""]
        change_count = 0
        # Per-pass counters used for the iter-end summary line.
        pass_summary = {"A": "skip", "B": "skip", "C": "skip", "D": "skip"}

        # ── Pass C: skeleton doctor — runs FIRST ───────────────
        # Running the structural pass at the TOP of the iteration means the
        # qualnames it invalidates are re-classified by THIS iteration's Pass A,
        # not deferred to a follow-up iter. That removes the old "skip C on the
        # last iter" special case (C's invalidations no longer outlive their
        # consumer) and lets A/B/D work against the structure C just settled.
        # Skipped on iter 0 ONLY: the mapping is still empty then, so there is
        # no member distribution for the doctor to reason about (its CAUTION
        # rules would propose nothing anyway).
        if enable_pass_c and iter_i >= 1:
            logger.info("── Pass C: skeleton doctor ──")
            try:
                c_result = pass_c_skeleton_doctor.run_pass_c(
                    api=api, skeleton_doc=skeleton_doc, mapping_doc=mapping_doc
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("Pass C crashed: %s", e)
                c_result = {
                    "changes_applied": [], "changes_proposed": 0,
                    "changes_rejected": 0, "invalidated": [],
                    "summary": f"CRASHED: {e}",
                }
            change_count += len(c_result["changes_applied"])
            pass_summary["C"] = (
                f"{len(c_result['changes_applied'])}/"
                f"{c_result['changes_proposed']} skeleton change(s) applied"
            )
            # C's invalidations join the queue Pass A consumes below this iter.
            invalidated.extend(c_result["invalidated"])
            changes_log_lines.append("## Pass C — " + c_result["summary"])
            for change in c_result["changes_applied"]:
                changes_log_lines.append(
                    f"  - applied: {change.get('action')} → {json.dumps(change, ensure_ascii=False)[:200]}"
                )
            changes_log_lines.append("")
        elif enable_pass_c and iter_i == 0:
            logger.info(
                "── Pass C: SKIPPED (iter 0; mapping empty, nothing to analyze) ──"
            )
            pass_summary["C"] = "skip_iter0"

        # Queue hygiene right before Pass A consumes `invalidated` — now also
        # sanitizes the qualnames Pass C just added: order-preserving dedup,
        # drop phantoms, re-inject prior failures. Keeps the invariant
        # `invalidated empty ⟺ no work remains` that convergence relies on.
        invalidated = _clean_invalidated(
            invalidated, mapping_doc, in_graph_qualnames
        )

        # ── Pass A ─────────────────────────────────────────────
        summaries: list[dict] = []
        if invalidated:
            logger.info(
                "── Pass A: classify %d function(s) ──", len(invalidated),
            )
            try:
                summaries = pass_a_classify.run_pass_a(
                    api=api,
                    graph=graph,
                    skeleton_doc=skeleton_doc,
                    mapping_doc=mapping_doc,
                    source_root=source_root,
                    invalidated=invalidated,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("Pass A crashed: %s", e)
                changes_log_lines.append(f"## Pass A — CRASHED: {e}")
                changes_log_lines.append("")
                summaries = []
            accepted = sum(1 for s in summaries if s["accepted"])
            change_count += accepted
            pass_summary["A"] = f"{accepted}/{len(summaries)} accepted"
            changes_log_lines.append(
                f"## Pass A — accepted {accepted}/{len(summaries)}"
            )
            for s in summaries:
                if not s["accepted"]:
                    changes_log_lines.append(f"  - {s['summary']}")
            changes_log_lines.append("")

        # Decide what to carry over to the next iter. Three classes of outcome:
        #   (a) accepted (Critic approved) → done, do not retry.
        #   (b) DISCARDED (Actor produced a proposal, Critic rejected after a
        #       revise round) → do NOT retry; same prompt yields the same
        #       verdict.
        #   (c) actor_failed (LLM call broke or returned unparseable JSON —
        #       typically transient HTTP 429 / 5xx) → DO retry. Without this,
        #       every transient failure abandons that qualname (sits in
        #       summaries with accepted=False, gets tagged missing_llm_output
        #       by populate_unmapped, and contributes to a false "converged"
        #       state). The next iter's re-injection of missing_llm_output
        #       would also catch this, but carrying over here makes the retry
        #       happen immediately rather than wait for the post-pass step.
        #   (d) Not in summaries at all (full Pass A crash / partial batch
        #       return) → retry (existing logic).
        actor_failed_qns = {
            s["qualname"] for s in summaries
            if "actor_failed" in s.get("summary", "")
        }
        processed_qns = {s["qualname"] for s in summaries} - actor_failed_qns
        carry_over = [q for q in invalidated if q not in processed_qns]
        if carry_over:
            logger.warning(
                "Pass A did not process %d invalidated qualname(s); carrying "
                "over to next iter: %s%s",
                len(carry_over), carry_over[:3],
                f"... and {len(carry_over) - 3} more" if len(carry_over) > 3 else "",
            )
        invalidated = carry_over

        # Dedup Pass A's freshly-added entries (a function-level entry coexisting
        # with region entries for the same qualname in the same stage) before
        # Pass B audits placement and Pass D refines regions.
        n_dropped_early = apply.dedup_members(mapping_doc)
        if n_dropped_early:
            logger.info("Post-PassA dedup: dropped %d function-level entries", n_dropped_early)
            changes_log_lines.append(
                f"## Post-PassA dedup — dropped {n_dropped_early} entries"
            )
            changes_log_lines.append("")

        # ── Pass B ─────────────────────────────────────────────
        if enable_pass_b:
            logger.info("── Pass B: global reassignment audit ──")
            try:
                b_result = pass_b_reassign.run_pass_b(
                    api=api,
                    skeleton_doc=skeleton_doc,
                    mapping_doc=mapping_doc,
                    cache_dir=pass_b_cache_dir,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("Pass B crashed: %s", e)
                b_result = {
                    "applied": [], "proposed": 0, "rejected": 0,
                    "invalidated": [], "per_stage": {},
                    "summary": f"CRASHED: {e}",
                }
            change_count += len(b_result["applied"])
            pass_summary["B"] = (
                f"{len(b_result['applied'])} move(s) applied, "
                f"{b_result['rejected']} rejected"
            )
            # Pass B's moves invalidate the moved qualnames so Pass A re-runs
            # them next iter with the new caller/callee context.
            invalidated.extend(b_result["invalidated"])
            changes_log_lines.append("## Pass B — " + b_result["summary"])
            for mv in b_result["applied"]:
                changes_log_lines.append(
                    f"  - moved: {mv['qualname']}  "
                    f"{mv['from_stage']} → {mv['to_stage']}  "
                    f"({mv.get('reason','')[:80]})"
                )
            changes_log_lines.append("")

        # ── Pass D ─────────────────────────────────────────────
        # Runs LAST among LLM passes so all stage-level moves (A/B/C) have
        # settled before we refine region boundaries. Pass D does NOT add to
        # `invalidated` — its revisions are authoritative for this iter.
        if enable_pass_d:
            logger.info("── Pass D: region boundary revision ──")
            try:
                d_result = pass_d_region_revision.run_pass_d(
                    api=api,
                    skeleton_doc=skeleton_doc,
                    mapping_doc=mapping_doc,
                    graph=graph,
                    source_root=source_root,
                    cache_dir=pass_d_cache_dir,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("Pass D crashed: %s", e)
                d_result = {
                    "applied": [], "proposed": 0, "rejected": 0,
                    "per_qn": {}, "summary": f"CRASHED: {e}",
                }
            n_action_count = sum(len(a["actions"]) for a in d_result["applied"])
            change_count += n_action_count
            pass_summary["D"] = (
                f"{n_action_count} action(s) on {len(d_result['applied'])} func(s)"
            )
            changes_log_lines.append("## Pass D — " + d_result["summary"])
            for entry in d_result["applied"]:
                kinds = [a.get("action") for a in entry["actions"]]
                changes_log_lines.append(
                    f"  - {entry['qualname']}: {kinds}"
                )
            changes_log_lines.append("")

        # The mechanical post-pass steps mutate mapping_doc but contribute
        # no LLM cost and no judgement. If any one crashes (e.g.,
        # `_sha1_of_range` fails because a source file vanished mid-run),
        # we want the iter's snapshot to still be saved and the loop to
        # continue — the next iter's state_hash will simply re-detect the
        # need to re-derive. Crashing through here would lose the LLM
        # progress already applied above.
        try:
            n_dropped = apply.dedup_members(mapping_doc)
            if n_dropped:
                logger.info("Dedup: dropped %d function-level entries that overlapped regions", n_dropped)
                changes_log_lines.append(f"## Dedup — dropped {n_dropped} function entries")
                changes_log_lines.append("")
            apply.rederive_uses_crosscuts_and_subsystem_refs(mapping_doc, graph)
            apply.populate_unmapped(mapping_doc, graph)
        except Exception as e:  # noqa: BLE001
            logger.exception("post-pass mechanical step crashed: %s", e)
            changes_log_lines.append(f"## post-pass CRASHED: {e}")
            changes_log_lines.append("")

        # ── Step 3.5: per-iter stage member ordering ─────────────
        # Run BEFORE the snapshot save so iter_N/mapping.yaml is the ordered
        # version. order_all_stages is fingerprint-cached over each stage's
        # member identity, so iters where membership didn't change cost nothing
        # (cache hit). Convergence detection uses state_hash, which sorts
        # member keys per stage, so reordering does not affect termination.
        if enable_ordering:
            logger.info("── Step 3.5: per-iter stage member ordering ──")
            try:
                order_stage_members.order_all_stages(
                    api=api,
                    mapping_doc=mapping_doc,
                    skeleton_doc=skeleton_doc,
                    graph=graph,
                    cache_dir=ordering_cache_dir,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("Per-iter ordering crashed: %s", e)
                changes_log_lines.append(f"## Step 3.5 — CRASHED: {e}")
                changes_log_lines.append("")

        # Snapshot.
        snapshot_dir = iterations_dir / f"iter_{iter_i}"
        save_state(snapshot_dir, skeleton_doc, mapping_doc,
                   "\n".join(changes_log_lines), invalidated)

        # Do NOT overwrite the source skeleton.yaml or skeleton.md during
        # iteration — those are user-authored canonical artifacts. The latest
        # working state is saved in iterations/iter_N/ instead.
        # Only the final converged result (under iterations/final/) is meant
        # to overwrite mapping.yaml; even there, skeleton changes stay in the
        # iteration snapshot for the user to review and merge manually.
        mapping_yaml_path.write_text(_dump_yaml(mapping_doc), encoding="utf-8")

        elapsed = time.time() - t0
        logger.info(
            "── Iteration %d/%d done in %s · changes=%d · carry=%d ──",
            iter_i + 1, max_iters, _format_duration(elapsed),
            change_count, len(invalidated),
        )
        logger.info(
            "    A: %s · B: %s · C: %s · D: %s",
            pass_summary["A"], pass_summary["B"],
            pass_summary["C"], pass_summary["D"],
        )

        # ── Convergence check ─────────────────────────────────
        # Unified state_hash-based check (replaces former hard + soft pair):
        #
        #   - state_hash captures the FULL mapping + skeleton state, including
        #     mutations from mechanical steps (dedup / rederive /
        #     populate_unmapped), not just LLM passes' change_count. So if any
        #     mechanical step turned non-idempotent, this still detects it.
        #
        #   - invalidated must be empty: even with stable state, pending Pass A
        #     work means we shouldn't terminate yet.
        #
        # The previous design had two checks (a "hard" stale state check and a
        # "soft" no-changes check). On analysis the soft check always fires
        # first in any reachable trajectory, so the hard branch was dead code.
        current_hash = apply.state_hash(skeleton_doc, mapping_doc)
        if prev_state_hash == current_hash and not invalidated:
            logger.info(
                "CONVERGED after %d iteration(s) (state stable, no pending work).",
                iter_i + 1,
            )
            _finalize(
                api, iterations_dir, skeleton_doc, mapping_doc, graph,
                f"Converged at iteration {iter_i}", enable_ordering,
                ordering_cache_dir, mapping_yaml_path,
            )
            return 0

        prev_state_hash = current_hash

    logger.warning("MAX iterations reached without explicit convergence.")
    _finalize(
        api, iterations_dir, skeleton_doc, mapping_doc, graph,
        f"Forced stop at MAX={max_iters} iterations", enable_ordering,
        ordering_cache_dir, mapping_yaml_path, invalidated=invalidated,
    )
    return 1


def _finalize(
    api: Api,
    iterations_dir: Path,
    skeleton_doc: dict,
    mapping_doc: dict,
    graph: dict,
    note: str,
    enable_ordering: bool,
    ordering_cache_dir: Path,
    mapping_yaml_path: Path,
    invalidated: list[str] | None = None,
) -> None:
    """Step 3.5 — order stage members, then save final/ + update mapping.yaml root."""
    if enable_ordering:
        logger.info("── Step 3.5: Stage member ordering ──")
        try:
            order_stage_members.order_all_stages(
                api=api,
                mapping_doc=mapping_doc,
                skeleton_doc=skeleton_doc,
                graph=graph,
                cache_dir=ordering_cache_dir,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Ordering crashed: %s", e)
    else:
        logger.info("── Step 3.5 SKIPPED ──")

    final_dir = iterations_dir / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    save_state(final_dir, skeleton_doc, mapping_doc, note, invalidated or [])
    # Also update the top-level mapping.yaml so downstream tooling sees the
    # ordered version, not the random-insertion-order one from the last iter.
    mapping_yaml_path.write_text(_dump_yaml(mapping_doc), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        format="[%(asctime)s][%(levelname)5s] %(message)s",
        level=logging.INFO,
    )
    here = Path(__file__).resolve()
    project = here.parents[3]
    phase2 = project / "handbook/phase2"

    ap = argparse.ArgumentParser()
    ap.add_argument("--skeleton-yaml", type=Path, default=phase2 / "skeleton.yaml")
    ap.add_argument("--skeleton-md", type=Path, default=phase2 / "skeleton.md")
    ap.add_argument("--graph", type=Path, default=project / "handbook/phase1/graph.json")
    ap.add_argument("--source-root", type=Path,
                    default=project / "harbor/src/harbor/agents/terminus_2")
    ap.add_argument("--mapping", type=Path, default=phase2 / "mapping.yaml")
    ap.add_argument("--iterations-dir", type=Path,
                    default=phase2 / "iterations")
    ap.add_argument("--max-iters", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N functions (smoke test)")
    ap.add_argument("--no-pass-b", action="store_true",
                    help="Skip Pass B (reassignment audit) for debugging")
    ap.add_argument("--no-pass-c", action="store_true",
                    help="Skip Pass C (skeleton doctor) for debugging")
    ap.add_argument("--no-pass-d", action="store_true",
                    help="Skip Pass D (region boundary revision) for debugging")
    ap.add_argument("--no-ordering", action="store_true",
                    help="Skip Step 3.5 stage member ordering")
    args = ap.parse_args(argv)

    return run(
        skeleton_yaml_path=args.skeleton_yaml,
        skeleton_md_path=args.skeleton_md,
        graph_json_path=args.graph,
        source_root=args.source_root,
        mapping_yaml_path=args.mapping,
        iterations_dir=args.iterations_dir,
        max_iters=args.max_iters,
        limit_functions=args.limit,
        enable_pass_b=not args.no_pass_b,
        enable_pass_c=not args.no_pass_c,
        enable_pass_d=not args.no_pass_d,
        enable_ordering=not args.no_ordering,
    )


if __name__ == "__main__":
    raise SystemExit(main())
