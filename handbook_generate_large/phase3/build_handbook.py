# -*- coding: utf-8 -*-
"""build_handbook.py — Phase 3 driver: bottom-up render + rollup → markdown tree.

Walks the StageTree from the leaves up. The leaf layer (files) is rendered
deterministically (render_file). Every non-leaf node is summarized by one LLM
call (rollup) that reads its children's already-written summaries plus its
directly-owned files' one-liners. Finally the top-level summaries roll into a
system overview. Output is a directory tree: one markdown per stage + index.md.

Concurrency: post-order requires a parent to wait for its children, so we batch
stages by DEPTH, deepest first. Within a depth, siblings are independent and run
concurrently (ThreadPoolExecutor). Each stage's markdown is written the moment
its summary is ready (crash-safe; pairs with rollup's content-hash cache for
resumable reruns).
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "shared"))
sys.path.insert(0, str(_HERE.parent / "phase2"))

from api_client import Api  # noqa: E402

import load_inputs as load_mod  # noqa: E402
import registers as registers_mod  # noqa: E402
import render_file as rf  # noqa: E402
import rollup as rollup_mod  # noqa: E402

logger = logging.getLogger(__name__)


def _tree_depths(tree: load_mod.StageTree) -> dict[str, int]:
    """Depth of each stage from the ACTUAL parent/children relations (root = 0),
    NOT from the id's dot count. Batching the rollup by this guarantees a parent
    is processed strictly after its children regardless of how stage ids are
    named (doctor-added ids like 'crosscut-config' need not encode depth)."""
    depth: dict[str, int] = {sid: 0 for sid in tree.top_level}

    def assign(sid: str, d: int) -> None:
        depth[sid] = d
        for c in tree.children(sid):
            assign(c, d + 1)

    for top in tree.top_level:
        assign(top, 0)
    # Any stage unreachable from a top-level root (shouldn't happen, but be
    # safe) defaults to depth 0 so it still gets summarized.
    for sid in tree.order:
        depth.setdefault(sid, 0)
    return depth


# Bilingual UI labels for the markdown pages (the prose itself is LLM-generated
# in the chosen language; these are the fixed section headings / chrome).
_UI = {
    "en": {"crosscut_long": " (cross-cutting infrastructure)", "crosscut": " · (cross-cutting)",
           "substages": "## Sub-stages", "files": "## Files in this stage",
           "files_n": "files", "sysov": "## 🗺️ System Overview", "seealso": "## See also",
           "stage_index": "Stage Index", "regs_link": "State-flow registers",
           "regs_link_desc": "global state that flows across stages",
           "stages_link_desc": "every stage and what it does",
           "index_intro": "Each stage below links to its full page; the paragraph is the "
                          "stage's role in the system."},
    "zh": {"crosscut_long": "（横切基础设施）", "crosscut": " ·（横切）",
           "substages": "## 子阶段", "files": "## 本阶段的文件",
           "files_n": "个文件", "sysov": "## 🗺️ 系统总览", "seealso": "## 另见",
           "stage_index": "阶段索引", "regs_link": "状态流动寄存器",
           "regs_link_desc": "跨阶段流动的全局状态",
           "stages_link_desc": "每个阶段及其作用",
           "index_intro": "下面每个阶段都链接到它的完整页面；段落说明该阶段在系统中的作用。"},
}


def _stage_page_md(tree: load_mod.StageTree, sid: str, summary: str,
                   lang: str = "en") -> str:
    """The markdown page for one stage: overview + child links + own files."""
    ui = _UI.get(lang, _UI["en"])
    title = tree.title(sid)
    cc = ui["crosscut_long"] if tree.is_crosscut(sid) else ""
    lines: list[str] = [f"# {title}  `{sid}`{cc}", "", summary.strip(), ""]

    children = tree.children(sid)
    if children:
        lines += [ui["substages"], ""]
        for cid in children:
            ctitle = tree.title(cid)
            nfiles = tree.subtree_file_count(cid)
            lines.append(f"- [{ctitle}]({cid}.md) `{cid}` — {nfiles} {ui['files_n']}")
        lines.append("")

    direct = tree.direct_files(sid)
    if direct:
        # Group the direct files by their organization sub-groups when present,
        # so a big stage reads as themed sections instead of a flat list.
        groups = tree.groups(sid)
        lines += [ui["files"], ""]
        if groups:
            placed: set[str] = set()
            for g in groups:
                gtitle = (g.get("title") or "Files").strip()
                gsummary = (g.get("summary") or "").strip()
                gfiles = [f.get("file") if isinstance(f, dict) else f
                          for f in (g.get("files") or [])]
                gfiles = [f for f in gfiles if f]
                if not gfiles:
                    continue
                lines.append(f"### {gtitle}")
                if gsummary:
                    lines += [gsummary, ""]
                for rel in gfiles:
                    placed.add(rel)
                    lines += [rf.render_file_md(rel, tree.cards.get(rel), lang), ""]
            # Any direct file not captured by a group (defensive) still rendered.
            for rel in direct:
                if rel not in placed:
                    lines += [rf.render_file_md(rel, tree.cards.get(rel), lang), ""]
        else:
            for rel in direct:
                lines += [rf.render_file_md(rel, tree.cards.get(rel), lang), ""]

    return "\n".join(lines).rstrip() + "\n"


def _overview_md(system_overview: str, title: str, *,
                 has_registers: bool, lang: str = "en") -> str:
    """The system-overview landing page: just the prose, plus nav links to the
    register and stage-index pages."""
    ui = _UI.get(lang, _UI["en"])
    lines = [f"# {title}", "", ui["sysov"], "",
             system_overview.strip(), "", "---", "", ui["seealso"], ""]
    if has_registers:
        lines.append(f"- [{ui['regs_link']}](register.md) — {ui['regs_link_desc']}")
    lines.append(f"- [{ui['stage_index']}](index.md) — {ui['stages_link_desc']}")
    return "\n".join(lines).rstrip() + "\n"


def _index_md(tree: load_mod.StageTree, summaries: dict[str, str], title: str,
              roots: list[str] | None = None, lang: str = "en") -> str:
    """The stage index: every stage with its title, link, AND its full rollup
    overview, so a reader knows what each stage does without opening it."""
    ui = _UI.get(lang, _UI["en"])
    lines = [f"# {title} — {ui['stage_index']}", "", ui["index_intro"], ""]

    def walk(sid: str, depth: int) -> None:
        stitle = tree.title(sid)
        cc = ui["crosscut"] if tree.is_crosscut(sid) else ""
        nfiles = tree.subtree_file_count(sid)
        # Heading level reflects tree depth (capped at H6) for a readable TOC.
        h = "#" * min(depth + 2, 6)
        lines.append(f"{h} [{stitle}]({sid}.md) `{sid}`{cc} — {nfiles} {ui['files_n']}")
        summ = (summaries.get(sid) or "").strip()
        if summ:
            # .extend (not `lines += ...`): inside this closure, `lines += ...`
            # is an assignment to `lines`, which would make `lines` a local and
            # break the .append above with UnboundLocalError.
            lines.extend(["", summ, ""])
        for cid in tree.children(sid):
            walk(cid, depth + 1)

    for top in (roots if roots is not None else tree.top_level):
        walk(top, 0)
    return "\n".join(lines).rstrip() + "\n"


def build(phase2_dir: Path, out_dir: Path, *, api: Api | None = None,
          lang: str = "zh", workers: int = 8, refresh: bool = False,
          subtree: str | None = None, html: bool = False,
          html_single: bool = False, agent: bool = False,
          model: str | None = None, api_user: str | None = None,
          api_key: str | None = None) -> dict:
    """Build the handbook under `out_dir`. Returns a small stats dict.

    subtree: if given (a stage id), build ONLY that stage's subtree (and emit a
    minimal index pointing at it) — used for a cheap dry-run / inspection of one
    branch without summarizing all 140 stages.

    model / api_user / api_key: override the LLM endpoint identity (else the
    Api() defaults). Only used when `api` is not supplied by the caller.
    """
    from progress import Progress

    if api is None:
        api_kwargs = {}
        if model:
            api_kwargs["model_marker"] = model
        if api_user:
            api_kwargs["user"] = api_user   # deprecated; ignored by the OpenAI client
        if api_key:
            api_kwargs["api_key"] = api_key
        api = Api(**api_kwargs)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "cache"

    tree = load_mod.load_all(phase2_dir)

    # Optional subtree restriction (dry-run): keep only `subtree` + descendants.
    allowed: set[str] | None = None
    if subtree:
        if subtree not in tree.stages_by_id:
            raise ValueError(f"--subtree {subtree!r} not a stage id")
        allowed = set()

        def collect(sid: str) -> None:
            allowed.add(sid)
            for c in tree.children(sid):
                collect(c)
        collect(subtree)
        logger.info("phase3: subtree mode — only %s (%d stages)",
                    subtree, len(allowed))

    # Stages that need a rollup summary = every stage that has children or files.
    # (A stage with neither is an empty placeholder; skip its page entirely.)
    def has_content(sid: str) -> bool:
        return bool(tree.children(sid) or tree.buckets.get(sid))

    summaries: dict[str, str] = {}     # sid -> rollup summary
    work_ids = [sid for sid in tree.order if has_content(sid)
                and (allowed is None or sid in allowed)]

    # Batch by depth, DEEPEST FIRST, so a parent's rollup always runs after its
    # children's summaries exist. Siblings within a depth run concurrently.
    # Depth comes from the real tree (not id dot-count), so the post-order
    # guarantee holds for any stage-id naming.
    node_depth = _tree_depths(tree)
    by_depth: dict[int, list[str]] = {}
    for sid in work_ids:
        by_depth.setdefault(node_depth[sid], []).append(sid)
    max_depth = max(by_depth) if by_depth else 0

    prog = Progress(logger, "phase3 rollup", len(work_ids))

    def _one(sid: str) -> tuple[str, str]:
        child_summaries = [(tree.title(c), summaries.get(c, ""))
                           for c in tree.children(sid)
                           if c in summaries]      # only content-bearing children
        file_lines = [rf.file_one_liner(rel, tree.cards.get(rel))
                      for rel in tree.direct_files(sid)]
        summ = rollup_mod.summarize_stage(
            api, sid, tree.title(sid), tree.description(sid),
            child_summaries, file_lines, cache_dir=cache_dir, refresh=refresh,
            lang=lang)
        return sid, summ

    for depth in range(max_depth, -1, -1):
        batch = by_depth.get(depth, [])
        if not batch:
            continue
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_one, sid): sid for sid in batch}
            for fut in cf.as_completed(futs):
                sid = futs[fut]
                try:
                    _, summ = fut.result()
                except Exception as e:  # noqa: BLE001
                    logger.warning("phase3 rollup %s failed: %s", sid, e)
                    summ = tree.description(sid) or tree.title(sid)
                summaries[sid] = summ
                # Write the stage page immediately (crash-safe).
                (out_dir / f"{sid}.md").write_text(
                    _stage_page_md(tree, sid, summ, lang), encoding="utf-8")
                prog.tick(note=f"stage {sid}")

    # System overview. In full mode it rolls up all top-level stages; in subtree
    # mode there is no whole-system view, so use the subtree root's own summary.
    import os
    title = os.environ.get("HANDBOOK_TITLE", "System Handbook")
    index_roots = [subtree] if subtree else tree.top_level
    if subtree:
        system_overview = summaries.get(subtree, tree.description(subtree))
    else:
        top_summaries = [(tree.title(sid), summaries.get(sid, ""))
                         for sid in tree.top_level if sid in summaries]
        archetype = tree.metadata.get("archetype", "")
        system_overview = rollup_mod.summarize_system(
            api, archetype, top_summaries, cache_dir=cache_dir, refresh=refresh,
            lang=lang)

    # ── State registers (full mode only) ──
    # One LLM call recovers the system's cross-stage global state + maps each
    # register to the stages it touches. Subtree mode has no whole-system view,
    # so registers are skipped there (keeps the dry-run light).
    registers: list[dict] = []
    if not subtree:
        reg_top = [(sid, tree.title(sid), summaries.get(sid, ""))
                   for sid in tree.top_level if sid in summaries]
        data_model_files = [
            (rel, (card.get("purpose") or ""))
            for rel, card in tree.cards.items()
            if card.get("role") == "data_model"]
        valid_ids = set(tree.order)
        registers = registers_mod.extract_registers(
            api, reg_top, data_model_files, valid_ids,
            cache_dir=cache_dir, refresh=refresh, lang=lang)

    # Three separate top-level pages:
    #   overview.md — system overview prose + nav links
    #   register.md — the state-flow register table (only if any registers)
    #   index.md    — every stage with its title, link, and full rollup overview
    (out_dir / "overview.md").write_text(
        _overview_md(system_overview, title, has_registers=bool(registers), lang=lang),
        encoding="utf-8")
    if registers:
        register_table = registers_mod.render_register_table(
            registers, title_of=tree.title, lang=lang)
        sf = "状态流动" if lang == "zh" else "State Flow"
        (out_dir / "register.md").write_text(
            f"# {title} — {sf}\n\n{register_table}", encoding="utf-8")
    (out_dir / "index.md").write_text(
        _index_md(tree, summaries, title, roots=index_roots, lang=lang),
        encoding="utf-8")

    # Annotate each stage page that a register touches: append a "📊 本阶段涉及
    # 的状态" section. Idempotent — skip if the page already has the marker (so a
    # rerun never double-appends).
    if registers:
        touched: set[str] = set()
        for r in registers:
            touched.update(r.get("stages", []))
        marker = registers_mod.stage_section_marker(lang)
        for sid in touched:
            page = out_dir / f"{sid}.md"
            if not page.exists():
                continue
            existing = page.read_text(encoding="utf-8")
            if marker in existing:
                continue
            section = registers_mod.render_stage_registers(sid, registers, lang)
            if section:
                page.write_text(existing.rstrip() + "\n\n" + section,
                                encoding="utf-8")

    stats = {
        "n_stages_summarized": len(summaries),
        "n_files": len(tree.cards),
        "n_registers": len(registers),
        "out_dir": str(out_dir),
    }

    # Agent arm — the deterministic, fixed-schema locator index for a code agent.
    # Pure rendering: reuses the in-memory tree/summaries/registers (no extra
    # LLM). Writes under <out_dir>/agent/. In subtree mode it scopes to the same
    # subtree root so a dry-run stays cheap.
    if agent:
        import render_agent
        roots = [subtree] if subtree else None
        astats = render_agent.render_agent_site(
            tree, summaries, registers, out_dir, lang=lang, roots=roots)
        stats["n_agent_pages"] = astats["n_stage_pages"]
        stats["agent_dir"] = astats["agent_dir"]

    # Optional HTML. Reuses the in-memory tree/summaries/registers — no reload,
    # no LLM. `html` = multi-page site under html/; `html_single` = one
    # self-contained handbook.html with everything (collapsed <details> per stage).
    if html:
        import render_html
        hstats = render_html.render_site(tree, summaries, system_overview,
                                         registers, out_dir, lang=lang)
        stats["n_html_pages"] = hstats["n_stage_pages"]
        stats["html_dir"] = hstats["html_dir"]
    if html_single:
        import render_html
        sstats = render_html.render_single_page(tree, summaries, system_overview,
                                                registers, out_dir, lang=lang)
        stats["single_page"] = sstats["path"]
        stats["single_page_bytes"] = sstats["bytes"]

    logger.info("phase3 done: %d stage pages, %d registers, "
                "overview.md + register.md + index.md%s → %s",
                stats["n_stages_summarized"], stats["n_registers"],
                " + html/" if html else "", out_dir)
    return stats


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(format="[%(asctime)s][%(levelname)5s] %(message)s",
                        level=logging.INFO)
    ap = argparse.ArgumentParser(
        description="Phase 3 — bottom-up handbook narration (file-as-leaf)")
    ap.add_argument("--phase2-dir", type=Path, required=True,
                    help="dir holding cards/ + skeleton.yaml + file_stage.json "
                         "+ stage_organization.yaml (e.g. work/codex/phase2)")
    ap.add_argument("--out", type=Path, required=True, help="handbook output dir")
    ap.add_argument("--lang", default="en", choices=["en", "zh"],
                    help="handbook narration language (en default; zh = Chinese)")
    ap.add_argument("--workers", type=int, default=8,
                    help="concurrent rollup LLM calls within one tree depth")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore cached rollup summaries and regenerate")
    ap.add_argument("--subtree", default=None,
                    help="build only this stage id's subtree (dry-run/inspect)")
    ap.add_argument("--html", action="store_true",
                    help="also render a multi-page, progressively-disclosed HTML "
                         "site under <out>/html/ (no LLM; open html/overview.html)")
    ap.add_argument("--html-single", action="store_true",
                    help="also render ONE self-contained <out>/handbook.html with "
                         "everything (each stage a collapsed <details>; no LLM)")
    ap.add_argument("--agent", action="store_true",
                    help="also render the AGENT arm: a deterministic fixed-schema "
                         "locator index under <out>/agent/ (no LLM)")
    ap.add_argument("--model", default=None,
                    help="LLM model for rollup/register (else Api() default = "
                         "$OPENAI_MODEL or gpt-4o-mini)")
    ap.add_argument("--api-user", default=None,
                    help="(deprecated; ignored) kept for backward-compat")
    ap.add_argument("--api-key", default=None,
                    help="LLM API key (else $OPENAI_API_KEY)")
    args = ap.parse_args(argv)

    stats = build(args.phase2_dir, args.out, lang=args.lang,
                  workers=args.workers, refresh=args.refresh,
                  subtree=args.subtree, html=args.html,
                  html_single=args.html_single, agent=args.agent,
                  model=args.model, api_user=args.api_user, api_key=args.api_key)
    logger.info("wrote handbook: %s stage pages → %s",
                stats["n_stages_summarized"], stats["out_dir"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
