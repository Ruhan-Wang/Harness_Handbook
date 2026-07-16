# -*- coding: utf-8 -*-
"""render_html.py — multi-page, progressively-disclosed HTML handbook (NO LLM).

Renders the Phase 3 data (the StageTree + rollup summaries + registers + deep
cards) into a self-contained static HTML site under `<out>/html/`:

  index.html      → redirects to overview.html
  overview.html   system overview + top-level stage cards + register entry
  register.html   the state-flow register table (rows link to stage pages)
  stage-<id>.html one page per stage (140), with progressive disclosure:
                    stage overview → sub-stage cards → files (<details>)
                      → per-file description + function list
                        → per-function purpose/data_flow/relations (<details>)

Progressive disclosure: each level shows only a summary + the entry to the next
level. Files and functions are collapsed `<details>` — the default view is always
clean; you drill down by clicking. Pages are separate files (not one giant page)
because the system has ~34k functions; cross-page links are relative so the site
works over file:// with no server.

Everything is inlined (CSS + JS), no external assets. Reuses the `markdown` lib
for rollup prose and `pygments` for signature highlighting; both are optional —
plain fallbacks keep it working if a lib is missing.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Optional deps — degrade gracefully.
try:
    import markdown as _markdown
except Exception:  # noqa: BLE001
    _markdown = None
try:
    from pygments import highlight as _pyg_highlight
    from pygments.formatters import HtmlFormatter as _PygFormatter
    from pygments.lexers import RustLexer as _RustLexer
except Exception:  # noqa: BLE001
    _pyg_highlight = None


# ─── markdown / code helpers ─────────────────────────────────────────────────


def _md(text: str) -> str:
    """Render a markdown string (rollup prose) to HTML, or escaped <p> fallback."""
    text = (text or "").strip()
    if not text:
        return ""
    if _markdown is not None:
        try:
            return _markdown.markdown(text, extensions=["fenced_code", "tables"])
        except Exception:  # noqa: BLE001
            pass
    return "<p>" + html.escape(text).replace("\n\n", "</p><p>") + "</p>"


def _sig(signature: str) -> str:
    """Highlight a Rust signature, or plain escaped <code>."""
    signature = (signature or "").strip()
    if not signature:
        return ""
    if _pyg_highlight is not None:
        try:
            return _pyg_highlight(signature, _RustLexer(),
                                  _PygFormatter(nowrap=True, noclasses=True))
        except Exception:  # noqa: BLE001
            pass
    return html.escape(signature)


def _esc(s: str) -> str:
    return html.escape(s or "")


def _first_sentence(text: str, cap: int = 160) -> str:
    t = (text or "").strip().replace("\n", " ")
    for sep in (". ", "。", "! ", "; "):
        i = t.find(sep)
        if 0 < i <= cap:
            return t[:i + 1]
    return (t[:cap] + "…") if len(t) > cap else t


# ─── CSS / JS shell (inlined, self-contained) ────────────────────────────────


_CSS = """
:root{--bg:#fafafa;--card:#fff;--code:#f6f8fa;--th:#f0f3f6;--fg:#1f2328;
--muted:#57606a;--link:#0969da;--border:#d0d7de;--soft:#e4e8ee;--accent:#0969da;}
:root[data-theme=dark]{--bg:#0d1117;--card:#161b22;--code:#161b22;--th:#21262d;
--fg:#e6edf3;--muted:#8b949e;--link:#58a6ff;--border:#30363d;--soft:#21262d;--accent:#58a6ff;}
*{box-sizing:border-box}
body{margin:0;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
color:var(--fg);background:var(--bg);}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.layout{display:flex;min-height:100vh}
.sidebar{width:300px;flex:0 0 300px;border-right:1px solid var(--border);background:var(--card);
position:sticky;top:0;height:100vh;overflow:auto;padding:14px 10px;font-size:13px}
.main{flex:1;min-width:0;padding:24px 36px;max-width:1000px}
.sidebar .toc-title{font-weight:700;font-size:12px;text-transform:uppercase;color:var(--muted);
letter-spacing:.05em;margin:8px 6px}
.toc ul{list-style:none;margin:0;padding-left:12px}
.toc>ul{padding-left:0}
.toc li{margin:1px 0}
.toc a{display:block;padding:3px 6px;border-radius:5px;color:var(--fg)}
.toc a:hover{background:var(--th);text-decoration:none}
.toc a.cur{background:var(--accent);color:#fff;font-weight:600}
.toc .cc{color:var(--muted);font-size:11px}
.toc .num{color:var(--muted);font-variant-numeric:tabular-nums;margin-right:3px}
.crumb{font-size:13px;color:var(--muted);margin-bottom:10px}
.crumb a{color:var(--muted)}
h1{font-size:26px;margin:.2em 0 .4em;border-bottom:1px solid var(--soft);padding-bottom:.3em}
h2{font-size:19px;margin:1.4em 0 .5em}
.bar{display:flex;gap:8px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.btn{font-size:12px;padding:4px 10px;border:1px solid var(--border);border-radius:6px;
background:var(--card);color:var(--fg);cursor:pointer}
.btn:hover{background:var(--th)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin:12px 0}
.card{border:1px solid var(--border);border-radius:8px;padding:14px;background:var(--card)}
.card h3{margin:0 0 6px;font-size:15px}
.card .meta{font-size:12px;color:var(--muted);margin-bottom:6px}
.card p{margin:0;font-size:13px;color:var(--fg)}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;
background:var(--th);color:var(--muted);margin-left:6px}
details{border:1px solid var(--soft);border-radius:7px;margin:7px 0;background:var(--card)}
details>summary{cursor:pointer;padding:8px 12px;list-style:none;display:flex;align-items:center;gap:8px}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"▶";font-size:10px;color:var(--muted);transition:transform .15s}
details[open]>summary::before{transform:rotate(90deg)}
details>summary:hover{background:var(--th);border-radius:7px}
.det-body{padding:2px 14px 12px 30px}
.fn{margin-left:14px}
.fn>summary code{font-size:13px}
.field{margin:6px 0}.field b{color:var(--accent)}
.callfacts{font-size:12px;color:var(--muted);font-style:italic}
pre,code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:13px}
pre{background:var(--code);padding:10px 12px;border-radius:7px;overflow:auto;border:1px solid var(--soft)}
pre code{background:none;padding:0}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px}
th,td{border:1px solid var(--border);padding:7px 10px;text-align:left;vertical-align:top}
th{background:var(--th)}
.role{font-size:11px;color:var(--muted)}
.regsec{margin-top:24px;border-top:1px solid var(--soft);padding-top:12px}
.regsec li{margin:4px 0}
"""

# JS: theme toggle (persisted), expand/collapse all <details> on the page,
# TOC current-page highlight is baked server-side (class="cur").
_JS = """
(function(){
 var root=document.documentElement;
 var saved=localStorage.getItem('hb-theme');
 if(saved)root.setAttribute('data-theme',saved);
 window.hbTheme=function(){var d=root.getAttribute('data-theme')==='dark'?'':'dark';
   if(d)root.setAttribute('data-theme',d);else root.removeAttribute('data-theme');
   localStorage.setItem('hb-theme',d);};
 window.hbAll=function(open){document.querySelectorAll('.main details').forEach(function(d){d.open=open;});};
})();
"""


# Bilingual HTML chrome labels (LLM prose is already in the chosen language;
# these are the fixed buttons / headings).
_HUI = {
    "en": {"htmllang": "en", "theme": "🌓 Theme", "expand": "Expand all",
           "collapse": "Collapse all", "sysov": "System Overview",
           "stages": "Stages", "substages": "Sub-stages", "files": "Files in this stage",
           "functions": "Functions", "regs_touched": "📊 State Registers Touched",
           "reg_table_h": "🔄 State Flow — Registers", "no_regs": "No state registers extracted.",
           "col_reg": "Register", "col_sem": "Semantics", "col_stages": "Stages touched",
           "regs_below": "state-flow registers below.", "stage_detail": "Stage detail",
           "overview_nav": "🗺️ Overview", "regs_nav": "🔄 Registers"},
    "zh": {"htmllang": "zh", "theme": "🌓 主题", "expand": "全部展开",
           "collapse": "全部折叠", "sysov": "系统总览",
           "stages": "阶段", "substages": "子阶段", "files": "本阶段的文件",
           "functions": "函数", "regs_touched": "📊 本阶段涉及的状态",
           "reg_table_h": "🔄 状态流动 — 寄存器", "no_regs": "未提取到状态寄存器。",
           "col_reg": "状态寄存器", "col_sem": "语义", "col_stages": "涉及阶段",
           "regs_below": "状态流动寄存器见下方。", "stage_detail": "阶段详情",
           "overview_nav": "🗺️ 总览", "regs_nav": "🔄 寄存器"},
}


def _ui(lang: str) -> dict:
    return _HUI.get(lang, _HUI["en"])


def _page(title: str, toc_html: str, crumb_html: str, body_html: str,
          lang: str = "en") -> str:
    """Wrap a page body in the shared shell (sidebar TOC + top bar + main)."""
    u = _ui(lang)
    return f"""<!doctype html><html lang="{u['htmllang']}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{_CSS}</style></head><body>
<div class="layout">
<nav class="sidebar"><div class="toc-title">Handbook</div>{toc_html}</nav>
<main class="main">
<div class="bar">
<button class="btn" onclick="hbTheme()">{u['theme']}</button>
<button class="btn" onclick="hbAll(true)">{u['expand']}</button>
<button class="btn" onclick="hbAll(false)">{u['collapse']}</button>
</div>
{crumb_html}
{body_html}
</main></div>
<script>{_JS}</script></body></html>"""


# ─── TOC (tree, current-page highlighted) ────────────────────────────────────


def _toc_html(tree, cur: str | None, lang: str = "en") -> str:
    """Sidebar tree: Overview / Registers + the full stage tree, current page
    marked. Built once per page (cur differs) — cheap."""
    u = _ui(lang)
    parts = ['<div class="toc"><ul>']
    ov_cur = " cur" if cur == "overview" else ""
    parts.append(f'<li><a class="{ov_cur.strip()}" href="overview.html">{u["overview_nav"]}</a></li>')
    reg_cur = " cur" if cur == "register" else ""
    parts.append(f'<li><a class="{reg_cur.strip()}" href="register.html">{u["regs_nav"]}</a></li>')
    parts.append(f'</ul><div class="toc-title">{u["stages"]}</div><ul>')

    def walk(sid: str) -> None:
        title = _esc(tree.title(sid))
        cls = "cur" if sid == cur else ""
        cc = '<span class="cc"> · cc</span>' if tree.is_crosscut(sid) else ""
        parts.append(f'<li><a class="{cls}" href="{sid}.html">{title}</a>{cc}')
        kids = tree.children(sid)
        if kids:
            parts.append("<ul>")
            for c in kids:
                walk(c)
            parts.append("</ul>")
        parts.append("</li>")

    for top in tree.top_level:
        walk(top)
    parts.append("</ul></div>")
    return "".join(parts)


def _crumb(tree, sid: str, lang: str = "en") -> str:
    """Breadcrumb from system root down to this stage."""
    sys_label = "系统" if lang == "zh" else "System"
    chain = []
    cur = sid
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        cur = (tree.stages_by_id.get(cur, {}) or {}).get("parent")
    chain.reverse()
    bits = [f'<a href="overview.html">{sys_label}</a>']
    for i, s in enumerate(chain):
        if i == len(chain) - 1:
            bits.append(_esc(tree.title(s)))
        else:
            bits.append(f'<a href="{s}.html">{_esc(tree.title(s))}</a>')
    return '<div class="crumb">' + " / ".join(bits) + "</div>"


# ─── Function / file rendering (progressive disclosure) ──────────────────────


def _render_function_html(fn: dict, lang: str = "en") -> str:
    zh = lang == "zh"
    qual = _esc(fn.get("qualname") or fn.get("name") or "(anonymous)")
    lr = fn.get("line_range") or [None, None]
    line_word = "行" if zh else "lines"
    line_tag = f" <span class='role'>{line_word} {lr[0]}–{lr[1]}</span>" if lr and lr[0] else ""
    sig = fn.get("signature") or ""
    parts = [f'<details class="fn"><summary><code>{qual}</code>{line_tag}</summary>',
             '<div class="det-body">']
    if sig:
        parts.append(f"<pre><code>{_sig(sig)}</code></pre>")
    labels = ((("作用" if zh else "Purpose"), "purpose"),
              (("数据流" if zh else "Data flow"), "data_flow"),
              (("调用关系" if zh else "Call relations"), "relations"))
    for label, key in labels:
        val = (fn.get(key) or "").strip()
        if val:
            parts.append(f'<div class="field"><b>{label}:</b> {_esc(val)}</div>')
    facts = []
    if fn.get("n_calls"):
        facts.append((f"调用 {fn['n_calls']} 个内部函数" if zh
                      else f"calls {fn['n_calls']} internal fn"))
    if fn.get("n_called_by"):
        facts.append((f"被 {fn['n_called_by']} 处调用" if zh
                      else f"called by {fn['n_called_by']}"))
    if fn.get("n_ext_calls"):
        facts.append((f"外部调用 {fn['n_ext_calls']} 个" if zh
                      else f"{fn['n_ext_calls']} external calls"))
    if facts:
        cg = "调用图" if zh else "Call graph"
        joiner = "；" if zh else "; "
        parts.append(f'<div class="callfacts">{cg}: {_esc(joiner.join(facts))}.</div>')
    parts.append("</div></details>")
    return "".join(parts)


def _render_file_html(rel: str, card: dict | None, lang: str = "en") -> str:
    card = card or {}
    role = _esc(card.get("role") or "?")
    lifecycle = card.get("lifecycle") or ""
    badge = f'<span class="badge">{role}</span>'
    if lifecycle and lifecycle != "none":
        badge += f'<span class="badge">{_esc(lifecycle)}</span>'
    parts = [f'<details><summary><code>{_esc(rel)}</code>{badge}</summary>',
             '<div class="det-body">']
    desc = (card.get("description") or "").strip()
    purpose = (card.get("purpose") or "").strip()
    if desc:
        parts.append(_md(desc))
    elif purpose:
        parts.append(_md(purpose))
    else:
        parts.append("<p><em>" + ("该文件暂无描述。" if lang == "zh"
                                   else "No description yet.") + "</em></p>")
    funcs = card.get("functions") or []
    if funcs:
        parts.append(f"<h2 style='font-size:15px'>{_ui(lang)['functions']}</h2>")
        for fn in funcs:
            parts.append(_render_function_html(fn, lang))
    parts.append("</div></details>")
    return "".join(parts)


# ─── Page renderers ──────────────────────────────────────────────────────────


def _stage_card(tree, sid: str, summaries: dict, lang: str = "en") -> str:
    """A clickable card for one (sub-)stage: title, first sentence, file count."""
    title = _esc(tree.title(sid))
    cc = '<span class="badge">cross-cutting</span>' if tree.is_crosscut(sid) else ""
    nfiles = tree.subtree_file_count(sid)
    fn = "个文件" if lang == "zh" else "files"
    blurb = _esc(_first_sentence(summaries.get(sid, "")))
    return (f'<div class="card"><h3><a href="{sid}.html">{title}</a>{cc}</h3>'
            f'<div class="meta"><code>{sid}</code> · {nfiles} {fn}</div>'
            f'<p>{blurb}</p></div>')


def render_overview_html(tree, summaries: dict, system_overview: str,
                         has_registers: bool, lang: str = "en") -> str:
    u = _ui(lang)
    body = [f'<h1>{u["sysov"]}</h1>', _md(system_overview)]
    if has_registers:
        body.append(f'<p class="regsec">🔄 <a href="register.html">{u["regs_nav"]}</a></p>')
    body.append(f'<h2>{u["stages"]}</h2><div class="cards">')
    for top in tree.top_level:
        body.append(_stage_card(tree, top, summaries, lang))
    body.append("</div>")
    return _page(f"System Handbook — {u['sysov']}", _toc_html(tree, "overview", lang),
                 "", "".join(body), lang)


def render_stage_html(tree, sid: str, summaries: dict, registers: list,
                      lang: str = "en") -> str:
    u = _ui(lang)
    fn = "个文件" if lang == "zh" else "files"
    title = _esc(tree.title(sid))
    cc = '<span class="badge">cross-cutting</span>' if tree.is_crosscut(sid) else ""
    body = [f'<h1>{title} {cc}</h1>', f'<p class="role"><code>{sid}</code> · '
            f'{tree.subtree_file_count(sid)} {fn}</p>',
            _md(summaries.get(sid, ""))]

    kids = tree.children(sid)
    if kids:
        body.append(f'<h2>{u["substages"]}</h2><div class="cards">')
        for c in kids:
            body.append(_stage_card(tree, c, summaries, lang))
        body.append("</div>")

    direct = tree.direct_files(sid)
    if direct:
        body.append(f"<h2>{u['files']}</h2>")
        groups = tree.groups(sid)
        placed = set()
        if groups:
            for g in groups:
                gtitle = _esc((g.get("title") or "Files").strip())
                gsum = _esc((g.get("summary") or "").strip())
                gfiles = [f.get("file") if isinstance(f, dict) else f
                          for f in (g.get("files") or [])]
                gfiles = [f for f in gfiles if f]
                if not gfiles:
                    continue
                body.append(f"<h3 style='font-size:15px'>{gtitle}</h3>")
                if gsum:
                    body.append(f'<p class="role">{gsum}</p>')
                for rel in gfiles:
                    placed.add(rel)
                    body.append(_render_file_html(rel, tree.cards.get(rel), lang))
            for rel in direct:
                if rel not in placed:
                    body.append(_render_file_html(rel, tree.cards.get(rel), lang))
        else:
            for rel in direct:
                body.append(_render_file_html(rel, tree.cards.get(rel), lang))

    hits = [r for r in registers if sid in r.get("stages", [])]
    if hits:
        body.append(f'<div class="regsec"><h2>{u["regs_touched"]}</h2><ul>')
        for r in hits:
            body.append(f'<li><code>{_esc(r["id"])}</code> — {_esc(r["semantics"])}</li>')
        body.append("</ul></div>")

    return _page(f"{tree.title(sid)} — Handbook", _toc_html(tree, sid, lang),
                 _crumb(tree, sid, lang), "".join(body), lang)


def render_register_html(tree, registers: list, lang: str = "en") -> str:
    u = _ui(lang)
    body = [f'<h1>{u["reg_table_h"]}</h1>']
    if not registers:
        body.append(f"<p><em>{u['no_regs']}</em></p>")
    else:
        body.append(f"<table><thead><tr><th>{u['col_reg']}</th><th>{u['col_sem']}</th>"
                    f"<th>{u['col_stages']}</th></tr></thead><tbody>")
        for r in registers:
            links = [f'<a href="{s}.html">{_esc(tree.title(s))}</a>'
                     for s in r.get("stages", [])]
            cell = ", ".join(links) if links else "—"
            body.append(f'<tr><td><code>{_esc(r["id"])}</code></td>'
                        f'<td>{_esc(r["semantics"])}</td><td>{cell}</td></tr>')
        body.append("</tbody></table>")
    return _page("Registers — Handbook", _toc_html(tree, "register", lang),
                 "", "".join(body), lang)


# ─── Driver ──────────────────────────────────────────────────────────────────


def render_site(tree, summaries: dict, system_overview: str,
                registers: list, out_dir, lang: str = "en") -> dict:
    """Render the whole HTML site under out_dir/html/. Returns a stats dict.

    `tree` is the StageTree, `summaries` the per-stage rollup map, `registers`
    the extracted register list — all passed in from build_handbook (no reload,
    no LLM)."""
    html_dir = Path(out_dir) / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    has_reg = bool(registers)
    open_label = "打开手册总览" if lang == "zh" else "Open the handbook overview"

    (html_dir / "overview.html").write_text(
        render_overview_html(tree, summaries, system_overview, has_reg, lang),
        encoding="utf-8")
    (html_dir / "register.html").write_text(
        render_register_html(tree, registers, lang), encoding="utf-8")
    (html_dir / "index.html").write_text(
        '<!doctype html><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0;url=overview.html">'
        f'<a href="overview.html">{open_label}</a>',
        encoding="utf-8")

    n_pages = 0
    for sid in tree.order:
        # Only stages that have content (children or files) get a page —
        # matches the markdown build's has-content rule.
        if not (tree.children(sid) or tree.buckets.get(sid)):
            continue
        (html_dir / f"{sid}.html").write_text(
            render_stage_html(tree, sid, summaries, registers, lang), encoding="utf-8")
        n_pages += 1

    logger.info("render_html: wrote %d stage pages + overview/register/index → %s",
                n_pages, html_dir)
    return {"n_stage_pages": n_pages, "html_dir": str(html_dir)}


# ─── Single-page (one self-contained HTML) ───────────────────────────────────
#
# Everything in ONE file. Cross-references become in-page #anchors. Each stage is
# itself wrapped in a top-level <details> (collapsed by default) so the browser
# does not lay out all ~34k function blocks at once — only an expanded stage's
# DOM is visible. This keeps a single multi-megabyte page openable.


def _number_map(tree) -> dict:
    """Hierarchical section numbers for every stage, by tree position + skeleton
    order: top-level → "1","2",…; their children → "1.1","1.2",…; recursively.
    Used in single-page mode so the TOC / titles / cards read as a numbered
    outline (1 → 1.1 → 1.1.1)."""
    numbers: dict[str, str] = {}

    def walk(sid: str, prefix: str) -> None:
        numbers[sid] = prefix
        for i, c in enumerate(tree.children(sid), 1):
            walk(c, f"{prefix}.{i}")

    for i, top in enumerate(tree.top_level, 1):
        walk(top, str(i))
    return numbers


def _anchor_toc_html(tree, lang: str = "en", numbers: dict | None = None) -> str:
    """Sidebar tree whose links are in-page #anchors (single-page mode)."""
    u = _ui(lang)
    nums = numbers or {}
    parts = ['<div class="toc"><ul>',
             f'<li><a href="#overview">{u["overview_nav"]}</a></li>',
             f'<li><a href="#registers">{u["regs_nav"]}</a></li>',
             f'</ul><div class="toc-title">{u["stages"]}</div><ul>']

    def walk(sid: str) -> None:
        num = nums.get(sid, "")
        prefix = f'<span class="num">{num}</span> ' if num else ""
        title = _esc(tree.title(sid))
        cc = '<span class="cc"> · cc</span>' if tree.is_crosscut(sid) else ""
        parts.append(f'<li><a href="#{sid}">{prefix}{title}</a>{cc}')
        kids = tree.children(sid)
        if kids:
            parts.append("<ul>")
            for c in kids:
                walk(c)
            parts.append("</ul>")
        parts.append("</li>")

    for top in tree.top_level:
        walk(top)
    parts.append("</ul></div>")
    return "".join(parts)


def _stage_card_anchor(tree, sid: str, summaries: dict, lang: str = "en",
                       numbers: dict | None = None) -> str:
    """Stage card whose link is an in-page #anchor."""
    num = (numbers or {}).get(sid, "")
    prefix = f"{num} " if num else ""
    title = _esc(tree.title(sid))
    cc = '<span class="badge">cross-cutting</span>' if tree.is_crosscut(sid) else ""
    nfiles = tree.subtree_file_count(sid)
    fn = "个文件" if lang == "zh" else "files"
    blurb = _esc(_first_sentence(summaries.get(sid, "")))
    return (f'<div class="card"><h3><a href="#{sid}">{prefix}{title}</a>{cc}</h3>'
            f'<div class="meta"><code>{sid}</code> · {nfiles} {fn}</div>'
            f'<p>{blurb}</p></div>')


def _stage_section_single(tree, sid: str, summaries: dict, registers: list,
                          lang: str = "en", numbers: dict | None = None) -> str:
    """One stage as a collapsed <details> section (single-page mode). The whole
    stage body (sub-stage cards + file/function details) lives inside, so it is
    not laid out until the user expands this stage."""
    u = _ui(lang)
    nums = numbers or {}
    num = nums.get(sid, "")
    num_prefix = f"{num} " if num else ""
    fn = "个文件" if lang == "zh" else "files"
    title = _esc(tree.title(sid))
    cc = '<span class="badge">cross-cutting</span>' if tree.is_crosscut(sid) else ""

    inner = [_md(summaries.get(sid, ""))]
    kids = tree.children(sid)
    if kids:
        inner.append(f'<h3 style="font-size:15px">{u["substages"]}</h3><div class="cards">')
        for c in kids:
            inner.append(_stage_card_anchor(tree, c, summaries, lang, nums))
        inner.append("</div>")

    direct = tree.direct_files(sid)
    if direct:
        inner.append(f'<h3 style="font-size:15px">{u["files"]}</h3>')
        groups = tree.groups(sid)
        placed = set()
        if groups:
            for g in groups:
                gtitle = _esc((g.get("title") or "Files").strip())
                gfiles = [f.get("file") if isinstance(f, dict) else f
                          for f in (g.get("files") or [])]
                gfiles = [f for f in gfiles if f]
                if not gfiles:
                    continue
                inner.append(f'<p class="role"><b>{gtitle}</b></p>')
                for rel in gfiles:
                    placed.add(rel)
                    inner.append(_render_file_html(rel, tree.cards.get(rel), lang))
            for rel in direct:
                if rel not in placed:
                    inner.append(_render_file_html(rel, tree.cards.get(rel), lang))
        else:
            for rel in direct:
                inner.append(_render_file_html(rel, tree.cards.get(rel), lang))

    hits = [r for r in registers if sid in r.get("stages", [])]
    if hits:
        inner.append(f'<div class="regsec"><b>{u["regs_touched"]}</b><ul>')
        for r in hits:
            inner.append(f'<li><code>{_esc(r["id"])}</code> — {_esc(r["semantics"])}</li>')
        inner.append("</ul></div>")

    # The stage section: an anchor target + a collapsed <details>. The summary
    # shows title + id + file count so the collapsed list still reads as a TOC.
    nfiles = tree.subtree_file_count(sid)
    return (f'<details id="{sid}" class="stagesec">'
            f'<summary><b>{num_prefix}{title}</b> {cc} '
            f'<span class="role">{sid} · {nfiles} {fn}</span></summary>'
            f'<div class="det-body">{"".join(inner)}</div></details>')


def render_single_page(tree, summaries: dict, system_overview: str,
                       registers: list, out_dir, filename: str = "handbook.html",
                       lang: str = "en") -> dict:
    """Render the ENTIRE handbook as one self-contained HTML file.

    Each stage is a collapsed <details>, so the browser only lays out a stage's
    ~functions when it is expanded — that is what keeps a single page holding
    34k functions openable. Returns a stats dict (path + byte size)."""
    u = _ui(lang)
    numbers = _number_map(tree)   # hierarchical section numbers (1, 1.1, …)
    body = [f'<h1 id="overview">{u["sysov"]}</h1>', _md(system_overview)]
    if registers:
        body.append(f'<p class="regsec">🔄 <a href="#registers">{u["regs_below"]}</a></p>')
    body.append(f'<h2>{u["stages"]}</h2><div class="cards">')
    for top in tree.top_level:
        body.append(_stage_card_anchor(tree, top, summaries, lang, numbers))
    body.append("</div>")

    # All stages, in skeleton order, each a collapsed section.
    body.append(f'<h2>{u["stage_detail"]}</h2>')
    n_pages = 0
    for sid in tree.order:
        if not (tree.children(sid) or tree.buckets.get(sid)):
            continue
        body.append(_stage_section_single(tree, sid, summaries, registers, lang, numbers))
        n_pages += 1

    # Registers table at the end.
    body.append(f'<h2 id="registers">{u["reg_table_h"]}</h2>')
    if not registers:
        body.append(f"<p><em>{u['no_regs']}</em></p>")
    else:
        body.append(f"<table><thead><tr><th>{u['col_reg']}</th><th>{u['col_sem']}</th>"
                    f"<th>{u['col_stages']}</th></tr></thead><tbody>")
        for r in registers:
            links = [f'<a href="#{s}">{_esc(tree.title(s))}</a>'
                     for s in r.get("stages", [])]
            cell = ", ".join(links) if links else "—"
            body.append(f'<tr><td><code>{_esc(r["id"])}</code></td>'
                        f'<td>{_esc(r["semantics"])}</td><td>{cell}</td></tr>')
        body.append("</tbody></table>")

    page = _page("System Handbook", _anchor_toc_html(tree, lang, numbers), "", "".join(body), lang)
    out_path = Path(out_dir) / filename
    out_path.write_text(page, encoding="utf-8")
    size = out_path.stat().st_size
    logger.info("render_single_page: %d stages → %s (%.1f MB)",
                n_pages, out_path, size / 1e6)
    return {"n_stages": n_pages, "path": str(out_path), "bytes": size}
