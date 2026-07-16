# -*- coding: utf-8 -*-
"""Step 2 — AST Snap.

For every region produced by Step 1, snap the LLM-given line_range to the
nearest legal Python statement boundary using the ``ast`` module. The LLM is
trusted for semantic intent ("the logical break is around line X"); AST snap
enforces syntactic correctness ("the statement actually ends at line X+2").

Inputs:  ``phase2/cache/llm_outputs/*.json`` (Step 1 outputs)
Outputs: same files, augmented with:
           - ``llm_output.regions[i].line_range`` snapped to AST boundary
           - ``llm_output.regions[i].original_llm_range`` (preserved for audit)
           - ``llm_output.regions[i].snap_status``: "ok" | "snapped" | "needs_review"
           - ``llm_output.regions[i].snap_distance``: max abs change in line numbers
         plus a summary written to ``phase2/cache/ast_snap_report.json``.

Snap rules:
  - start → smallest ``s.lineno >= original_start`` over function-body statements
  - end   → largest ``s.end_lineno <= original_end`` over function-body statements
  - if either move > ``snap_threshold`` lines: mark ``needs_review``
  - if no valid statement found: mark ``needs_review``, keep original range
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "adapters"))

logger = logging.getLogger(__name__)

DEFAULT_SNAP_THRESHOLD = 3  # lines


@dataclass
class SnapResult:
    start: int
    end: int
    status: str  # "ok" | "snapped" | "needs_review"
    distance: int  # max(|new_start - old_start|, |new_end - old_end|)
    note: str = ""


def _collect_all_statements(func_body: list[ast.stmt]) -> list[tuple[int, int]]:
    """Walk every ``stmt`` node in ``func_body`` (at any nesting depth) and return
    ``(lineno, end_lineno)`` pairs. Snap boundaries can land at any statement
    boundary in the function — not just the function-body top level — so that
    regions inside a ``for``/``while``/``if`` block can be snapped properly.
    """
    pairs: list[tuple[int, int]] = []
    for top in func_body:
        for node in ast.walk(top):
            if isinstance(node, ast.stmt):
                end = node.end_lineno or node.lineno
                pairs.append((node.lineno, end))
    # Dedupe and sort by start for predictable behavior.
    return sorted(set(pairs))


def find_function_statements(
    file_path: Path, qualname: str
) -> list[tuple[int, int]] | None:
    """Return ``[(lineno, end_lineno), ...]`` for every statement in the named
    function's body — the legal snap boundaries. None if it can't be located.

    Multilang: dispatches to the language adapter for the file's extension.
    Python uses the exact `ast` path (identical to the legacy behavior); Rust /
    TS / Go use their tree-sitter span collectors. ``qualname`` is whatever the
    Phase-1 graph used for that language (dotted for Python/TS/Go, ``::`` for
    Rust); each adapter matches on the leaf name.
    """
    try:
        from base import adapter_for_file
        adapter = adapter_for_file(Path(file_path))
    except Exception:
        return None
    try:
        return adapter.statement_spans(Path(file_path), qualname)
    except Exception:
        return None


def snap_range(
    requested_start: int,
    requested_end: int,
    statements: list[tuple[int, int]],
    snap_threshold: int = DEFAULT_SNAP_THRESHOLD,
) -> SnapResult:
    """Snap (start, end) to the nearest enclosing top-level statement boundaries."""
    if not statements:
        return SnapResult(
            requested_start,
            requested_end,
            status="needs_review",
            distance=0,
            note="no statements in function body",
        )

    # Candidate snapped_start: smallest s.lineno >= requested_start
    candidates_start = [s[0] for s in statements if s[0] >= requested_start]
    if candidates_start:
        snapped_start = min(candidates_start)
    else:
        # requested_start is past all statement starts; fall back to last statement's start
        snapped_start = statements[-1][0]

    # Candidate snapped_end: largest s.end_lineno <= requested_end
    candidates_end = [s[1] for s in statements if s[1] <= requested_end]
    if candidates_end:
        snapped_end = max(candidates_end)
    else:
        # requested_end is before all statement ends; fall back to first statement's end
        snapped_end = statements[0][1]

    if snapped_end < snapped_start:
        # Degenerate: snap landed inverted. Mark needs_review, keep original.
        return SnapResult(
            requested_start,
            requested_end,
            status="needs_review",
            distance=0,
            note="snap produced inverted range",
        )

    dist = max(
        abs(snapped_start - requested_start), abs(snapped_end - requested_end)
    )
    status = "ok"
    if dist > 0:
        status = "snapped"
    if dist > snap_threshold:
        status = "needs_review"
    return SnapResult(snapped_start, snapped_end, status=status, distance=dist)


def verify_first_last_lines(
    file_path: Path,
    line_range: tuple[int, int],
    first_line: str | None,
    last_line: str | None,
) -> tuple[bool, str]:
    """Sanity-check that LLM's first_line / last_line match the source content."""
    if not first_line and not last_line:
        return True, ""
    text = file_path.read_text(encoding="utf-8").splitlines()
    notes: list[str] = []
    start, end = line_range

    def matches(expected: str, lineno: int) -> bool:
        if not expected:
            return True
        if lineno < 1 or lineno > len(text):
            return False
        actual = text[lineno - 1].strip()
        exp = expected.strip()
        # Tolerate small typing differences: match by 30-char prefix.
        return exp[:30] in actual or actual[:30] in exp

    if first_line and not matches(first_line, start):
        notes.append(
            f"first_line mismatch at line {start}: expected '{first_line[:50]}', "
            f"got '{text[start - 1].strip()[:50] if 1 <= start <= len(text) else '?'}'"
        )
    if last_line and not matches(last_line, end):
        notes.append(
            f"last_line mismatch at line {end}: expected '{last_line[:50]}', "
            f"got '{text[end - 1].strip()[:50] if 1 <= end <= len(text) else '?'}'"
        )
    return (len(notes) == 0), "; ".join(notes)


def snap_record(
    record: dict,
    source_root: Path,
    snap_threshold: int = DEFAULT_SNAP_THRESHOLD,
) -> tuple[dict, list[dict]]:
    """Mutate ``record['llm_output']['regions']`` in place. Returns (record, report_rows)."""
    report_rows: list[dict] = []
    llm = record.get("llm_output")
    if not llm:
        return record, report_rows
    if llm.get("granularity") != "region":
        return record, report_rows
    regions = llm.get("regions") or []

    file_path = source_root / record["file"]
    statements = find_function_statements(file_path, record["qualname"])
    if statements is None:
        for region in regions:
            region["snap_status"] = "needs_review"
            region["snap_note"] = "could not locate function in AST"
            report_rows.append(
                {
                    "qualname": record["qualname"],
                    "region_stage_id": region.get("stage_id"),
                    "status": "needs_review",
                    "note": "AST lookup failed",
                }
            )
        return record, report_rows

    for region in regions:
        # Be idempotent: if we've snapped this region before, snap from the
        # preserved original_llm_range, not from the already-snapped line_range.
        # Otherwise re-runs would silently reset snap_status to "ok".
        prev_original = region.get("original_llm_range")
        try:
            if prev_original:
                orig_start, orig_end = prev_original
            else:
                orig_start, orig_end = region["line_range"]
        except (KeyError, ValueError, TypeError):
            region["snap_status"] = "needs_review"
            region["snap_note"] = "malformed line_range from LLM"
            report_rows.append(
                {
                    "qualname": record["qualname"],
                    "region_stage_id": region.get("stage_id"),
                    "status": "needs_review",
                    "note": "malformed line_range",
                }
            )
            continue

        snap = snap_range(orig_start, orig_end, statements, snap_threshold)
        region["original_llm_range"] = [orig_start, orig_end]
        region["line_range"] = [snap.start, snap.end]
        region["snap_status"] = snap.status
        region["snap_distance"] = snap.distance
        if snap.note:
            region["snap_note"] = snap.note
        elif "snap_note" in region:
            # Clear a stale note from a prior run.
            region.pop("snap_note", None)

        # Cross-check: do first_line / last_line match after snap?
        ok, note = verify_first_last_lines(
            file_path,
            (snap.start, snap.end),
            region.get("first_line"),
            region.get("last_line"),
        )
        if not ok:
            existing_note = region.get("snap_note", "")
            region["snap_note"] = (existing_note + "; " + note).strip("; ")
            if region["snap_status"] == "ok":
                region["snap_status"] = "snapped"  # at least one mismatch — flag mildly
            # If both lines mismatched, escalate
            if "first_line mismatch" in note and "last_line mismatch" in note:
                region["snap_status"] = "needs_review"

        report_rows.append(
            {
                "qualname": record["qualname"],
                "region_stage_id": region.get("stage_id"),
                "original_range": [orig_start, orig_end],
                "snapped_range": [snap.start, snap.end],
                "distance": snap.distance,
                "status": region["snap_status"],
                "note": region.get("snap_note", ""),
            }
        )

    return record, report_rows


def run(
    cache_dir: Path,
    source_root: Path,
    report_path: Path,
    snap_threshold: int = DEFAULT_SNAP_THRESHOLD,
) -> int:
    if not cache_dir.exists():
        logger.error("cache dir does not exist: %s", cache_dir)
        return 1

    total = 0
    snapped = 0
    needs_review = 0
    all_rows: list[dict] = []

    for path in sorted(cache_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("skip unreadable %s", path.name)
            continue
        record, rows = snap_record(record, source_root, snap_threshold)
        path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        all_rows.extend(rows)
        for row in rows:
            total += 1
            if row["status"] == "snapped":
                snapped += 1
            elif row["status"] == "needs_review":
                needs_review += 1

    report = {
        "total_regions": total,
        "snapped": snapped,
        "needs_review": needs_review,
        "rows": all_rows,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(
        "ast_snap: total=%d snapped=%d needs_review=%d → %s",
        total,
        snapped,
        needs_review,
        report_path,
    )
    return 0 if needs_review == 0 else 0  # not fatal here; Validator decides


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        format="[%(asctime)s][%(levelname)5s] %(message)s",
        level=logging.INFO,
    )

    here = Path(__file__).resolve()
    project = here.parents[3]
    default_cache = project / "handbook/phase2/cache/llm_outputs"
    default_source = Path(os.environ.get("HANDBOOK_SOURCE_ROOT", "."))
    default_report = project / "handbook/phase2/cache/ast_snap_report.json"

    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", type=Path, default=default_cache)
    ap.add_argument("--source-root", type=Path, default=default_source)
    ap.add_argument("--report", type=Path, default=default_report)
    ap.add_argument("--snap-threshold", type=int, default=DEFAULT_SNAP_THRESHOLD)
    args = ap.parse_args(argv)
    return run(args.cache_dir, args.source_root, args.report, args.snap_threshold)


if __name__ == "__main__":
    raise SystemExit(main())
