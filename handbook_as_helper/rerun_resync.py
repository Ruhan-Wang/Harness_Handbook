#!/usr/bin/env python3
"""rerun_resync.py — re-run the handbook resync for an ALREADY-EXECUTED case, without
touching the agents. The intended workflow: run the expensive agent phases ONCE per case
(typically with --no-translate), then derive the OTHER variant from the same artifacts —
both variants share the identical code diff and ledger, differing ONLY in whether the
cards were translated. That is what makes a Translate vs No-Translate comparison clean.

The source case dir must contain: edited/ (the executor's sandbox), plan.md or
plan_check.json (the declarations), and mapping.updated.yaml (the rolled ledger).
Classification is REPLAYED mechanically from that ledger (zero LLM); only the card
translation may call the LLM (and hits the shared content cache first).

Usage:
    python rerun_resync.py --src results/no-translate/Q1 \
                           --dst results/translate/Q1
    python rerun_resync.py --src ... --dst ... --no-translate   # mechanical-only rerun
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import resync_handbook as rh  # noqa: E402
from update_handbook import HANDBOOK_REFS  # noqa: E402  (paths only; no agent deps)
from targets import get_target  # noqa: E402

PRISTINE = get_target().pristine_root

_GIT = ["git", "-c", "user.email=eval@local", "-c", "user.name=eval"]


def _snapshot(src: Path, dst: Path) -> None:
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
    subprocess.run(["git", "init", "-q"], cwd=dst, check=True)
    subprocess.run(_GIT + ["add", "-A"], cwd=dst, check=True)
    subprocess.run(_GIT + ["commit", "-q", "-m", "pristine"], cwd=dst, check=True)


def _git_diff(workdir: Path) -> str:
    subprocess.run(_GIT + ["add", "-A"], cwd=workdir, check=True)
    return subprocess.run(_GIT + ["diff", "--cached"], cwd=workdir,
                          capture_output=True, text=True, check=True).stdout


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--src", required=True, help="completed case dir (edited/, plan, ledger)")
    ap.add_argument("--dst", required=True, help="output case dir for this variant")
    ap.add_argument("--no-translate", action="store_true",
                    help="mechanical-only resync (cards stay pending)")
    args = ap.parse_args()
    src, dst = Path(args.src).resolve(), Path(args.dst).resolve()

    # a usable source case needs the executor sandbox, the declarations and the ledger
    missing = [n for n in ("edited", "mapping.updated.yaml") if not (src / n).exists()]
    if not (src / "plan_check.json").exists() and not (src / "plan.md").exists():
        missing.append("plan_check.json/plan.md")
    if missing:
        raise SystemExit(f"{src} is not a completed case dir — missing: "
                         f"{', '.join(missing)} (run run_eval on it first)")

    # declarations: prefer the parsed/validated json, fall back to the plan text
    pc = src / "plan_check.json"
    if pc.exists():
        decl = json.loads(pc.read_text()).get("declarations") or {}
    else:
        decl = rh.parse_declarations((src / "plan.md").read_text())

    # the source ledger drives a MECHANICAL classification replay: changed/new entries
    # are re-inserted exactly as phase A classified them — zero LLM, and both variants
    # end up with the identical ledger
    ledger = yaml.safe_load((src / "mapping.updated.yaml").read_text())
    by_q: dict[str, list] = {}
    for sid, st in (ledger.get("stages") or {}).items():
        for m in st.get("members") or []:
            if m.get("type") in ("function", "region") and m.get("line_range"):
                by_q.setdefault(m["qualname"], []).append((sid, dict(m)))

    def replay(api, q, span, fname, mapping_doc, skeleton, graph, code_dir):
        if q not in by_q:
            return False                       # unseen qualname → normal fallback path
        for st in mapping_doc["stages"].values():
            st["members"] = [m for m in st.get("members") or []
                             if not (m.get("qualname") == q
                                     and m.get("type") in ("function", "region"))]
        for sid, m in by_q[q]:
            mapping_doc["stages"].setdefault(sid, {"members": []})["members"] \
                .append(dict(m))
        return True

    rh._reclassify_one = replay

    import os
    if not args.no_translate and not (os.environ.get("OPENAI_API_KEY")
                                       or os.environ.get("LLM_API_KEY")):
        print("NOTE: no OPENAI_API_KEY / LLM_API_KEY set — card translation will call the "
              "OpenAI endpoint and fail auth. Export OPENAI_API_KEY, or use --no-translate "
              "for a mechanical-only rerun.")

    dst.mkdir(parents=True, exist_ok=True)
    if src != dst:
        shutil.rmtree(dst / "edited", ignore_errors=True)
        shutil.copytree(src / "edited", dst / "edited",
                        ignore=shutil.ignore_patterns(".git"))
        for f in ("plan.md", "plan_check.json", "agent.diff"):
            if (src / f).exists():
                shutil.copy2(src / f, dst / f)
    _snapshot(HANDBOOK_REFS, dst / "handbook")

    rep = rh.resync(dst / "edited", dst / "handbook", PRISTINE, decl,
                    mapping_out=dst / "mapping.updated.yaml",
                    translate_cards=not args.no_translate)
    (dst / "resync_report.json").write_text(json.dumps(rep, indent=2,
                                                       ensure_ascii=False))
    (dst / "handbook_final.diff").write_text(_git_diff(dst / "handbook"))

    v = rep["verdicts"]
    print(f"{dst.name}: {sum(1 for x in v.values() if x == 'changed')} changed | "
          f"cards {len(rep['cards_translated'])} translated / "
          f"{len(rep['cards_pending'])} pending | "
          f"errors {len(rep['errors'])} | check ok={rep['check'].get('ok')}")


if __name__ == "__main__":
    main()
