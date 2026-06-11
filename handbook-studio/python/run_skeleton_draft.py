#!/usr/bin/env python3
"""run_skeleton_draft.py — draft a skeleton.yaml for an arbitrary repo.

Phase 2 of the handbook pipeline needs a hand-authored skeleton (the lifecycle
"stage" breakdown). Handbook Studio bootstraps a first draft by asking the
active LLM provider (via the local LLM gateway, reached through the same
api_client.Api the rest of the pipeline uses) to propose stages, state
registers, and subsystems from the Phase 1 call graph. The user then reviews
and edits it in-app before Phase 2 runs.

Env (set by the Node pipeline-runner):
  HANDBOOK_PHASE1_OUT  — dir containing graph.json
  HANDBOOK_PHASE2_DIR  — dir to write skeleton.yaml into
  HANDBOOK_LLM_HOST/PORT — point api_client at the gateway
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Reuse the pipeline's API client + skeleton writer.
_HERE = Path(__file__).resolve().parent
_GENERATE = Path(
    os.environ.get("HANDBOOK_GENERATE_DIR", str(_HERE.parent.parent / "Harness_Handbook" / "handbook_generate"))
)
sys.path.insert(0, str(_GENERATE / "phase2"))

from api_client import Api  # noqa: E402
import skeleton_yaml  # noqa: E402


def _phase1_out() -> Path:
    return Path(os.environ.get("HANDBOOK_PHASE1_OUT", str(_GENERATE / "phase1")))


def _phase2_dir() -> Path:
    return Path(os.environ.get("HANDBOOK_PHASE2_DIR", str(_GENERATE / "phase2")))


def build_inventory(graph: dict) -> str:
    """Compact, token-bounded inventory of the code for the prompt."""
    nodes = graph.get("nodes", {})
    internal = [n for n in nodes.values() if n.get("kind") == "internal" and not n.get("synthetic")]
    by_file: dict[str, list[dict]] = {}
    for n in internal:
        by_file.setdefault(n.get("file", "?"), []).append(n)

    lines: list[str] = []
    for fname in sorted(by_file):
        fns = sorted(by_file[fname], key=lambda x: x.get("line_start", 0))
        lines.append(f"\n## {fname}")
        for fn in fns:
            sig = fn.get("signature", fn.get("qualname", "")).strip()
            callers = fn.get("n_callers", 0)
            callees = fn.get("n_callees", 0)
            lines.append(f"  - {fn.get('qualname')}  (callers={callers}, callees={callees})  {sig[:120]}")

    # Candidate state registers: self-attrs read/written across many functions.
    self_attrs = graph.get("self_attrs", {})
    reg_lines: list[str] = []
    for cls, attrs in self_attrs.items():
        for attr, usage in attrs.items():
            r = len(usage.get("read_in", []))
            w = len(usage.get("written_in", []))
            if r + w >= 2:
                reg_lines.append(f"  - {cls}.{attr}  (reads={r}, writes={w})")
    if reg_lines:
        lines.append("\n## Candidate state registers (self-attrs used across functions)")
        lines.extend(sorted(reg_lines))

    return "\n".join(lines)


PROMPT = """You are mapping a code repository into a structural "skeleton" for a developer handbook.

Below is an inventory of the repository's functions (grouped by file) and candidate state variables.

Produce a SKELETON: an ordered list of lifecycle STAGES that describe, end to end, what this
codebase does when it runs. Think of the stages as chapters of the program's story (setup,
the main loop, each major step inside the loop, teardown, plus side flows and cross-cutting
utilities). Also list the important STATE REGISTERS (long-lived variables that carry state
across stages) and external SUBSYSTEMS the code talks to.

Rules:
- 6-15 stages. Use ids like "stage-1", "stage-2", "stage-2.1" (sub-stage), "side-S1" (side flow),
  "crosscut-X1" (cross-cutting utility). Order them in execution order.
- Each stage: a short title and a 1-3 sentence description of its responsibility.
- state_registers: id (the variable name) + one-line semantics.
- subsystems: id + one-line role (e.g. "the LLM", "the shell/tmux session", "the filesystem").

Return ONE fenced json block, exactly this shape:
```json
{
  "stages": [
    {"id": "stage-1", "title": "...", "description": "...", "parent": null}
  ],
  "state_registers": [{"id": "...", "semantics": "..."}],
  "subsystems": [{"id": "...", "role": "..."}]
}
```

REPOSITORY INVENTORY:
"""


def heuristic_skeleton(graph: dict) -> dict:
    """Fallback when the LLM is unavailable: one stage per source file."""
    nodes = graph.get("nodes", {})
    files = sorted({n.get("file", "?") for n in nodes.values() if n.get("kind") == "internal"})
    stages = []
    for i, f in enumerate(files, start=1):
        stages.append({
            "id": f"stage-{i}",
            "title": f.replace(".py", "").replace("/", " / "),
            "description": f"Code defined in {f}. (Auto-generated placeholder; please refine.)",
            "parent": None,
            "children": [],
        })
    return {"metadata": {"version": 1, "drafted_by": "heuristic"}, "stages": stages,
            "state_registers": [], "subsystems": []}


def normalize(doc: dict) -> dict:
    stages = []
    for s in doc.get("stages", []):
        stages.append({
            "id": s.get("id"),
            "title": s.get("title", s.get("id", "")),
            "description": (s.get("description") or "(no description)").strip(),
            "parent": s.get("parent"),
            "children": s.get("children", []),
        })
    # Derive children from parent links if not provided.
    by_id = {s["id"]: s for s in stages}
    for s in stages:
        p = s.get("parent")
        if p and p in by_id and s["id"] not in by_id[p]["children"]:
            by_id[p]["children"].append(s["id"])
    return {
        "metadata": {"version": 1, "drafted_by": "llm"},
        "stages": stages,
        "state_registers": doc.get("state_registers", []),
        "subsystems": doc.get("subsystems", []),
    }


def main() -> int:
    graph_path = _phase1_out() / "graph.json"
    if not graph_path.exists():
        print(f"[skeleton] graph.json not found at {graph_path}; run Phase 1 first", file=sys.stderr)
        return 2
    graph = json.loads(graph_path.read_text(encoding="utf-8"))

    inventory = build_inventory(graph)
    print(f"[skeleton] built inventory ({len(inventory)} chars); asking the LLM to draft stages...")

    doc = None
    try:
        api = Api()
        result = api.call(PROMPT + inventory)
        if result.parsed_json:
            doc = normalize(result.parsed_json)
            print(f"[skeleton] LLM drafted {len(doc['stages'])} stages, "
                  f"{len(doc['state_registers'])} registers, {len(doc['subsystems'])} subsystems")
        else:
            print(f"[skeleton] LLM returned no JSON block: {result.error}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[skeleton] LLM draft failed ({e}); falling back to heuristic", file=sys.stderr)

    if doc is None:
        doc = heuristic_skeleton(graph)
        print(f"[skeleton] heuristic skeleton with {len(doc['stages'])} stages")

    out_dir = _phase2_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    skeleton_yaml.save_yaml(doc, out_dir / "skeleton.yaml")
    # Also render the human-readable markdown alongside it.
    try:
        skeleton_yaml.render_md_from_yaml(doc, out_dir / "skeleton.md")
    except Exception:  # noqa: BLE001
        pass
    print(f"[skeleton] wrote {out_dir / 'skeleton.yaml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
