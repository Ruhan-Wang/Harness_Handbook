# -*- coding: utf-8 -*-
"""Step 1 — LLM per-function analysis.

For each internal non-synthetic function in graph.json:
  1. Render its source with line numbers + metadata
  2. Call the LLM with a structured prompt + the skeleton stage table
  3. Parse the JSON response
  4. Cache the response to disk so re-runs only hit changed functions

Output: a directory of per-function JSON files at ``phase2/cache/llm_outputs/``
        plus a summary index at ``phase2/cache/llm_outputs/_index.json``.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow running as a script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_client import Api, LLMCallResult  # noqa: E402
from parse_skeleton import SkeletonTable, parse_skeleton  # noqa: E402

logger = logging.getLogger(__name__)


PROMPT_SYSTEM_RULES = """You are analyzing one Python function from the Terminus 2 agent harness.

Task: produce a JSON object describing the function's purpose, granularity, and which stage(s) it implements.

GRANULARITY RULES
- "function": the entire function is a single narrative unit. Use for short, cohesive functions (typically ≤30 lines or with a single clear purpose).
- "region": the function contains multiple distinct narrative steps and should be split into 2–10 regions. Use for large functions (typically >30 lines with multiple decision points or sequential phases).

REGION RULES
When granularity is "region":
- Each region MUST be a contiguous range of lines, ending at a complete statement boundary (not mid-statement).
- Provide first_line and last_line as the EXACT text content of the first and last lines of the region (for verification).
- Cover the function's main narrative; small gaps for plumbing (variable init, return) are acceptable.
- Each region gets one stage_id assignment.

STAGE ASSIGNMENT RULES
- Pick ONLY from the provided stage list. Do not invent IDs.
- A function is CROSS-CUTTING only when it IS the small utility called from many places (the helper itself), NOT when it merely calls such a utility or calls `self.logger.*`. Examples:
    * `_count_total_tokens` IS cross-cutting → crosscut-X1
    * `_limit_output_length` IS cross-cutting → crosscut-X1
    * `_record_asciinema_marker` IS cross-cutting → crosscut-X2
    * `_query_llm` calls `self.logger.debug(...)` but it is NOT cross-cutting — it is the primary implementation of stage-4.2. Assign it to stage-4.2.
    * `_save_subagent_trajectory` calls the logger but its real work is persisting a subagent trajectory file — that belongs to its real stage, not crosscut-X3.
- crosscut-X3 (Logging) describes the BaseAgent `self.logger` machinery itself, which is NOT defined in this codebase's internal-function set. It will commonly end up with zero members — that is fine. Do NOT route a function into crosscut-X3 just because it uses the logger.
- For functions that BELONG to the main flow (the work itself), assign to the matching stage / sub-stage / side flow, even if they happen to also log or count tokens incidentally.
- Multiple stages allowed when a function genuinely participates in more than one (rare for cross-cuts since they go to crosscut-X*).
- API-SURFACE functions exposed solely for the harness framework (e.g. `name()`, `version()`) and not part of any data-flow stage: return `function_assignments: []` and set `granularity: "function"`. These will be recorded as unmapped api_surface entries. ONLY use empty assignment for trivial public methods (≤5 lines, no business logic). Any other function with non-trivial work MUST have at least one stage assignment.
- SUBSYSTEM-INTERNAL functions (defined in tmux_session.py / terminus_*_parser.py / asciinema_handler.py and not the subsystem's public entrypoint): assign them to the MAIN-FLOW stage that drives their execution. For example, `TmuxSession.send_keys` and its helpers belong to stage-4.5 (the command-execution path that calls send_keys); `TmuxSession.start` and its install helpers belong to stage-2 (the environment-readiness path).

PURPOSE FIELD — BE SPECIFIC
The `purpose` field must be a concrete, multi-aspect description (60–150 words, roughly 3–5 sentences).
Cover these aspects, in this order, in plain English:
  1. ACTION: What the function actually does, in 1–2 sentences. Avoid generic verbs like "handles" or "manages" — say *how*.
  2. INPUTS / STATE READ: What arguments and which `self._*` attributes determine its behavior. Name them.
  3. OUTPUTS / STATE WRITTEN: What it returns and which `self._*` attributes it mutates (or which trajectory steps it appends, files it writes, etc.).
  4. WHEN INVOKED: From which other function(s) or stage(s), and under what condition.
  5. NON-OBVIOUS: Any edge case, retry logic, fallback path, or design choice a reader would miss from the signature alone. Skip this aspect only if nothing notable.

Region-level `purpose` follows the same structure but is shorter (1–3 sentences, 30–80 words) and focused on what makes this contiguous block a distinct narrative step (e.g., "writes `_pending_completion`", "calls into `_summarize`", "constructs the agent's trajectory step").

OUTPUT FORMAT
Return ONLY a single JSON object wrapped in a ```json fenced block. The schema:

{
  "qualname": "<the function's qualname, exactly as given>",
  "purpose": "<60–150 word description following the 5-aspect structure above>",
  "granularity": "function" | "region",
  "function_assignments": ["stage-X", ...],   // when granularity=="function"; the stages this function implements
  "regions": [                                   // when granularity=="region"; null otherwise
    {
      "line_range": [<int>, <int>],
      "first_line": "<exact text of source line at line_range[0]>",
      "last_line":  "<exact text of source line at line_range[1]>",
      "purpose": "<30–80 word description focused on what this region does and what state change marks its boundary>",
      "stage_id": "<one stage from the available list>"
    },
    ...
  ]
}

When granularity == "function", set "regions" to null.
When granularity == "region", "function_assignments" should list the parent stage(s) the whole function belongs to (often the parent stage of the regions' sub-stages, e.g. ["stage-4"] for the loop).
"""


def render_source_with_line_numbers(file_path: Path, start: int, end: int) -> str:
    """Return the function source as ``LINENO: <code>`` lines, inclusive of both ends."""
    text = file_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    out = []
    for lineno in range(start, min(end, len(lines)) + 1):
        out.append(f"{lineno:5d}: {lines[lineno - 1]}")
    return "\n".join(out)


def function_sha1(file_path: Path, start: int, end: int) -> str:
    text = file_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    snippet = "\n".join(lines[start - 1 : end])
    return hashlib.sha1(snippet.encode("utf-8")).hexdigest()


def build_prompt(
    node: dict,
    source_block: str,
    skeleton_table: SkeletonTable,
) -> str:
    metadata_lines = [
        f"qualname:               {node['qualname']}",
        f"file:                   {node['file']}",
        f"line_range:             [{node['line_start']}, {node['line_end']}]",
        f"line_count:             {node['line_end'] - node['line_start']}",
        f"is_async:               {node.get('is_async', False)}",
        f"is_method:              {node.get('is_method', False)}",
        f"class_name:             {node.get('class_name', '')}",
        f"decorators:             {node.get('decorators', [])}",
        f"n_callers:              {node.get('n_callers', 0)}",
        f"n_callees:              {node.get('n_callees', 0)}",
        f"used_self_attrs_read:   {node.get('used_self_attrs_read', [])}",
        f"used_self_attrs_written:{node.get('used_self_attrs_written', [])}",
        f"signature:              {node.get('signature', '')}",
    ]
    parts = [
        PROMPT_SYSTEM_RULES,
        "",
        "## Available stages (use these IDs exactly)",
        skeleton_table.to_prompt_block(),
        "",
        "## Function metadata",
        "\n".join(metadata_lines),
        "",
        "## Function source (line-numbered)",
        "```python",
        source_block,
        "```",
        "",
        "Return only the JSON block.",
    ]
    return "\n".join(parts)


@dataclass
class FunctionAnalysisRecord:
    qualname: str
    file: str
    line_range: tuple[int, int]
    sha1: str
    llm_output: dict | None
    raw_text: str
    error: str | None
    elapsed_sec: float
    prompt_chars: int


def call_for_function(
    api: Api,
    node: dict,
    source_root: Path,
    skeleton_table: SkeletonTable,
    dry_run: bool = False,
) -> FunctionAnalysisRecord:
    file_path = source_root / node["file"]
    start = node["line_start"]
    end = node["line_end"]
    source_block = render_source_with_line_numbers(file_path, start, end)
    sha1 = function_sha1(file_path, start, end)
    prompt = build_prompt(node, source_block, skeleton_table)

    if dry_run:
        # Synthesize a plausible LLM output for pipeline testing without API calls.
        return FunctionAnalysisRecord(
            qualname=node["qualname"],
            file=node["file"],
            line_range=(start, end),
            sha1=sha1,
            llm_output=_synthetic_llm_output(node, source_root),
            raw_text="<dry-run synthetic output>",
            error=None,
            elapsed_sec=0.0,
            prompt_chars=len(prompt),
        )

    try:
        result: LLMCallResult = api.call(prompt)
    except Exception as e:  # noqa: BLE001
        return FunctionAnalysisRecord(
            qualname=node["qualname"],
            file=node["file"],
            line_range=(start, end),
            sha1=sha1,
            llm_output=None,
            raw_text="",
            error=f"api_call_failed: {e}",
            elapsed_sec=0.0,
            prompt_chars=len(prompt),
        )
    return FunctionAnalysisRecord(
        qualname=node["qualname"],
        file=node["file"],
        line_range=(start, end),
        sha1=sha1,
        llm_output=result.parsed_json,
        raw_text=result.raw_text,
        error=result.error,
        elapsed_sec=result.elapsed_sec,
        prompt_chars=len(prompt),
    )


# Heuristics used by dry-run to generate plausible synthetic outputs so the
# downstream pipeline (Steps 2-4) can be tested without hitting the API.
_CROSSCUT_NAMES = {
    "_count_total_tokens": "crosscut-X1",
    "_limit_output_length": "crosscut-X1",
    "_track_api_request_time": "crosscut-X1",
    "_record_asciinema_marker": "crosscut-X2",
    "_extract_usage_metrics": "crosscut-X1",
    "_collect_subagent_rollout_detail": "crosscut-X1",
    "_update_subagent_metrics": "crosscut-X1",
}


def _synthetic_llm_output(node: dict, source_root: Path) -> dict:
    """Produce a plausible-but-fake LLM output for dry-run testing."""
    name = node.get("name", "")
    line_count = (node.get("line_end") or 0) - (node.get("line_start") or 0)

    # Cross-cuts go straight to crosscut-X stages.
    if name in _CROSSCUT_NAMES:
        return {
            "qualname": node["qualname"],
            "purpose": f"[dry-run] {name} — cross-cutting utility.",
            "granularity": "function",
            "function_assignments": [_CROSSCUT_NAMES[name]],
            "regions": None,
        }

    # Long functions get region granularity (one big region covering the whole body).
    if line_count > 60:
        return {
            "qualname": node["qualname"],
            "purpose": f"[dry-run] {name} — large function, single region placeholder.",
            "granularity": "region",
            "function_assignments": ["stage-4"],
            "regions": [
                {
                    "line_range": [node["line_start"], node["line_end"]],
                    "first_line": "",
                    "last_line": "",
                    "purpose": f"[dry-run] entire body of {name}",
                    "stage_id": "stage-4.2",
                }
            ],
        }

    # Default: function granularity, assigned to a reasonable-looking stage.
    return {
        "qualname": node["qualname"],
        "purpose": f"[dry-run] {name} — function-granularity placeholder.",
        "granularity": "function",
        "function_assignments": ["stage-3"],
        "regions": None,
    }


def select_functions(graph: dict) -> list[dict]:
    """Pick the 96 non-synthetic internal functions we want to analyze."""
    out = []
    for node in graph["nodes"].values():
        if node.get("kind") != "internal":
            continue
        if node.get("synthetic"):
            continue
        if node.get("line_start") is None or node.get("line_end") is None:
            continue
        out.append(node)
    out.sort(key=lambda n: (n["file"], n["line_start"]))
    return out


def load_existing(cache_dir: Path) -> dict[str, dict]:
    """Read previously cached LLM outputs so we can skip unchanged functions."""
    existing: dict[str, dict] = {}
    if not cache_dir.exists():
        return existing
    for path in cache_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        try:
            existing[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
    return existing


def cache_filename(qualname: str) -> str:
    return qualname.replace("/", "_").replace(":", "_") + ".json"


def serialize_record(record: FunctionAnalysisRecord) -> dict:
    return {
        "qualname": record.qualname,
        "file": record.file,
        "line_range": list(record.line_range),
        "sha1": record.sha1,
        "llm_output": record.llm_output,
        "raw_text_preview": record.raw_text[:2000] if record.raw_text else "",
        "error": record.error,
        "elapsed_sec": record.elapsed_sec,
        "prompt_chars": record.prompt_chars,
    }


def run(
    graph_json: Path,
    skeleton_md: Path,
    cache_dir: Path,
    source_root: Path,
    max_workers: int = 6,
    limit: int | None = None,
    force: bool = False,
    only_qualname: str | None = None,
    dry_run: bool = False,
) -> int:
    cache_dir.mkdir(parents=True, exist_ok=True)
    skeleton_table = parse_skeleton(skeleton_md)
    graph = json.loads(graph_json.read_text(encoding="utf-8"))
    candidates = select_functions(graph)
    if only_qualname:
        candidates = [n for n in candidates if n["qualname"] == only_qualname]
    if limit:
        candidates = candidates[:limit]

    existing = load_existing(cache_dir)
    api = Api()

    to_run: list[dict] = []
    skipped = 0
    for node in candidates:
        cache_path = cache_dir / cache_filename(node["qualname"])
        prior = existing.get(cache_path.stem)
        current_sha1 = function_sha1(
            source_root / node["file"], node["line_start"], node["line_end"]
        )
        if (
            not force
            and prior
            and prior.get("sha1") == current_sha1
            and prior.get("llm_output") is not None
            and not prior.get("error")
        ):
            skipped += 1
            continue
        to_run.append(node)

    logger.info(
        "candidates=%d already_cached=%d to_run=%d",
        len(candidates),
        skipped,
        len(to_run),
    )

    if not to_run:
        _write_index(cache_dir, existing)
        return 0

    successes = 0
    failures = 0
    t0 = time.time()

    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                call_for_function, api, node, source_root, skeleton_table, dry_run
            ): node
            for node in to_run
        }
        for fut in cf.as_completed(futures):
            node = futures[fut]
            try:
                record = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "unexpected failure on %s: %s", node["qualname"], e
                )
                failures += 1
                continue
            data = serialize_record(record)
            cache_path = cache_dir / cache_filename(node["qualname"])
            cache_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            if record.llm_output and not record.error:
                successes += 1
                logger.info(
                    "OK   %s (granularity=%s, %.1fs)",
                    record.qualname,
                    record.llm_output.get("granularity"),
                    record.elapsed_sec,
                )
            else:
                failures += 1
                logger.warning(
                    "FAIL %s: %s",
                    record.qualname,
                    record.error,
                )

    elapsed = time.time() - t0
    logger.info(
        "done in %.1fs — success=%d fail=%d skipped=%d",
        elapsed,
        successes,
        failures,
        skipped,
    )

    # Rebuild index after the run.
    final_outputs = load_existing(cache_dir)
    _write_index(cache_dir, final_outputs)
    return 0 if failures == 0 else 1


def _write_index(cache_dir: Path, outputs: dict[str, dict]) -> None:
    index = {
        "count": len(outputs),
        "qualnames": sorted(o["qualname"] for o in outputs.values()),
        "errors": [
            {"qualname": o["qualname"], "error": o.get("error")}
            for o in outputs.values()
            if o.get("error") or o.get("llm_output") is None
        ],
    }
    (cache_dir / "_index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        format="[%(asctime)s][%(levelname)5s] %(message)s",
        level=logging.INFO,
    )

    here = Path(__file__).resolve()
    project = here.parents[3]  # .../Harness_Translation
    default_graph = project / "handbook/phase1/graph.json"
    default_skeleton = project / "handbook/phase2/skeleton.md"
    default_cache = project / "handbook/phase2/cache/llm_outputs"
    default_source_root = project / "harbor/src/harbor/agents/terminus_2"

    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", type=Path, default=default_graph)
    ap.add_argument("--skeleton", type=Path, default=default_skeleton)
    ap.add_argument("--cache-dir", type=Path, default=default_cache)
    ap.add_argument("--source-root", type=Path, default=default_source_root)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N functions (for smoke testing)")
    ap.add_argument("--force", action="store_true",
                    help="Re-run even when cached output matches sha1")
    ap.add_argument("--only", type=str, default=None,
                    help="Only process this qualname (for debugging)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Synthesize fake LLM output (no API call) for pipeline testing")
    args = ap.parse_args(argv)

    return run(
        graph_json=args.graph,
        skeleton_md=args.skeleton,
        cache_dir=args.cache_dir,
        source_root=args.source_root,
        max_workers=args.workers,
        limit=args.limit,
        force=args.force,
        only_qualname=args.only,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
