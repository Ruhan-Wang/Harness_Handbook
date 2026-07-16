#!/usr/bin/env python3
"""update_handbook.py — resync the handbook to a code change ALREADY produced by run_eval.

This is the optional second step. `run_eval.py` produces, per case:
    plan.md      the planner's edit plan (carries the declarations block)
    agent.diff   the executor's code diff
    edited/      the executor's sandbox (the changed source tree)

`update_handbook.py` takes one such case directory and rolls the handbook's DERIVED layer
(cards, line anchors, code-sites, index) forward to match that change — see
resync_handbook.py for the A→D mechanics. It does NOT re-run the agent.

    plan → diff   (run_eval.py)        →   update handbook   (this script, optional)

Multi-language: the engine gets its spans / syntax gate / rename fingerprint / call
graph from lang_layer (Python via `ast`, Rust/TypeScript/Go/... via the
handbook_generate_small tree-sitter adapters). It refuses only a language with no
registered adapter. A function-level phase-2 mapping for the target is still required.

Usage:
    python update_handbook.py runs/handbook/Q1
    python update_handbook.py runs/handbook/Q1 --no-translate     # skip card translation
    python update_handbook.py runs/handbook/*                     # several cases
    EVAL_TARGET=terminus2 python update_handbook.py runs/handbook/Q1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from targets import get_target  # noqa: E402

# The handbook whose per-case copy the resync edits: this repo's carved references when
# built, else the sibling v1's (data reuse only). Overridable via HANDBOOK_REFS.
HANDBOOK_REFS = (
    Path(os.environ["HANDBOOK_REFS"]) if os.environ.get("HANDBOOK_REFS")
    else next(
        (p for p in (
            HELPER_ROOT / "handbook_skills" / "handbook_skill_terminus" / "references",
            HELPER_ROOT / "handbook_skill" / "references",
        ) if p.exists()),
        HELPER_ROOT / "handbook_skills" / "handbook_skill_terminus" / "references",
    )
)


def resync_case(case_dir: Path, pristine: Path, translate_cards: bool = True,
                lang: str = "python",
                source_exts: tuple[str, ...] = (".py",)) -> dict | None:
    """Resync the handbook for one completed case dir. Returns the resync report (or None
    if there was nothing to do). Writes mapping.updated.yaml, resync_report.json,
    plan_check.json, handbook_final.diff into the case dir."""
    from code_agent import _git_diff, _snapshot_git
    from resync_handbook import parse_declarations, resync, validate_declarations

    sandbox = case_dir / "edited"
    plan_md = case_dir / "plan.md"
    if not sandbox.exists() or not plan_md.exists():
        raise SystemExit(
            f"{case_dir} is not a completed case dir — need edited/ and plan.md "
            "(run run_eval.py on it first)."
        )
    diff = (case_dir / "agent.diff")
    if diff.exists() and not diff.read_text().strip():
        print(f"  .. {case_dir.name}: empty diff — nothing to resync")
        return None

    plan = plan_md.read_text()
    decl = parse_declarations(plan)
    check = validate_declarations(decl)
    (case_dir / "plan_check.json").write_text(
        json.dumps({"declarations": decl, **check}, indent=2, ensure_ascii=False))
    if not check["ok"]:
        print(f"  !! declaration check: {check['errors'][:3]}")

    hb_sb = case_dir / "handbook"
    _snapshot_git(HANDBOOK_REFS, hb_sb)
    try:
        rep = resync(sandbox, hb_sb, pristine, decl,
                     mapping_out=case_dir / "mapping.updated.yaml",
                     translate_cards=translate_cards, lang=lang,
                     source_exts=source_exts)
    except Exception as e:  # noqa: BLE001  (a resync failure must not lose the run)
        (case_dir / "resync_report.json").write_text(json.dumps({"fatal": repr(e)}))
        print(f"  !! resync failed: {e!r}")
        return None
    (case_dir / "resync_report.json").write_text(
        json.dumps(rep, indent=2, ensure_ascii=False))
    (case_dir / "handbook_final.diff").write_text(_git_diff(hb_sb))

    v = rep["verdicts"]
    print(f"  {case_dir.name}: {sum(1 for x in v.values() if x == 'unchanged')} unchanged | "
          f"{sum(1 for x in v.values() if x == 'changed')} changed | "
          f"{len(rep['removed'])} removed | {len(rep['renamed'])} renamed | "
          f"{len(rep['new'])} new | anchors {rep['anchors_refreshed']} | cards "
          f"{len(rep.get('cards_patched', []))} patched / "
          f"{len(rep['cards_translated'])} rewritten / {len(rep['cards_deleted'])} "
          f"deleted"
          + (f" / {len(rep['cards_pending'])} PENDING (translation off)"
             if rep.get("cards_pending") else "")
          + f" | {len(rep['errors'])} errors")
    if rep["missed"] or rep["unplanned"]:
        print(f"  reconcile: missed={rep['missed']} unplanned={rep['unplanned']}")
    if not rep["check"].get("ok", True):
        bad = {k: len(x) for k, x in rep["check"].items() if k != "ok" and x}
        print(f"  !! end checks RED: {bad}")
    return rep


# The large handbook skill (phase2/ + handbook/) the FILE-level resync edits, when
# HANDBOOK_GEN_SCALE=large. Overridable via HANDBOOK_LARGE_SKILL.
HANDBOOK_LARGE_SKILL = (
    Path(os.environ["HANDBOOK_LARGE_SKILL"]) if os.environ.get("HANDBOOK_LARGE_SKILL")
    else HELPER_ROOT / "handbook_skills" / "handbook_skill_large"
)


def resync_case_large(case_dir: Path, pristine: Path, *, lang: str,
                      narrate_lang: str, build: bool = True) -> dict | None:
    """FILE-level resync for a large-pipeline skill (see resync_large.py). Snapshots
    the large skill into <case>/handbook_large, rolls it forward to <case>/edited,
    and writes resync_report.json + handbook_final.diff into the case dir."""
    from code_agent import _git_diff, _snapshot_git
    from resync_large import resync_large

    sandbox = case_dir / "edited"
    plan_md = case_dir / "plan.md"
    if not sandbox.exists():
        raise SystemExit(f"{case_dir} has no edited/ — run run_eval.py on it first.")
    diff = case_dir / "agent.diff"
    if diff.exists() and not diff.read_text().strip():
        print(f"  .. {case_dir.name}: empty diff — nothing to resync")
        return None
    if not HANDBOOK_LARGE_SKILL.exists():
        raise SystemExit(
            f"large skill not found at {HANDBOOK_LARGE_SKILL} — build it with "
            "handbook_generate_large (needs phase2/ + handbook/), or set "
            "HANDBOOK_LARGE_SKILL.")

    decl = None
    if plan_md.exists():
        from resync_decl import parse_declarations  # lazy, member-free
        decl = parse_declarations(plan_md.read_text())

    skill_sb = case_dir / "handbook_large"
    _snapshot_git(HANDBOOK_LARGE_SKILL, skill_sb)
    try:
        rep = resync_large(sandbox, skill_sb, pristine, lang=lang,
                           narrate_lang=narrate_lang, decl=decl,
                           report_out=case_dir / "resync_report.json", build=build)
    except Exception as e:  # noqa: BLE001  (a resync failure must not lose the run)
        (case_dir / "resync_report.json").write_text(json.dumps({"fatal": repr(e)}))
        print(f"  !! resync_large failed: {e!r}")
        return None
    (case_dir / "handbook_final.diff").write_text(_git_diff(skill_sb))
    c = rep.get("counts", {})
    print(f"  {case_dir.name}: {c.get('unchanged', 0)} unchanged | "
          f"{c.get('changed', 0)} changed | {c.get('removed', 0)} removed | "
          f"{c.get('new', 0)} new | reorganized "
          f"{len(rep.get('stages_reorganized', []))} stage(s) | "
          f"{len(rep.get('errors', []))} errors")
    return rep


def _scale() -> str:
    return (os.environ.get("HANDBOOK_GEN_SCALE") or "").strip().lower()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("cases", nargs="+", type=Path,
                    help="one or more completed case dirs (each with edited/ + plan.md)")
    ap.add_argument("--target", default=os.environ.get("EVAL_TARGET", "terminus2"),
                    help="target project (default: terminus2). Any language with a "
                         "registered adapter (python, rust, typescript, go, ...).")
    ap.add_argument("--no-translate", action="store_true",
                    help="resync WITHOUT the final card translation (the most expensive LLM "
                         "step): ledger/coordinates/anchors/registers/index still sync, "
                         "changed cards keep their old prose, new ones are listed as "
                         "cards_pending. Env equivalent: RESYNC_TRANSLATE=0")
    ap.add_argument("--narrate-lang", default=os.environ.get("RESYNC_NARRATE_LANG", "zh"),
                    choices=["en", "zh"],
                    help="handbook prose language for the FILE-level (large) resync")
    args = ap.parse_args()

    target = get_target(args.target)

    # SCALE=large → the FILE-level engine (resync_large): the handbook leaf is a
    # whole file, so the resync is file-level and drives handbook_generate_large.
    # This path never imports the member engine (which hard-fails under SCALE=large).
    if _scale() in ("large", "big"):
        print(f"resync[LARGE] target={target.name} | lang={target.language} | "
              f"pristine={target.pristine_root} | skill={HANDBOOK_LARGE_SKILL} | "
              f"narrate={args.narrate_lang}")
        for case_dir in args.cases:
            resync_case_large(case_dir.resolve(), target.pristine_root,
                              lang=target.language, narrate_lang=args.narrate_lang)
        return

    # resync is multi-language (lang_layer: Python via ast, others via the
    # handbook_generate_small tree-sitter adapters). Only refuse a language with no
    # registered adapter.
    import lang_layer as _L
    supported = _L.supported_languages()
    if target.language not in supported:
        raise SystemExit(
            f"resync has no language adapter for '{target.name}' ({target.language}). "
            f"Adapters available: {', '.join(supported)}. Install "
            "'tree-sitter-language-pack' for the tree-sitter languages, or add an "
            "adapter under handbook_generate_small/adapters/."
        )
    source_exts = tuple(_L.ext_of(g) for g in target.source_globs) or (".py",)
    pristine = target.pristine_root

    translate_cards = (not args.no_translate
                       and os.environ.get("RESYNC_TRANSLATE", "1").lower()
                       not in ("0", "false", "off"))

    print(f"resync target={target.name} | lang={target.language} | pristine={pristine} | "
          f"handbook_refs={HANDBOOK_REFS} | translate={translate_cards}")
    for case_dir in args.cases:
        resync_case(case_dir.resolve(), pristine, translate_cards=translate_cards,
                    lang=target.language, source_exts=source_exts)


if __name__ == "__main__":
    main()
