#!/usr/bin/env python3
"""resync_large.py — FILE-level resync for the handbook_generate_large pipeline.

The member-level engine (`resync_handbook.py`) rolls a per-FUNCTION ledger
forward. The large-codebase pipeline is different in kind: its handbook LEAF is
a whole FILE (one deep card per file), stages are file buckets, and Phase 3
narrates bottom-up with a content-hash rollup cache. So its resync is file-level
too — and it leans on machinery the pipeline already has for cheap incremental
work:

  * Phase 2a `read_files.read_purposes(resume=True)` re-reads only files whose
    card is missing/stale — so deleting the changed files' cards and resuming
    re-describes exactly the changed + new files, keeping every unchanged card.
  * Phase 3 `build_handbook.build` is content-hash cached per stage rollup and
    re-renders leaf cards deterministically — so re-running it touches the LLM
    only for stages whose inputs actually changed; every other stage page comes
    out byte-identical (minimal handbook diff, the whole point).

Flow (the file-level analogue of the member engine's A→D):

  A. file verdicts      — content hash of each file, PRISTINE vs EDITED:
                          unchanged / changed / removed / new. No line numbers,
                          no per-function ledger — the file is the unit.
  B. graph refresh      — rebuild the call graph over the EDITED tree with the
                          large pipeline's own Phase 1 (scanned_files metadata,
                          so function-less files still get cards). No LLM.
  C. cards + buckets    — delete changed + removed cards, then read_purposes
                          (resume) re-reads changed + new; drop removed files
                          from file_stage buckets; assign NEW files to a stage
                          against the EXISTING skeleton; re-organize only the
                          stages whose membership changed (others stay verbatim).
  D. handbook writeback — re-run build_handbook (cache warm) → unchanged stage
                          pages come out identical, only affected ones re-narrate.

The skeleton (stage tree) is treated as STABLE across a resync — a code change
rolls files within the existing stages; it never re-synthesizes the spine (that
is a full rebuild). Everything mechanical (verdicts, bucket edits, organize
prune, coordinates) is zero-LLM; the LLM appears only at the read of changed/new
files, the assignment of new files, the (few) affected-stage organizes, and the
(cached) Phase-3 rollups.

Layout of a large handbook "skill" the resync edits (a per-case copy):
    <skill>/phase2/cards/            one JSON card per file
    <skill>/phase2/skeleton.yaml     stage tree (stable)
    <skill>/phase2/file_stage.json   file -> stage buckets
    <skill>/phase2/stage_organization.yaml
    <skill>/handbook/                rendered md (+ cache/ — keep it for warm rollups)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
HELPER_ROOT = HERE.parent
REPO_ROOT = HELPER_ROOT.parent

logger = logging.getLogger("resync_large")


# ─── locate + wire the large generator onto sys.path ──────────────────────────

def _resolve_large_gen() -> Path:
    """The file-level generator resync drives. $HANDBOOK_GEN_ROOT wins; else the
    checkout's handbook_generate_large."""
    if os.environ.get("HANDBOOK_GEN_ROOT"):
        return Path(os.environ["HANDBOOK_GEN_ROOT"])
    return REPO_ROOT / "handbook_generate_large"


_GEN = _resolve_large_gen()
for _p in (str(_GEN), str(_GEN / "adapters"), str(_GEN / "phase1"),
           str(_GEN / "phase2"), str(_GEN / "phase3"), str(_GEN / "shared"),
           str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import base                      # noqa: E402  (phase1: adapters registry)
    import build_graph               # noqa: E402  (phase1: graph assembly)
    import build_handbook            # noqa: E402  (phase3: driver)
    import file_assign               # noqa: E402  (phase2b: file->stage)
    import nav_pack as navmod        # noqa: E402  (phase2: file descriptors)
    import organize_stages           # noqa: E402  (phase2c: intra-stage order)
    import read_files                # noqa: E402  (phase2a: per-file cards)
    import skeleton_yaml             # noqa: E402  (shared: skeleton (de)ser)
    from ir import ModuleAnalysis    # noqa: E402  (phase1 IR)
    from skeleton_yaml import stage_short_descriptions  # noqa: E402
except ModuleNotFoundError as _e:    # noqa: E402
    raise ModuleNotFoundError(
        f"resync_large could not load the file-level generator from {_GEN} "
        f"(missing {_e.name!r}). It drives handbook_generate_large "
        "(read_files + file_assign + organize_stages + build_handbook). Point "
        "HANDBOOK_GEN_ROOT at a large-pipeline checkout."
    ) from _e

import resync_llm  # noqa: E402  (shared LLM backend; import-light)


def _sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def _file_sha(path: Path) -> str | None:
    try:
        return _sha1_bytes(path.read_bytes())
    except OSError:
        return None


# ─── B · rebuild the call graph over the edited tree (Phase 1, no LLM) ─────────

def build_graph_for(edited_root: Path, lang: str) -> dict:
    """Run the large pipeline's Phase 1 over `edited_root` and return the
    in-memory graph.json (with metadata.scanned_files, so function-less files are
    still covered). Mirrors run_phase1.py's auto/single-language handling."""
    edited_root = Path(edited_root).resolve()
    with tempfile.TemporaryDirectory(prefix="resync_graph_") as td:
        out = Path(td)
        if lang == "auto":
            groups = base.discover_all(edited_root)
            funcs, edges, scanned = [], [], []
            for lg, files in groups.items():
                a = base.get_adapter(lg).analyze(files, edited_root)
                funcs.extend(a.functions)
                edges.extend(a.edges)
                scanned.extend(str(p.relative_to(edited_root)) for p in files)
            analysis = ModuleAnalysis(functions=funcs, edges=edges)
            build_graph.build(analysis, source_root=edited_root,
                              scanned_files=scanned, out_dir=out,
                              lang="multi", default_ext=".py", verbose=False)
        else:
            adapter = base.get_adapter(lang)
            files = adapter.discover(edited_root)
            scanned = [str(p.relative_to(edited_root)) for p in files]
            default_ext = adapter.extensions[0] if adapter.extensions else ""
            analysis = adapter.analyze(files, edited_root)
            build_graph.build(analysis, source_root=edited_root,
                              scanned_files=scanned, out_dir=out, lang=lang,
                              default_ext=default_ext, verbose=False)
        return json.loads((out / "graph.json").read_text(encoding="utf-8"))


def _scanned_files(graph: dict) -> set[str]:
    """Every source file the edited tree exposes (1:1 with cards)."""
    return {f["file"] for f in navmod.all_file_descriptors(graph)}


# ─── A · file-level verdicts (content hash) ───────────────────────────────────

def _verdicts(known_files: set[str], edited_files: set[str],
              edited_root: Path, pristine_root: Path) -> dict:
    """Classify every file into unchanged / changed / removed / new by comparing
    PRISTINE and EDITED content (the file is the unit — no per-function ledger)."""
    removed = sorted(known_files - edited_files)
    new = sorted(edited_files - known_files)
    changed, unchanged = [], []
    for f in sorted(known_files & edited_files):
        pr = _file_sha(pristine_root / f)
        ed = _file_sha(edited_root / f)
        # A file the handbook knew but that never existed pristine (drift), or
        # whose bytes differ, is re-read; identical bytes stay put.
        if pr is not None and pr == ed:
            unchanged.append(f)
        else:
            changed.append(f)
    return {"unchanged": unchanged, "changed": changed,
            "removed": removed, "new": new}


# ─── C · card refresh via resume ──────────────────────────────────────────────

def _card_path(cards_dir: Path, rel: str) -> Path:
    return Path(cards_dir) / (rel + ".json")


def _detect_detail(known_cards: dict) -> str:
    """Infer the granularity the skill was built at from its existing cards: a
    card carrying a `description` or an annotated function means `deep`, else
    `brief`. resync must re-read at the SAME detail — hardcoding `deep` over a
    brief-built skill would (needlessly) re-read every file on resume (brief
    cards never satisfy the deep `_is_done` gate) and rewrite them all deeper,
    ballooning the diff. Empty skill → deep (the file-as-leaf default)."""
    for card in known_cards.values():
        if card.get("description") or any(
                f.get("purpose") for f in (card.get("functions") or [])):
            return "deep"
    return "brief" if known_cards else "deep"


def _refresh_cards(api, graph: dict, edited_root: Path, cards_dir: Path,
                   verdicts: dict, *, narrate_lang: str, workers: int,
                   detail: str, report: dict) -> None:
    """Delete the changed + removed files' cards, then read_purposes(resume=True)
    re-reads exactly the changed + new files (every unchanged card kept). `detail`
    matches the skill's own granularity so unchanged cards stay `done` on resume."""
    for rel in verdicts["changed"] + verdicts["removed"]:
        p = _card_path(cards_dir, rel)
        try:
            p.unlink(missing_ok=True)
        except OSError as e:  # noqa: BLE001
            report["errors"].append(f"card delete {rel}: {e!r}")
    resync_llm.set_phase("read")
    res = read_files.read_purposes(
        api, graph, edited_root, cards_dir=cards_dir,
        batch_size=1, max_workers=workers, max_chars_per_file=0,
        detail=detail, resume=True, lang=narrate_lang)
    report["cards_after"] = res["coverage"]["n_files"]
    report["cards_described"] = res["coverage"]["n_described"]


# ─── C · file_stage bucket sync (drop removed, assign new) ─────────────────────

def _assign_new_files(api, graph: dict, skeleton_doc: dict,
                      new_files: list[str], file_purposes: dict,
                      *, batch_size: int = 25) -> dict[str, dict]:
    """Place each NEW file in one of the EXISTING stages (the skeleton is stable
    on a resync). Reuses file_assign's batch classifier against the current
    stage menu. Returns {file: {"stage", "also"}} (a dropped file → unassigned)."""
    valid_ids = {s["id"] for s in skeleton_doc.get("stages", [])}
    stage_menu = "\n".join(
        f"  - {sid}: {desc}"
        for sid, desc in stage_short_descriptions(skeleton_doc).items())
    by_path = {f["file"]: f for f in navmod.all_file_descriptors(graph)}
    descriptors = [by_path.get(f, {"file": f, "dir": os.path.dirname(f) or ".",
                                    "n_functions": 0, "classes": [],
                                    "sample_functions": []})
                   for f in new_files]
    resync_llm.set_phase("assign")
    out: dict[str, dict] = {}
    for i in range(0, len(descriptors), batch_size):
        batch = descriptors[i:i + batch_size]
        out.update(file_assign._assign_batch(api, stage_menu, valid_ids, batch,
                                              file_purposes))
    return out


def _sync_buckets(api, graph: dict, skeleton_doc: dict, file_stage: dict,
                  verdicts: dict, file_purposes: dict, report: dict) -> set[str]:
    """Update file_stage.json in place: drop removed files, assign new files to a
    stage. Returns the set of stages whose membership CHANGED (need re-organize)."""
    fs = file_stage.setdefault("file_stage", {})
    buckets = file_stage.setdefault("buckets", {})
    for sid in {s["id"] for s in skeleton_doc.get("stages", [])}:
        buckets.setdefault(sid, [])
    affected: set[str] = set()

    removed = set(verdicts["removed"])
    for rel in removed:
        entry = fs.pop(rel, None)
        sid = (entry or {}).get("stage")
        if sid and rel in buckets.get(sid, []):
            buckets[sid] = [f for f in buckets[sid] if f != rel]
            affected.add(sid)

    unassigned: list[str] = []
    if verdicts["new"]:
        assigned = _assign_new_files(api, graph, skeleton_doc, verdicts["new"],
                                     file_purposes)
        for rel in verdicts["new"]:
            entry = assigned.get(rel) or {"stage": "unassigned", "also": []}
            sid = entry.get("stage")
            if sid and sid != "unassigned" and sid in buckets:
                buckets[sid].append(rel)
                fs[rel] = {"stage": sid, "also": entry.get("also", [])}
                affected.add(sid)
            else:
                fs[rel] = {"stage": "unassigned", "also": []}
                unassigned.append(rel)

    all_files = set(fs)
    n_unassigned = sum(1 for e in fs.values() if e.get("stage") == "unassigned")
    file_stage["coverage"] = {
        "n_files": len(all_files),
        "n_assigned": len(all_files) - n_unassigned,
        "unassigned": sorted(f for f, e in fs.items()
                             if e.get("stage") == "unassigned"),
    }
    report["new_unassigned"] = sorted(unassigned)
    return affected


# ─── C · stage-organization sync (only affected stages) ───────────────────────

def _prune_org_stage(entry: dict, removed: set[str]) -> dict:
    """Mechanically drop `removed` files from one organized-stage entry (no LLM),
    keeping every surviving group/file in place — the minimal edit."""
    groups = []
    for g in entry.get("groups", []) or []:
        gf = [f for f in (g.get("files") or [])
              if (f.get("file") if isinstance(f, dict) else f) not in removed]
        if gf:
            groups.append({**g, "files": gf})
    ordered = [f for f in entry.get("ordered_files", []) or [] if f not in removed]
    return {**entry, "groups": groups, "ordered_files": ordered}


def _sync_organization(api, graph: dict, skeleton_doc: dict, file_stage: dict,
                       org: dict, verdicts: dict, file_purposes: dict,
                       affected: set[str], report: dict, *,
                       narrate_lang: str) -> None:
    """Refresh stage_organization.yaml for the affected stages only. A stage that
    gained files is re-organized by the LLM (so new files are grouped/ordered); a
    stage that only lost files is pruned mechanically; an emptied stage is
    dropped. Every other stage's organization stays byte-identical."""
    stages_by_id = {s["id"]: s for s in skeleton_doc.get("stages", [])}
    buckets = file_stage.get("buckets", {})
    org_stages = org.setdefault("stages", {})
    adj = organize_stages.file_call_adjacency(graph)
    file_info = organize_stages._file_info_map(graph, file_purposes)
    removed = set(verdicts["removed"])
    gained_stages = set()
    for rel in verdicts["new"]:
        sid = file_stage.get("file_stage", {}).get(rel, {}).get("stage")
        if sid and sid != "unassigned":
            gained_stages.add(sid)

    reorganized: list[str] = []
    emptied: list[str] = []
    resync_llm.set_phase("organize")
    for sid in sorted(affected):
        files_now = list(buckets.get(sid, []))
        if not files_now:
            org_stages.pop(sid, None)
            emptied.append(sid)
            continue
        if sid in gained_stages or sid not in org_stages:
            stage = stages_by_id.get(sid, {"id": sid, "title": sid})
            org_stages[sid] = organize_stages._organize_one_stage(
                api, stage, files_now, file_info, adj, narrate_lang)
            reorganized.append(sid)
        else:
            org_stages[sid] = _prune_org_stage(org_stages[sid], removed)

    # Re-key in skeleton order for a stable, readable artifact.
    org["stages"] = {s["id"]: org_stages[s["id"]]
                     for s in skeleton_doc.get("stages", [])
                     if s["id"] in org_stages}
    all_bucket_files: set[str] = set()
    for fs in buckets.values():
        all_bucket_files.update(fs)
    org["coverage"] = {
        "n_files": len(all_bucket_files),
        "n_organized": sum(len(s.get("ordered_files", []))
                           for s in org["stages"].values()),
    }
    report["stages_reorganized"] = sorted(reorganized)
    report["stages_emptied"] = sorted(emptied)


# ─── the file-level resync driver ─────────────────────────────────────────────

def resync_large(edited_dir: Path, skill_dir: Path, pristine_dir: Path, *,
                 lang: str = "python", source_exts: tuple[str, ...] = (".py",),
                 narrate_lang: str = "zh", decl: dict | None = None,
                 report_out: Path | None = None, workers: int | None = None,
                 build: bool = True, html: bool = False, agent: bool = False,
                 api=None) -> dict:
    """Roll a large-pipeline handbook `skill_dir` forward to the code change in
    `edited_dir` (vs `pristine_dir`). Edits phase2/ + handbook/ in place and
    returns the report dict (the caller persists it + diffs the skill).

    `lang` drives Phase-1 source parsing ("python"/"rust"/.../"auto");
    `narrate_lang` ("en"/"zh") drives handbook prose. `build=False` stops after
    the Phase-2 sync (cards/buckets/organization) without re-rendering — useful
    for a cheap dry-run or a later render-only pass."""
    edited_dir = Path(edited_dir).resolve()
    skill_dir = Path(skill_dir).resolve()
    pristine_dir = Path(pristine_dir).resolve()
    phase2_dir = skill_dir / "phase2"
    handbook_dir = skill_dir / "handbook"
    cards_dir = phase2_dir / "cards"
    skeleton_path = phase2_dir / "skeleton.yaml"
    file_stage_path = phase2_dir / "file_stage.json"
    org_path = phase2_dir / "stage_organization.yaml"

    for need in (cards_dir, skeleton_path, file_stage_path):
        if not need.exists():
            raise FileNotFoundError(
                f"{need} missing — resync_large needs a built large-pipeline skill "
                "under <skill>/phase2 (cards/ + skeleton.yaml + file_stage.json).")

    workers = workers or max(1, int(os.environ.get("RESYNC_WORKERS", "4")))
    report: dict = {"scale": "large", "lang": lang, "narrate_lang": narrate_lang,
                    "verdicts": {}, "new_unassigned": [], "stages_reorganized": [],
                    "errors": [], "build": {}}

    if report_out is not None:
        resync_llm.set_usage_path(report_out.parent / "resync_llm_usage.jsonl")
    if api is None:
        api = resync_llm.get_api()

    # A/B — verdicts over a freshly-rebuilt edited-tree graph.
    graph = build_graph_for(edited_dir, lang)
    edited_files = _scanned_files(graph)
    known_cards = read_files.load_cards(cards_dir)
    known_files = set(known_cards)
    detail = _detect_detail(known_cards)      # re-read at the skill's own granularity
    report["detail"] = detail
    verdicts = _verdicts(known_files, edited_files, edited_dir, pristine_dir)
    report["verdicts"] = {k: (v if len(v) <= 200 else v[:200] + ["...(truncated)"])
                          for k, v in verdicts.items()}
    report["counts"] = {k: len(v) for k, v in verdicts.items()}

    if decl:  # optional reconcile: which declared functions map to which files
        q2f = {n.get("qualname"): n.get("file") for n in graph["nodes"].values()
               if n.get("kind") == "internal"}
        declared_files = sorted({q2f[q] for k in ("will_modify", "will_add",
                                                  "will_remove")
                                 for q in (decl.get(k) or []) if q2f.get(q)})
        report["declared_files"] = declared_files

    nothing = not (verdicts["changed"] or verdicts["removed"] or verdicts["new"])
    if nothing:
        report["note"] = "no file-level change detected — nothing to resync"
        if report_out is not None:
            report_out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        return report

    # C — cards, buckets, organization.
    _refresh_cards(api, graph, edited_dir, cards_dir, verdicts,
                   narrate_lang=narrate_lang, workers=workers, detail=detail,
                   report=report)
    file_purposes = read_files.load_cards(cards_dir)

    skeleton_doc = skeleton_yaml.load_yaml(skeleton_path)
    file_stage = json.loads(file_stage_path.read_text(encoding="utf-8"))
    affected = _sync_buckets(api, graph, skeleton_doc, file_stage, verdicts,
                             file_purposes, report)
    file_stage_path.write_text(json.dumps(file_stage, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    org = (yaml.safe_load(org_path.read_text(encoding="utf-8"))
           if org_path.exists() else {"metadata": {}, "stages": {}, "coverage": {}})
    _sync_organization(api, graph, skeleton_doc, file_stage, org, verdicts,
                       file_purposes, affected, report, narrate_lang=narrate_lang)
    org_path.write_text(yaml.safe_dump(org, sort_keys=False, allow_unicode=True,
                                       width=10000), encoding="utf-8")

    # D — writeback (Phase 3; rollup cache warm → only affected stages re-narrate).
    if build:
        resync_llm.set_phase("rollup")
        try:
            stats = build_handbook.build(
                phase2_dir, handbook_dir, api=api, lang=narrate_lang,
                workers=workers, refresh=False, html=html, agent=agent)
            report["build"] = {k: stats.get(k) for k in
                               ("n_stages_summarized", "n_files", "n_registers",
                                "out_dir")}
        except Exception as e:  # noqa: BLE001  (a build failure must not lose the sync)
            report["errors"].append(f"phase3 build failed: {e!r}")
        # A stage emptied by a removal is no longer rendered by build (it has no
        # content), but its OLD page/html would linger stale — drop them so the
        # emptied stage behaves exactly like a natively-empty one.
        for sid in report.get("stages_emptied", []):
            for p in (handbook_dir / f"{sid}.md",
                      handbook_dir / "html" / f"{sid}.html",
                      handbook_dir / "agent" / f"{sid}.md"):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

    if report_out is not None:
        report_out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="[%(asctime)s][%(levelname)5s] %(message)s",
                        level=logging.INFO)
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--edited", required=True, type=Path, help="edited source tree")
    ap.add_argument("--pristine", required=True, type=Path, help="original source tree")
    ap.add_argument("--skill", required=True, type=Path,
                    help="large handbook skill dir (contains phase2/ + handbook/)")
    ap.add_argument("--lang", default="python",
                    help="Phase-1 source language (python/rust/go/typescript/auto)")
    ap.add_argument("--narrate-lang", default="zh", choices=["en", "zh"],
                    help="handbook prose language")
    ap.add_argument("--report", type=Path, default=None, help="write report JSON here")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--no-build", action="store_true",
                    help="stop after the Phase-2 sync (no handbook re-render)")
    ap.add_argument("--html", action="store_true")
    ap.add_argument("--agent", action="store_true")
    args = ap.parse_args(argv)

    rep = resync_large(args.edited, args.skill, args.pristine, lang=args.lang,
                       narrate_lang=args.narrate_lang, report_out=args.report,
                       workers=args.workers, build=not args.no_build,
                       html=args.html, agent=args.agent)
    c = rep.get("counts", {})
    print(f"resync_large: {c.get('unchanged', 0)} unchanged | "
          f"{c.get('changed', 0)} changed | {c.get('removed', 0)} removed | "
          f"{c.get('new', 0)} new | reorganized "
          f"{len(rep.get('stages_reorganized', []))} stage(s) | "
          f"{len(rep.get('errors', []))} errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
