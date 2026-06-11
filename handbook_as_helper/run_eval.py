#!/usr/bin/env python3
"""run_eval.py — run the two-phase code agent over golden queries and save its outputs.

For each selected query the code agent produces:
  - plan.md     : the planner's natural-language plan of the edits (file:location -> change)
  - agent.diff  : the executor's actual code change, as a git diff (objective)

No grading — inspect the two outputs directly.

Usage:
    python run_eval.py --arm baseline
    python run_eval.py --arm baseline --cases Q1,Q12
    python run_eval.py --arm handbook --cases Q1
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent          # Harness_Translation/
HERE = Path(__file__).resolve().parent                 # roundtrip_eval/
PRISTINE = ROOT / "harbor/src/harbor/agents/terminus_2"
GOLDEN = ROOT / "terminus2_roundtrip_golden.yaml"

# Where the agent's writable sandboxes + outputs go. Defaults to ./runs (local), but
# under Apptainer set EVAL_WORK_ROOT=/work (a writable bind) while the harness itself is
# mounted READ-ONLY — so the agent physically cannot modify harness code.
WORK_ROOT = Path(os.environ.get("EVAL_WORK_ROOT", HERE / "runs"))

sys.path.insert(0, str(HERE))


def load_cases(subset: list[str] | None) -> list[dict]:
    golden = yaml.safe_load(GOLDEN.read_text())
    cases = golden["test_cases"]
    if subset:
        want = set(subset)
        cases = [c for c in cases if c["id"] in want]
    return cases


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["baseline", "handbook", "opus"], required=True,
                    help="baseline = Qwen, no handbook | handbook = Qwen + handbook | "
                         "opus = the SAME baseline pipeline (no handbook) with Claude Opus, the "
                         "'ceiling' arm — run with LLM_API_TYPE=anthropic_chat_completion "
                         "LLM_BASE_URL=<opus_proxy> LLM_MODEL=claude-opus-4-8")
    ap.add_argument("--cases", help="comma-separated case ids, e.g. Q1,Q12 (default: all)")
    args = ap.parse_args()

    subset = args.cases.split(",") if args.cases else None
    cases = load_cases(subset)

    from code_agent import run_query  # imported here so import errors surface clearly

    use_handbook = args.arm == "handbook"
    run_dir = WORK_ROOT / args.arm
    run_dir.mkdir(parents=True, exist_ok=True)

    for c in cases:
        cid = c["id"]
        case_dir = run_dir / cid
        sandbox = case_dir / "edited"
        print(f"\n===== {args.arm} :: {cid} — {c['title']} =====")

        out = run_query(use_handbook, c["query"], PRISTINE, sandbox)

        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "plan.md").write_text(out["plan"])
        (case_dir / "agent.diff").write_text(out["diff"])

        print(f"  plan: {len(out['plan'])} chars | diff: {len(out['diff'].splitlines())} lines")
        print(f"  -> {case_dir}/  (plan.md, agent.diff)")

    print(f"\nDone. Outputs under {run_dir}/<case>/")


if __name__ == "__main__":
    main()
