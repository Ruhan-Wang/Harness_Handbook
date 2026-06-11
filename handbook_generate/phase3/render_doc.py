# -*- coding: utf-8 -*-
"""Render a HandbookDoc tree → markdown (linear) or HTML (single-page nested
collapse). Both walk the SAME tree; neither reverse-engineers structure from
text. The reader-facing chapter numbers come straight off the tree, so Pass C's
stage-id gaps never show.

  render_md   — linear markdown, GitHub-readable. Reuses render_member for the
                Tier-3 function cards (which are <details> blocks).
  render_html — L1 overview always visible; each stage a collapsible <details>
                holding its Tier-2 prose + nested sub-stages + Tier-3 cards.
                TOC is built by walking the tree, not by regex-stripping details.
"""
from __future__ import annotations

import sys
from html import escape
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import PHASE2_FINAL, SOURCE_ROOT, UI_STRINGS  # noqa: E402
from document import HandbookDoc, StageNode  # noqa: E402
from render_member import render_unit  # noqa: E402
from translate_member import collect_units  # noqa: E402


def _load_units_by_stage(doc: HandbookDoc) -> dict:
    """Reconstruct Tier-3 units (with verified source) per stage so the cards
    can be rendered. The tree stores translations; source comes from mapping."""
    mapping = yaml.safe_load((PHASE2_FINAL / "mapping.yaml").read_text(encoding="utf-8"))
    out: dict = {}
    for sid in doc.order:
        members = mapping.get("stages", {}).get(sid, {}).get("members", [])
        units = collect_units(sid, members, SOURCE_ROOT)
        out[sid] = {u.qualname: u for u in units}
    return out


# ─── markdown ────────────────────────────────────────────────────────────────


def render_md(doc: HandbookDoc, *, lang: str = "zh") -> str:
    ui = UI_STRINGS.get(lang, UI_STRINGS["zh"])
    units_by_stage = _load_units_by_stage(doc)
    out: list = [f"# {ui['title']}", ""]

    if doc.overview_md:
        out += [f"## {ui['overview']}", "", doc.overview_md, "", "---", ""]

    for sid in doc.order:
        s = doc.stages[sid]
        level = "###" if s.parent else "##"
        out += [f"{level} {s.chapter} · {s.title}", ""]
        if s.logical_md:
            out += [s.logical_md, ""]
        unit_idx = units_by_stage.get(sid, {})
        for fn in s.functions:
            unit = unit_idx.get(fn.qualname)
            if unit is None or not fn.translation:
                continue
            try:
                out.append(render_unit(unit, fn.translation, lang))
            except Exception as e:  # noqa: BLE001
                out.append(f"<!-- render failed for {fn.qualname}: {e} -->")
            out.append("")

    if doc.registers_md:
        out += [f"## {ui['registers']}", "", doc.registers_md, ""]

    return "\n".join(out)


# ─── HTML (single-page nested collapse) ──────────────────────────────────────


def _md(md_text: str):
    """A fresh markdown converter matching render_html's setup (md_in_html so
    markdown inside <details> renders)."""
    import re

    import markdown
    pre = re.sub(
        r'<details(\s+[^>]*)?>',
        lambda m: f'<details{m.group(1) or ""} markdown="1">'
        if "markdown=" not in (m.group(1) or "") else m.group(0),
        md_text,
    )
    md = markdown.Markdown(
        extensions=["extra", "codehilite", "sane_lists", "smarty"],
        extension_configs={"codehilite": {"css_class": "codehilite", "guess_lang": False}},
        output_format="html5",
    )
    return md.convert(pre)


def _toc_from_tree(doc: HandbookDoc, ui: dict) -> str:
    lines = ['<nav class="toc">', "<h2>目录</h2>",
             f'<a href="#overview" class="level-2">{escape(ui["overview"])}</a>']
    for sid in doc.order:
        s = doc.stages[sid]
        cls = "level-3" if s.parent else "level-2"
        anchor = f"stage-{s.chapter}"
        wrap = (f'<li><a href="#{anchor}" class="{cls}">{escape(s.chapter)} · '
                f'{escape(s.title)}</a></li>' if s.parent
                else f'<a href="#{anchor}" class="{cls}">{escape(s.chapter)} · {escape(s.title)}</a>')
        lines.append(wrap)
    if doc.registers_md:
        lines.append(f'<a href="#registers" class="level-2">{escape(ui["registers"])}</a>')
    lines.append("</nav>")
    return "\n".join(lines)


def _stage_html(doc: HandbookDoc, s: StageNode, units_by_stage: dict, ui: dict) -> str:
    anchor = f"stage-{s.chapter}"
    body = []
    if s.logical_md:
        body.append(_md(s.logical_md))
    # nested sub-stages
    for cid in s.children:
        if cid in doc.stages:
            body.append(_stage_html(doc, doc.stages[cid], units_by_stage, ui))
    # function cards
    unit_idx = units_by_stage.get(s.id, {})
    cards = []
    for fn in s.functions:
        unit = unit_idx.get(fn.qualname)
        if unit is None or not fn.translation:
            continue
        try:
            cards.append(render_unit(unit, fn.translation, "zh"))
        except Exception:  # noqa: BLE001
            continue
    if cards:
        body.append(f'<h4>📚 {escape(ui["fns"])}</h4>')
        body.append(_md("\n\n".join(cards)))
    return (
        f'<details class="stage" id="{anchor}">'
        f'<summary><b>{escape(s.chapter)} · {escape(s.title)}</b></summary>'
        f'<div class="details-body">{"".join(body)}</div>'
        f'</details>'
    )


def render_html(doc: HandbookDoc, *, lang: str = "zh") -> str:
    from render_html import _BASE_CSS, _JS  # reuse styling/behaviour
    from pygments.formatters import HtmlFormatter

    ui = UI_STRINGS.get(lang, UI_STRINGS["zh"])
    units_by_stage = _load_units_by_stage(doc)

    parts = [f'<h1>{escape(ui["title"])}</h1>']
    if doc.overview_md:
        parts.append(f'<h2 id="overview">{escape(ui["overview"])}</h2>')
        parts.append(_md(doc.overview_md))
    # only top-level stages here; sub-stages nest inside their parent
    for s in doc.top_level():
        parts.append(_stage_html(doc, s, units_by_stage, ui))
    if doc.registers_md:
        parts.append(f'<h2 id="registers">{escape(ui["registers"])}</h2>')
        parts.append(_md(doc.registers_md))
    body_html = "\n".join(parts)

    toc_html = _toc_from_tree(doc, ui)
    pyg = HtmlFormatter(style="default").get_style_defs(".codehilite")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(ui['title'])}</title>
<style>
{_BASE_CSS}
{pyg}
details.stage > summary {{ font-size: 17px; }}
</style></head>
<body>
<div class="toolbar">
  <button id="expand-all">展开全部</button>
  <button id="collapse-all">折叠全部</button>
  <button id="theme-toggle">🌓</button>
</div>
<div class="layout">
{toc_html}
<main>
{body_html}
</main>
</div>
<script>
{_JS}
</script>
</body></html>
"""


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Render handbook.json tree → md / html")
    ap.add_argument("--json", required=True, help="path to handbook.json")
    ap.add_argument("--lang", default="zh", choices=["zh", "en"])
    args = ap.parse_args()
    doc = HandbookDoc.read(Path(args.json))
    base = Path(args.json).with_suffix("")
    (base.parent / f"{base.name}.md").write_text(render_md(doc, lang=args.lang), encoding="utf-8")
    (base.parent / f"{base.name}.html").write_text(render_html(doc, lang=args.lang), encoding="utf-8")
    print(f"md   → {base}.md\nhtml → {base}.html")


if __name__ == "__main__":
    main()
