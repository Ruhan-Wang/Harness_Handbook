#!/usr/bin/env python3
"""run.py — end-to-end driver for the file-as-leaf handbook pipeline.

A bottom-up, full-coverage flow where the FILE is the handbook's leaf node:

  Phase 1   run_phase1.py        source → graph.json                  (no LLM)
  Phase 2a  phase2/read_files    read EVERY file → phase2/cards/ (one card/file)
  Phase 2b  phase2/synth_stages  purposes → skeleton.yaml + file_stage.json
  Phase 2c  phase2/organize_stages   order + group each stage's files
  Phase 3   phase3/build_handbook    bottom-up narration → handbook (md + optional html)

Example
-------
python3 run.py --lang auto --source-root /path/to/codex --work-dir work/codex \
    --read-detail deep --read-batch-size 1 --narrate-lang zh --phase3-html
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "phase2"))
sys.path.insert(0, str(_HERE / "phase3"))
sys.path.insert(0, str(_HERE / "shared"))

logger = logging.getLogger("run")


@contextmanager
def _phase(name: str):
    """Banner + elapsed around a phase, so the user sees which stage is running."""
    logger.info("══════════ %s ══════════", name)
    t0 = time.time()
    yield
    secs = time.time() - t0
    dur = (f"{secs:.0f}s" if secs < 60
           else f"{int(secs // 60)}m{int(secs % 60):02d}s")
    logger.info("══════════ %s done in %s ══════════", name, dur)


def _run(cmd: list[str]) -> None:
    printable = " ".join(str(c) for c in cmd)
    logger.info("$ %s", printable)
    env = dict(os.environ)
    extra = [str(_HERE), str(_HERE / "adapters"), str(_HERE / "shared"),
             str(_HERE / "phase1")]
    env["PYTHONPATH"] = os.pathsep.join(extra + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    r = subprocess.run([str(c) for c in cmd], env=env)
    if r.returncode != 0:
        sys.exit(f"[run] step failed (exit {r.returncode}): {printable}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="[%(asctime)s][%(levelname)5s] %(message)s",
                        level=logging.INFO)
    ap = argparse.ArgumentParser(description="Large-codebase handbook pipeline")
    ap.add_argument("--lang", default="auto")
    ap.add_argument("--narrate-lang", default="en", choices=["en", "zh"],
                    help="language of handbook-bound prose across 2a/2b/2c/3 "
                         "(en default; zh = Chinese file/function/stage/register text). "
                         "Distinct from --lang (which is source-language detection for phase 1).")
    ap.add_argument("--source-root", required=True, type=Path)
    ap.add_argument("--files", default="", help="comma-separated files (phase1); empty = auto")
    ap.add_argument("--work-dir", required=True, type=Path)
    ap.add_argument("--phase", default="all",
                    help="all | 1 | 2a | 2b | 2c | 2 (=2a-2c) | 3 | comma list e.g. '2c,3'")
    ap.add_argument("--read-batch-size", type=int, default=8,
                    help="files per LLM call in 2a (batch small files; the reader "
                         "auto-degrades batch→per-file→function-chunks on overflow)")
    ap.add_argument("--max-chars-per-file", type=int, default=0,
                    help="source cap per file in 2a; 0 = no truncation (read the "
                         "whole file — the default, right for deep mode)")
    ap.add_argument("--read-workers", type=int, default=12,
                    help="concurrent LLM calls in 2a (read_files)")
    ap.add_argument("--chunk-chars", type=int, default=60000,
                    help="deep 2a: if a whole-file call fails (too large), retry "
                         "splitting the file into function chunks of ~this size")
    ap.add_argument("--resume", action="store_true",
                    help="2a: skip files that already have a good card in cards/ "
                         "(only (re)process missing/failed ones)")
    ap.add_argument("--read-detail", default="brief", choices=["brief", "deep"],
                    help="2a depth. brief: 1-line purpose (batched). deep: "
                         "full-file read → detailed description + per-function "
                         "purpose/data_flow/relations (file = handbook leaf; "
                         "pair with --read-batch-size 1).")
    ap.add_argument("--assign-workers", type=int, default=12,
                    help="concurrent LLM calls in 2b file→stage assignment")
    ap.add_argument("--assign-batch-size", type=int, default=25,
                    help="files per LLM call in 2b file→stage assignment")
    ap.add_argument("--synth-mode", default="oneshot",
                    choices=["oneshot", "agent", "doctor"],
                    help="2b skeleton synthesis: oneshot (single LLM call, default); "
                         "agent (NexAU agent drafts, then actor-critic doctor loop "
                         "enriches until every file is assigned — needs LLM_BASE_URL/"
                         "LLM_MODEL/LLM_API_KEY); doctor (one-shot drafts, SAME doctor "
                         "loop enriches — no NexAU/LLM_* needed; richest skeletons).")
    ap.add_argument("--max-rounds", type=int, default=6,
                    help="2b agent mode: max draft->assign->doctor->reassign rounds "
                         "before forcing a stop (residual files stay unassigned)")
    ap.add_argument("--doctor-workers", type=int, default=1,
                    help="2b agent mode: parallel actor-critic diagnoses per round. "
                         "1 = single global actor (critics still parallel); >1 = one "
                         "split-only actor-critic per overloaded stage + a global "
                         "add/merge/remove pass, all concurrent (disjoint scopes).")
    ap.add_argument("--doctor-llm-workers", type=int, default=None,
                    help="2b agent mode: global cap on concurrent doctor LLM calls "
                         "across both nested pools (diagnosis x critics). Defaults to "
                         "max(assign_workers, doctor_workers, 3). Set to your "
                         "endpoint's safe concurrency.")
    ap.add_argument("--organize-workers", type=int, default=8,
                    help="2c: stages organized in parallel")
    ap.add_argument("--phase3-workers", type=int, default=8,
                    help="3: concurrent rollup LLM calls within one tree depth")
    ap.add_argument("--handbook-out", type=Path, default=None,
                    help="3: handbook output dir (default: <work-dir>/handbook)")
    ap.add_argument("--phase3-refresh", action="store_true",
                    help="3: ignore cached rollup summaries and regenerate")
    ap.add_argument("--phase3-html", action="store_true",
                    help="3: also render a multi-page progressive-disclosure HTML "
                         "site under <handbook>/html/ (open html/overview.html)")
    args = ap.parse_args(argv)

    source_root = args.source_root.resolve()
    work = args.work_dir.resolve()
    phases = _expand(args.phase)

    graph_path = work / "phase1" / "graph.json"
    skeleton_path = work / "phase2" / "skeleton.yaml"
    file_stage_path = work / "phase2" / "file_stage.json"
    cards_dir = work / "phase2" / "cards"   # one card per file (2a output)
    p2_work = work / "phase2"

    # ── Phase 1 ──
    if "1" in phases:
        with _phase("Phase 1: call graph"):
            cmd = [sys.executable, _HERE / "run_phase1.py", "--lang", args.lang,
                   "--source-root", source_root, "--out", work / "phase1"]
            if args.files.strip():
                cmd += ["--files", args.files]
            _run(cmd)

    if not (phases & {"2a", "2b", "2c", "3"}):
        return 0

    # Imports deferred until needed (heavy: requests/yaml).
    import skeleton_yaml
    from api_client import Api

    # Phase 3 (narration) needs no call graph — only the Phase 2 artifacts. So
    # the graph is loaded only when a 2x phase actually runs; a bare `--phase 3`
    # works even without graph.json in memory.
    graph = None
    api = None
    if phases & {"2a", "2b", "2c"}:
        if not graph_path.exists():
            sys.exit(f"[run] need {graph_path} — run phase 1 first")
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        api = Api()

    # ── Phase 2a: read EVERY file → one card per file (work/phase2/cards/) ──
    if "2a" in phases:
        import read_files
        with _phase("Phase 2a: read every file → cards"):
            res = read_files.read_purposes(
                api, graph, source_root,
                cards_dir=cards_dir,
                batch_size=args.read_batch_size,
                max_workers=args.read_workers,
                max_chars_per_file=args.max_chars_per_file,
                detail=args.read_detail,
                chunk_chars=args.chunk_chars,
                resume=args.resume,
                lang=args.narrate_lang)
            logger.info("[2a] %d/%d files described → %s",
                        res["coverage"]["n_described"], res["coverage"]["n_files"],
                        cards_dir)

    # ── Phase 2b: synthesize stages from cards → skeleton + file→stage ──
    if "2b" in phases:
        import read_files
        if not cards_dir.exists():
            sys.exit(f"[run] need {cards_dir} — run phase 2a first")
        import synth_stages
        with _phase("Phase 2b: synthesize stages from purposes + assign"):
            purposes = read_files.load_cards(cards_dir)
            skeleton_doc, res = synth_stages.synth(
                api, graph, purposes, assign_workers=args.assign_workers,
                assign_batch_size=args.assign_batch_size,
                synth_mode=args.synth_mode, max_rounds=args.max_rounds,
                doctor_workers=args.doctor_workers,
                doctor_llm_workers=args.doctor_llm_workers,
                lang=args.narrate_lang)
            skeleton_yaml.save_yaml(skeleton_doc, skeleton_path)
        file_stage_path.parent.mkdir(parents=True, exist_ok=True)
        file_stage_path.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        logger.info("[2b] %d/%d files assigned; %d non-empty buckets",
                    res["coverage"]["n_assigned"], res["coverage"]["n_files"],
                    sum(1 for v in res["buckets"].values() if v))

    # ── Phase 2c: stage-internal structure (file-level organize) ──
    # Order + group each stage's files into readable sub-groups (O(stages), cheap).
    if "2c" in phases:
        if not file_stage_path.exists():
            sys.exit(f"[run] need {file_stage_path} — run phase 2b first")
        import read_files
        if not cards_dir.exists():
            sys.exit(f"[run] phase 2c needs {cards_dir} — run phase 2a first")
        import organize_stages
        import yaml as _yaml
        with _phase("Phase 2c: organize stages (file-level)"):
            skeleton_doc = skeleton_yaml.load_yaml(skeleton_path)
            assign = json.loads(file_stage_path.read_text(encoding="utf-8"))
            purposes = read_files.load_cards(cards_dir)
            doc = organize_stages.organize(
                api, graph, skeleton_doc, assign, purposes,
                workers=args.organize_workers, lang=args.narrate_lang)
            out = p2_work / "stage_organization.yaml"
            out.write_text(_yaml.safe_dump(doc, sort_keys=False,
                           allow_unicode=True, width=10000), encoding="utf-8")
            logger.info("[2c] wrote %s (%d stages, %d/%d files organized)", out,
                        len(doc["stages"]), doc["coverage"]["n_organized"],
                        doc["coverage"]["n_files"])

    # ── Phase 3: bottom-up narration → handbook markdown tree ──
    if "3" in phases:
        org_path = p2_work / "stage_organization.yaml"
        for need in (cards_dir, skeleton_path, file_stage_path, org_path):
            if not need.exists():
                sys.exit(f"[run] phase 3 needs {need} — run phases 2a-2c first")
        import build_handbook
        out_dir = args.handbook_out or (work / "handbook")
        with _phase("Phase 3: bottom-up narration → handbook"):
            stats = build_handbook.build(
                p2_work, out_dir, lang=args.narrate_lang,
                workers=args.phase3_workers, refresh=args.phase3_refresh,
                html=args.phase3_html)
            logger.info("[3] wrote %d stage pages + index.md%s → %s",
                        stats["n_stages_summarized"],
                        " + html/" if args.phase3_html else "", stats["out_dir"])
            entry = "html/overview.html" if args.phase3_html else "overview.md"
            logger.info("[done] handbook at %s/%s", stats["out_dir"], entry)

    return 0


def _expand(spec: str) -> set[str]:
    spec = spec.strip().lower()
    if spec == "all":
        return {"1", "2a", "2b", "2c", "3"}
    if spec == "2":
        return {"2a", "2b", "2c"}
    # Comma list lets you run a subset in one process, e.g. "2a,2b" =
    # skeleton + file→stage, stopping before function classification (2c).
    if "," in spec:
        return {s.strip() for s in spec.split(",") if s.strip()}
    return {spec}


if __name__ == "__main__":
    raise SystemExit(main())
