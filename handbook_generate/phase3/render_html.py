# -*- coding: utf-8 -*-
"""Render handbook.md → handbook.html.

Self-contained: CSS + minimal JS inlined, no external assets. Includes:
  - Collapsible sidebar TOC (H2/H3 only — H4 is region sub-headings inside details)
  - Sticky TOC; click jumps + smooth scroll
  - Light/dark theme toggle (preference persisted to localStorage)
  - Expand-all / collapse-all controls for <details>
  - Syntax-highlighted Python code blocks via pygments inline stylesheet
  - md_in_html so markdown inside <details> renders
"""
from __future__ import annotations

import argparse
import re
from html import escape
from pathlib import Path

import markdown
from pygments.formatters import HtmlFormatter


# ─── CSS ─────────────────────────────────────────────────────────────────────


_BASE_CSS = """
:root {
  --bg: #fafafa;
  --bg-card: #ffffff;
  --bg-code: #f6f8fa;
  --bg-table-h: #f0f3f6;
  --fg: #1f2328;
  --fg-muted: #57606a;
  --fg-link: #0969da;
  --border: #d0d7de;
  --border-soft: #e4e8ee;
  --accent: #0969da;
  --callout-bg: #fff8e7;
  --callout-border: #d4a017;
}
[data-theme="dark"] {
  --bg: #0d1117;
  --bg-card: #161b22;
  --bg-code: #1a1f27;
  --bg-table-h: #21262d;
  --fg: #e6edf3;
  --fg-muted: #8b949e;
  --fg-link: #58a6ff;
  --border: #30363d;
  --border-soft: #21262d;
  --accent: #58a6ff;
  --callout-bg: #1e1a0c;
  --callout-border: #d4a017;
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
               "Microsoft YaHei", "Source Han Sans CN", "Noto Sans CJK SC",
               "Segoe UI", Roboto, sans-serif;
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.65;
  font-size: 15px;
}
.layout {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 0;
  max-width: 1400px;
  margin: 0 auto;
}
nav.toc {
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
  padding: 24px 16px 24px 24px;
  border-right: 1px solid var(--border-soft);
  background: var(--bg);
  font-size: 13px;
}
nav.toc h2 {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--fg-muted);
  margin: 12px 0 8px;
  font-weight: 600;
}
nav.toc ul {
  list-style: none;
  padding: 0;
  margin: 0 0 12px;
}
nav.toc li { margin: 2px 0; }
nav.toc a {
  color: var(--fg);
  text-decoration: none;
  display: block;
  padding: 3px 6px;
  border-radius: 4px;
  border-left: 2px solid transparent;
}
nav.toc a:hover { background: var(--bg-card); color: var(--accent); }
nav.toc a.level-3 { padding-left: 18px; color: var(--fg-muted); }
nav.toc a.active {
  background: var(--bg-card);
  color: var(--accent);
  border-left-color: var(--accent);
  font-weight: 600;
}

main {
  padding: 24px 40px 80px;
  max-width: 980px;
  min-width: 0;
}
h1 { font-size: 28px; margin: 0 0 12px; padding-bottom: 8px; border-bottom: 2px solid var(--border); }
h2 {
  font-size: 22px;
  margin: 40px 0 16px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border-soft);
  scroll-margin-top: 16px;
}
h3 {
  font-size: 18px;
  margin: 28px 0 12px;
  scroll-margin-top: 16px;
}
h4 { font-size: 16px; margin: 20px 0 10px; color: var(--fg); }
p { margin: 8px 0 12px; }
a { color: var(--fg-link); text-decoration: none; }
a:hover { text-decoration: underline; }

ul, ol { padding-left: 28px; margin: 8px 0 12px; }
li { margin: 4px 0; }

blockquote {
  margin: 12px 0;
  padding: 10px 16px;
  background: var(--callout-bg);
  border-left: 4px solid var(--callout-border);
  border-radius: 4px;
  color: var(--fg);
}
blockquote p { margin: 4px 0; }

code {
  font-family: "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 0.9em;
  background: var(--bg-code);
  padding: 1px 5px;
  border-radius: 3px;
  border: 1px solid var(--border-soft);
}
pre {
  background: var(--bg-code) !important;
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  padding: 14px 16px;
  overflow-x: auto;
  margin: 12px 0;
  font-size: 13px;
  line-height: 1.55;
}
pre code {
  background: none;
  padding: 0;
  border: 0;
  font-size: inherit;
}

table {
  border-collapse: collapse;
  margin: 12px 0;
  width: 100%;
  font-size: 14px;
}
th, td {
  border: 1px solid var(--border-soft);
  padding: 8px 12px;
  text-align: left;
  vertical-align: top;
}
th { background: var(--bg-table-h); font-weight: 600; }

details {
  margin: 16px 0;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-card);
  padding: 0;
  overflow: hidden;
}
details summary {
  cursor: pointer;
  padding: 12px 18px;
  font-size: 15px;
  font-weight: 500;
  list-style: none;
  user-select: none;
  background: var(--bg-card);
  border-bottom: 1px solid transparent;
  transition: background-color 0.1s;
}
details summary:hover { background: var(--bg-table-h); }
details summary::-webkit-details-marker { display: none; }
details summary::before {
  content: "▶";
  display: inline-block;
  margin-right: 8px;
  font-size: 0.75em;
  color: var(--fg-muted);
  transition: transform 0.15s;
}
details[open] summary::before { transform: rotate(90deg); }
details[open] summary {
  border-bottom-color: var(--border-soft);
  background: var(--bg-table-h);
}
details summary b { font-weight: 600; }
.details-body {
  padding: 16px 22px 8px;
}
/* The actual rendered content lives inside the details; pad it explicitly. */
details > *:not(summary) {
  margin-left: 22px;
  margin-right: 22px;
}
details > h3, details > h4 {
  margin-top: 18px;
}
details > *:first-of-type {
  margin-top: 16px;
}
details > *:last-of-type {
  margin-bottom: 16px;
}
details > pre { margin-left: 22px; margin-right: 22px; }

hr {
  border: 0;
  border-top: 1px solid var(--border-soft);
  margin: 24px 0;
}

/* Toolbar */
.toolbar {
  position: fixed;
  top: 12px;
  right: 12px;
  display: flex;
  gap: 6px;
  z-index: 1000;
}
.toolbar button {
  font-family: inherit;
  font-size: 12px;
  padding: 6px 12px;
  border: 1px solid var(--border);
  background: var(--bg-card);
  color: var(--fg);
  border-radius: 6px;
  cursor: pointer;
  transition: background-color 0.1s;
}
.toolbar button:hover { background: var(--bg-table-h); }

/* Mobile */
@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
  nav.toc {
    position: relative;
    height: auto;
    max-height: 60vh;
    border-right: 0;
    border-bottom: 1px solid var(--border-soft);
  }
  main { padding: 16px 20px 60px; }
}
"""


_JS = """
// TOC scroll-spy + theme toggle + expand/collapse all
(function() {
  const tocLinks = Array.from(document.querySelectorAll('nav.toc a'));
  const headings = tocLinks
    .map(a => ({ link: a, target: document.getElementById(a.getAttribute('href').slice(1)) }))
    .filter(x => x.target);

  function onScroll() {
    let active = null;
    const probeY = window.scrollY + 80;
    for (const h of headings) {
      if (h.target.offsetTop <= probeY) active = h;
      else break;
    }
    tocLinks.forEach(a => a.classList.remove('active'));
    if (active) active.link.classList.add('active');
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // Theme toggle
  const themeBtn = document.getElementById('theme-toggle');
  const stored = localStorage.getItem('handbook-theme');
  if (stored === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
  themeBtn.addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme');
    const next = cur === 'dark' ? 'light' : 'dark';
    if (next === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
    else document.documentElement.removeAttribute('data-theme');
    localStorage.setItem('handbook-theme', next);
  });

  // Expand/collapse all
  document.getElementById('expand-all').addEventListener('click', () => {
    document.querySelectorAll('details').forEach(d => d.open = true);
  });
  document.getElementById('collapse-all').addEventListener('click', () => {
    document.querySelectorAll('details').forEach(d => d.open = false);
  });
})();
"""


# ─── TOC extraction (from final HTML, by parsing heading tags) ────────────────


_HEADING_RE = re.compile(r'<h([23])\s+id="([^"]+)">(.*?)</h\1>', re.DOTALL)
_DETAILS_BLOCK_RE = re.compile(r'<details\b[^>]*>.*?</details>', re.DOTALL)


def build_toc(html: str) -> str:
    """Scan rendered HTML for H2/H3, emit a structured sidebar.

    Excludes headings inside <details> blocks — those are sub-section labels
    of a single function card (e.g. "总体结构", "Non-obvious 设计决策")
    and are not navigation targets since the parent details is folded by default.
    """
    # Strip details blocks before regexing so internal headings drop out.
    html_for_toc = _DETAILS_BLOCK_RE.sub("", html)
    items = []
    for m in _HEADING_RE.finditer(html_for_toc):
        level = int(m.group(1))
        hid = m.group(2)
        # Strip inner tags from heading text for the TOC entry
        text = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        items.append((level, hid, text))

    lines = ['<nav class="toc">', '<h2>目录</h2>']
    current_l2 = None
    in_l3 = False
    for level, hid, text in items:
        if level == 2:
            if in_l3:
                lines.append("</ul>")
                in_l3 = False
            lines.append(f'<a href="#{hid}" class="level-2">{escape(text)}</a>')
            current_l2 = hid
        else:  # level == 3
            if not in_l3:
                lines.append("<ul>")
                in_l3 = True
            lines.append(f'<li><a href="#{hid}" class="level-3">{escape(text)}</a></li>')
    if in_l3:
        lines.append("</ul>")
    lines.append("</nav>")
    return "\n".join(lines)


# ─── Pre-processing: ensure markdown-extra processes inside <details> ─────────


def _preprocess(md_text: str) -> str:
    """
    Python-Markdown's md_in_html extension requires `markdown="1"` on the parent
    tag. We add it to every <details> that doesn't already have it.
    """
    return re.sub(
        r'<details(\s+id="[^"]+")?>',
        lambda m: f'<details{m.group(1) or ""} markdown="1">',
        md_text,
    )


# ─── Main ─────────────────────────────────────────────────────────────────────


def render(md_path: Path, html_path: Path, title: str = "Terminus 2 Handbook"):
    md_text = md_path.read_text(encoding="utf-8")
    md_text = _preprocess(md_text)

    md = markdown.Markdown(
        extensions=[
            "extra",        # tables, fenced_code, attr_list, md_in_html, etc.
            "codehilite",   # pygments-driven highlighting
            "sane_lists",
            "smarty",
        ],
        extension_configs={
            "codehilite": {
                "css_class": "codehilite",
                "guess_lang": False,
                "linenums": False,
            },
        },
        output_format="html5",
    )
    body_html = md.convert(md_text)

    # Heading IDs: python-markdown's `extra` does not auto-id headings; we need
    # them for TOC anchors. Insert ids derived from header text.
    body_html = _inject_heading_ids(body_html)

    toc_html = build_toc(body_html)

    pygments_css = HtmlFormatter(style="default").get_style_defs(".codehilite")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
<style>
{_BASE_CSS}
{pygments_css}
</style>
</head>
<body>
<div class="toolbar">
  <button id="expand-all" title="展开全部 details">展开全部</button>
  <button id="collapse-all" title="折叠全部 details">折叠全部</button>
  <button id="theme-toggle" title="切换主题">🌓</button>
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
</body>
</html>
"""
    html_path.write_text(html, encoding="utf-8")


# ─── Heading id injection ─────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.strip().lower()
    # Replace common separators
    text = re.sub(r"[·:：、\s]+", "-", text)
    # Drop most punctuation but keep word chars and dashes (including CJK)
    text = re.sub(r"[^\w\-\.一-鿿]", "", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "section"


def _inject_heading_ids(html: str) -> str:
    """Add id="..." to <h1>/<h2>/<h3> tags lacking one."""
    used: set[str] = set()

    def repl(m: re.Match) -> str:
        level = m.group(1)
        inner = m.group(2)
        # Don't overwrite an existing id
        if 'id="' in m.group(0)[:20]:
            return m.group(0)
        base = _slugify(inner)
        slug = base
        i = 1
        while slug in used:
            i += 1
            slug = f"{base}-{i}"
        used.add(slug)
        return f'<h{level} id="{slug}">{inner}</h{level}>'

    return re.sub(r"<h([1-3])>(.*?)</h\1>", repl, html, flags=re.DOTALL)


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", default="output/handbook.md",
        help="Source markdown file (default: output/handbook.md)",
    )
    parser.add_argument(
        "--output", default="output/handbook.html",
        help="Destination HTML file (default: output/handbook.html)",
    )
    parser.add_argument("--title", default="Terminus 2 Handbook")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    in_path = (here.parent / args.input).resolve() if not Path(args.input).is_absolute() else Path(args.input)
    out_path = (here.parent / args.output).resolve() if not Path(args.output).is_absolute() else Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    render(in_path, out_path, title=args.title)
    print(f"HTML written: {out_path}  ({out_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
