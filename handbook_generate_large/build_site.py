#!/usr/bin/env python3
"""Build a static, progressively-disclosed HTML site from the generated
handbook markdown under work/codex/handbook (EN) and work/codex_zh/handbook (ZH).

Output: handbook_generate_large/site/
  index.html          bilingual landing page with collapsible stage tree
  en/stage-*.html     one page per stage (+ register.html)
  zh/stage-*.html
  assets/style.css, assets/app.js
"""

import html as html_mod
import os
import re
import sys
from pathlib import Path

ROOT = Path("/Users/tencentintern/Desktop/Harness_Handbook/handbook_generate_large")


def _dir_from_env(env_key: str, default_rel: str) -> Path:
    """A path overridable via env. Absolute env values are used as-is; relative
    ones resolve against ROOT (so `work/codex_plain/handbook` works)."""
    val = os.environ.get(env_key)
    if not val:
        return ROOT / default_rel
    p = Path(val)
    return p if p.is_absolute() else ROOT / val


# Output dir + per-language handbook sources are env-overridable so the SAME
# builder can render either the technical handbook (default) or the plain-language
# one (point HANDBOOK_SITE_EN_SRC/ZH_SRC at work/codex_plain / work/codex_zh_plain
# and HANDBOOK_SITE_OUT at a fresh dir).
SITE = _dir_from_env("HANDBOOK_SITE_OUT", "site")
CODE_SRC = ROOT.parent / "codex" / "codex-rs"  # source root the handbook paths resolve against

# Set of repo-relative source paths documented by either handbook; filled in main().
FILE_SET = set()

LANGS = {
    "en": {
        "src": _dir_from_env("HANDBOOK_SITE_EN_SRC", "work/codex/handbook"),
        "html_lang": "en",
        "name": "English",
        "files_word": "files",
        "labels": {
            "files_in_stage": {"Files in this stage"},
            "substages": {"Sub-stages"},
            "handbook": "Codex System Handbook",
            "index": "Stage index",
            "registers": "State-flow registers",
            "registers_short": "State registers touched",
            "fn_details": "Function details",
            "expand": "Expand all",
            "collapse": "Collapse all",
            "prev": "Previous",
            "next": "Next",
            "back": "Index",
            "stages": "stages",
            "files": "files documented",
            "filter": "Filter stages…",
            "overview": "System overview",
            "in_group": "files",
            "lang_switch": "中文版",
            "functions": "functions",
            "view_source": "source ↗",
            "back_home": "Home",
            "fnindex": "Function index",
            "fnindex_note": "Search every documented function by name. Without a query, the list below shows names defined in more than one file.",
            "filter_fn": "Search all functions…",
            "candidates": "definitions",
        },
    },
    "zh": {
        "src": _dir_from_env("HANDBOOK_SITE_ZH_SRC", "work/codex_zh/handbook"),
        "html_lang": "zh-CN",
        "name": "中文",
        "files_word": "个文件",
        "labels": {
            "files_in_stage": {"本阶段的文件"},
            "substages": {"子阶段"},
            "handbook": "Codex 系统手册",
            "index": "阶段索引",
            "registers": "状态流寄存器",
            "registers_short": "本阶段涉及的状态",
            "fn_details": "函数细节",
            "expand": "全部展开",
            "collapse": "全部折叠",
            "prev": "上一阶段",
            "next": "下一阶段",
            "back": "索引",
            "stages": "个阶段",
            "files": "个文件已成文",
            "filter": "筛选阶段…",
            "overview": "系统总览",
            "in_group": "个文件",
            "lang_switch": "English",
            "functions": "个函数",
            "view_source": "源码 ↗",
            "back_home": "返回主页",
            "fnindex": "函数索引",
            "fnindex_note": "可搜索手册收录的全部函数;未输入时,下方列出在多个文件中重名的函数。",
            "filter_fn": "搜索全部函数…",
            "candidates": "处定义",
        },
    },
}

# --------------------------------------------------------------------------
# inline / block markdown rendering (tuned to this generator's output)
# --------------------------------------------------------------------------

def esc(t: str) -> str:
    return html_mod.escape(t, quote=False)


# plain-text source paths in prose, e.g. "process-hardening/src/lib.rs runs first"
PATH_RE = re.compile(r"(?<![\w/.])((?:[\w.-]+/)+[\w.-]+\.\w{1,5})")

# pictographic emoji, stripped from all prose (keeps ★, arrows, and other
# meaningful symbols; fenced code blocks never pass through render_inline)
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u2604\u2606-\u27BF\uFE0F\u200D\u20E3]")


def strip_emoji(text: str) -> str:
    text = EMOJI_RE.sub("", text)
    return re.sub(r"  +", " ", text)


def render_inline(text: str, ctx: dict) -> str:
    """Escape + inline markdown. ctx: {link(url)->href, register_href, code_prefix?}"""
    text = strip_emoji(text)
    codes, mdlinks = [], []

    def stash_code(m):
        codes.append(m.group(1))
        return f"\x00C{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash_code, text)

    def stash_link(m):
        mdlinks.append((m.group(1), m.group(2)))
        return f"\x00A{len(mdlinks) - 1}\x00"

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", stash_link, text)
    text = esc(text)

    if "code_prefix" in ctx:
        def path_link(m):
            p = m.group(1)
            if p in FILE_SET:
                return f'<a class="code-link" href="{ctx["code_prefix"]}{p}.html"><code>{p}</code></a>'
            return p

        text = PATH_RE.sub(path_link, text)

    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)

    def sub_link(m):
        label, url = mdlinks[int(m.group(1))]
        # Model prose sometimes explains Markdown-like syntax with examples such
        # as [$tool](some/path). If the URL is just a placeholder, rendering it as
        # a real link creates broken anchors in the handbook. Keep genuine
        # handbook/external links clickable, but leave placeholder links as text.
        if (not re.match(r"^(https?://|#|/|\.\.?/)", url)
                and not url.endswith((".md", ".html"))):
            return f"{esc(label)}({esc(url)})"
        href = ctx["link"](url)
        extra = ' class="ext" target="_blank" rel="noopener"' if href.startswith("http") else ""
        return f'<a href="{href}"{extra}>{esc(label)}</a>'

    text = re.sub("\x00A(\\d+)\x00", sub_link, text)

    def restore_code(m):
        c = codes[int(m.group(1))]
        e = esc(c)
        if re.fullmatch(r"reg-[a-z0-9-]+", c):
            return f'<a class="reg" href="{ctx["register_href"]}#{c}"><code>{e}</code></a>'
        if c in FILE_SET and "code_prefix" in ctx:
            return f'<a class="code-link" href="{ctx["code_prefix"]}{c}.html" title="{e}"><code>{e}</code></a>'
        return f"<code>{e}</code>"

    return re.sub("\x00C(\\d+)\x00", restore_code, text)


def render_blocks(lines, ctx, heading_shift=0, register_anchor_col=False):
    """Generic block renderer: paragraphs, fenced code, lists, tables, hr, headings."""
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            j = i + 1
            buf = []
            while j < n and not lines[j].startswith("```"):
                buf.append(lines[j])
                j += 1
            out.append(f'<pre><code>{esc(chr(10).join(buf))}</code></pre>')
            i = j + 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lvl = min(6, len(m.group(1)) + heading_shift)
            out.append(f"<h{lvl}>{render_inline(m.group(2), ctx)}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^-{3,}\s*$", line):
            out.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith("|"):
            rows = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            out.append(render_table(rows, ctx, register_anchor_col))
            continue
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                item = re.sub(r"^\s*[-*]\s+", "", lines[i])
                i += 1
                while i < n and lines[i].strip() and not re.match(r"^\s*[-*]\s+", lines[i]) and not lines[i].startswith("#"):
                    item += " " + lines[i].strip()
                    i += 1
                items.append(f"<li>{render_inline(item, ctx)}</li>")
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        # paragraph: gather until blank / structural line
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#{1,6}\s|```|\s*[-*]\s|\|)", lines[i]):
            buf.append(lines[i])
            i += 1
        out.append(f"<p>{render_inline(' '.join(s.strip() for s in buf), ctx)}</p>")
    return "\n".join(out)


def render_table(rows, ctx, register_anchor_col=False):
    def cells(row):
        return [c.strip() for c in row.strip().strip("|").split("|")]

    body = []
    header = cells(rows[0])
    body.append("<thead><tr>" + "".join(f"<th>{render_inline(c, ctx)}</th>" for c in header) + "</tr></thead>")
    body.append("<tbody>")
    for row in rows[2:] if len(rows) > 1 and re.match(r"^\|[\s:|-]+\|$", rows[1].replace(" ", "")) else rows[1:]:
        cs = cells(row)
        tds = []
        for k, c in enumerate(cs):
            rendered = render_inline(c, ctx)
            if register_anchor_col and k == 0:
                m = re.match(r"^`(reg-[a-z0-9-]+)`$", c)
                if m:
                    rendered = f'<code id="{m.group(1)}" class="reg-def">{m.group(1)}</code>'
            tds.append(f"<td>{rendered}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    body.append("</tbody>")
    return '<div class="table-wrap"><table>' + "".join(body) + "</table></div>"


# --------------------------------------------------------------------------
# index.md -> stage tree
# --------------------------------------------------------------------------

class Node:
    def __init__(self, depth, title, sid, badge, count):
        self.depth = depth
        self.title = title
        self.sid = sid  # 'stage-1.3.2'
        self.num = sid.replace("stage-", "")
        self.badge = badge
        self.count = count
        self.desc_lines = []
        self.children = []


HEAD_RE = re.compile(
    r"^(#{2,6})\s*\[(.+?)\]\((stage-[\d.]+)\.md\)\s*`stage-[\d.]+`\s*(.*?)\s*—\s*(\d+)\s*(?:files|个文件)"
)


def parse_index(md: str):
    roots, stack = [], []
    cur = None
    for line in md.splitlines():
        m = HEAD_RE.match(line)
        if m:
            depth = len(m.group(1)) - 1  # ## -> 1
            node = Node(depth, m.group(2), m.group(3), m.group(4), int(m.group(5)))
            while stack and stack[-1].depth >= depth:
                stack.pop()
            (stack[-1].children if stack else roots).append(node)
            stack.append(node)
            cur = node
        elif cur is not None and not line.startswith("#"):
            cur.desc_lines.append(line)
    return roots


def flatten(nodes):
    for nd in nodes:
        yield nd
        yield from flatten(nd.children)


# --------------------------------------------------------------------------
# stage-*.md parsing
# --------------------------------------------------------------------------

def split_sections(lines):
    """-> (intro_lines, [(title, lines)]) split on '## ' headings."""
    intro, sections, cur_title, cur = [], [], None, []
    for line in lines:
        m = re.match(r"^##\s+(.*)$", line)
        if m and not line.startswith("###"):
            if cur_title is not None:
                sections.append((cur_title, cur))
            cur_title, cur = m.group(1).strip(), []
        elif cur_title is None:
            intro.append(line)
        else:
            cur.append(line)
    if cur_title is not None:
        sections.append((cur_title, cur))
    return intro, sections


def split_h3(lines):
    """-> [(heading, lines)] split on '### '."""
    blocks, cur_title, cur = [], None, []
    for line in lines:
        m = re.match(r"^###\s+(.*)$", line)
        if m and not line.startswith("####"):
            if cur_title is not None:
                blocks.append((cur_title, cur))
            cur_title, cur = m.group(1).strip(), []
        elif cur_title is not None:
            cur.append(line)
    if cur_title is not None:
        blocks.append((cur_title, cur))
    return blocks


TAGLINE_RE = re.compile(r"^`[^`]+`(?:\s*·\s*`[^`]+`)*$")
FN_RE = re.compile(r"^#####\s+`(.+?)`\s*(?:\((?:lines|行)\s*([^)]*)\))?\s*$")
CALLGRAPH_RE = re.compile(r"^\s*\*(?:Call graph|调用图)\*")
FN_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*!?")

# Exact documentation pages for well-known external symbols seen in call graphs.
# Curated, not guessed: names absent here stay plain text.
_STD = "https://doc.rust-lang.org/std/"
_TRACING = "https://docs.rs/tracing/latest/tracing/"
_ANYHOW = "https://docs.rs/anyhow/latest/anyhow/"
EXTERNAL_DOC = {}
for _m in ("assert assert_eq assert_ne debug_assert debug_assert_eq debug_assert_ne panic vec "
           "format println print eprintln eprint write writeln matches todo unimplemented "
           "unreachable include_str include_bytes concat stringify env option_env cfg dbg "
           "format_args compile_error file line column module_path").split():
    EXTERNAL_DOC[_m + "!"] = f"{_STD}macro.{_m}.html"
for _m in "trace debug info warn error span event debug_span info_span warn_span error_span trace_span".split():
    EXTERNAL_DOC[_m + "!"] = f"{_TRACING}macro.{_m}.html"
for _m in "anyhow bail ensure".split():
    EXTERNAL_DOC[_m + "!"] = f"{_ANYHOW}macro.{_m}.html"
_SJ = "https://docs.rs/serde_json/latest/serde_json/"
_UUID = "https://docs.rs/uuid/latest/uuid/struct.Uuid.html"
EXTERNAL_DOC.update({
    # serde_json
    "to_value": f"{_SJ}fn.to_value.html",
    "from_value": f"{_SJ}fn.from_value.html",
    "from_slice": f"{_SJ}fn.from_slice.html",
    "to_vec_pretty": f"{_SJ}fn.to_vec_pretty.html",
    "to_string_pretty": f"{_SJ}fn.to_string_pretty.html",
    # std::fs / os
    "remove_dir_all": f"{_STD}fs/fn.remove_dir_all.html",
    "remove_file": f"{_STD}fs/fn.remove_file.html",
    "read_dir": f"{_STD}fs/fn.read_dir.html",
    "create_dir": f"{_STD}fs/fn.create_dir.html",
    "create_dir_all": f"{_STD}fs/fn.create_dir_all.html",
    "symlink_metadata": f"{_STD}fs/fn.symlink_metadata.html",
    "set_permissions": f"{_STD}fs/fn.set_permissions.html",
    "symlink": f"{_STD}os/unix/fs/fn.symlink.html",
    # std::env
    "var_os": f"{_STD}env/fn.var_os.html",
    "set_var": f"{_STD}env/fn.set_var.html",
    "vars": f"{_STD}env/fn.vars.html",
    "temp_dir": f"{_STD}env/fn.temp_dir.html",
    "current_exe": f"{_STD}env/fn.current_exe.html",
    # std misc, unambiguous
    "last_os_error": f"{_STD}io/struct.Error.html#method.last_os_error",
    "null_mut": f"{_STD}ptr/fn.null_mut.html",
    "stdout": f"{_STD}io/fn.stdout.html",
    "write_str": f"{_STD}fmt/trait.Write.html#tymethod.write_str",
    "debug_struct": f"{_STD}fmt/struct.Formatter.html#method.debug_struct",
    "to_string": f"{_STD}string/trait.ToString.html#tymethod.to_string",
    "pin!": f"{_STD}pin/macro.pin.html",
    # well-known crates
    "new_v4": f"{_UUID}#method.new_v4",
    "from_u128": f"{_UUID}#method.from_u128",
    "spawn_blocking": "https://docs.rs/tokio/latest/tokio/task/fn.spawn_blocking.html",
    "try_parse_from": "https://docs.rs/clap/latest/clap/trait.Parser.html#method.try_parse_from",
    "assert_snapshot!": "https://docs.rs/insta/latest/insta/macro.assert_snapshot.html",
    "assert_matches!": "https://docs.rs/assert_matches/latest/assert_matches/macro.assert_matches.html",
    "json!": "https://docs.rs/serde_json/latest/serde_json/macro.json.html",
    "select!": "https://docs.rs/tokio/latest/tokio/macro.select.html",
    "join!": "https://docs.rs/tokio/latest/tokio/macro.join.html",
    "try_join!": "https://docs.rs/tokio/latest/tokio/macro.try_join.html",
    "lazy_static!": "https://docs.rs/lazy_static/latest/lazy_static/macro.lazy_static.html",
    "tempdir": "https://docs.rs/tempfile/latest/tempfile/fn.tempdir.html",
    "from_millis": f"{_STD}time/struct.Duration.html#method.from_millis",
    "from_secs": f"{_STD}time/struct.Duration.html#method.from_secs",
})

# genuinely ambiguous std names: send readers to the std doc search page,
# which lists every candidate — the official disambiguation
for _s in "now read_to_string sleep from_utf8 channel exists once into_iter from_fn".split():
    EXTERNAL_DOC.setdefault(_s, f"{_STD}?search={_s}")

# repo-defined macro_rules! -> (path, line); filled in main()
MACRO_MAP = {}


def collect_macro_map():
    pat = re.compile(r"^\s*(?:pub\s+)?macro_rules!\s+([A-Za-z_]\w*)")
    for path in FILE_SET:
        try:
            src_lines = (CODE_SRC / path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, ln in enumerate(src_lines, 1):
            m = pat.match(ln)
            if m:
                MACRO_MAP.setdefault(m.group(1) + "!", []).append((path, i))


def collect_fn_map(src, sids):
    """Scan a language's stage files: fn name -> [(stage_sid, anchor_id)].
    Returns (full_name_map, short_name_map)."""
    full, short = {}, {}
    for sid in sids:
        f = src / f"{sid}.md"
        if not f.exists():
            continue
        cur_path = None
        for line in f.read_text(encoding="utf-8").splitlines():
            fm = re.match(r"^### `([^`]+)`$", line)
            if fm:
                cur_path = fm.group(1)
                continue
            nm = FN_RE.match(line)
            if nm and cur_path:
                name = nm.group(1)
                loc = (sid, f"{slugify(cur_path)}--{slugify(name)}", cur_path, name)
                full.setdefault(name, []).append(loc)
                short.setdefault(name.split("::")[-1], []).append(loc)
    return full, short


def link_callgraph_line(line, anchors, self_name, fnmap=None, cur_sid=None):
    """Turn resolvable fn names in a '*Call graph*: ...' line into md links.
    Resolution: same file first, then a language-wide unique match (cross-page)."""
    def rep(m):
        tok = m.group(0)
        if tok == self_name:
            return tok
        if tok.endswith("!"):
            url = EXTERNAL_DOC.get(tok)
            if url:
                return f"[{tok}]({url})"
            locs = MACRO_MAP.get(tok)
            if locs and len(locs) == 1:
                mpath, mline = locs[0]
                return f"[{tok}](../code/{mpath}.html#L{mline})"
            return tok
        aid = anchors.get(tok)
        if aid:
            return f"[{tok}](#{aid})"
        if fnmap:
            for gm in fnmap:
                locs = gm.get(tok)
                if not locs:
                    continue
                if len(locs) == 1:
                    sid, gaid = locs[0][0], locs[0][1]
                    href = f"#{gaid}" if sid == cur_sid else f"{sid}.html#{gaid}"
                else:
                    href = f"fnindex.html#fn-{slugify(tok)}"
                return f"[{tok}]({href})"
        url = EXTERNAL_DOC.get(tok)
        if url:
            return f"[{tok}]({url})"
        return tok

    return FN_TOKEN_RE.sub(rep, line)


def slugify(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-").lower()


def render_file_block(path, lines, ctx, L, fnmap=None, cur_sid=None):
    """One '### `path`' block -> <details class='file'>."""
    # find tags line (first non-empty line)
    tags_html = ""
    body = list(lines)
    for k, line in enumerate(body):
        if line.strip():
            if TAGLINE_RE.match(line.strip()):
                tags = re.findall(r"`([^`]+)`", line)
                tags_html = "".join(f'<span class="tag">{esc(t)}</span>' for t in tags)
                body = body[:k] + body[k + 1:]
            break
    # split off function details (#### heading)
    fn_start = None
    for k, line in enumerate(body):
        if re.match(r"^####\s+", line):
            fn_start = k
            break
    prose = body if fn_start is None else body[:fn_start]
    fn_html = ""
    if fn_start is not None:
        fn_lines = body[fn_start + 1:]
        entries, cur_head, cur = [], None, []
        for line in fn_lines:
            m = FN_RE.match(line)
            if m:
                if cur_head:
                    entries.append((cur_head, cur))
                cur_head, cur = (m.group(1), m.group(2)), []
            elif cur_head:
                cur.append(line)
        if cur_head:
            entries.append((cur_head, cur))
        if entries:
            code_href = f'{ctx["code_prefix"]}{path}.html' if path in FILE_SET and "code_prefix" in ctx else None
            fslug = slugify(path)
            anchors, shorts = {}, {}
            for (name, _), _ in entries:
                aid = f"{fslug}--{slugify(name)}"
                anchors[name] = aid
                shorts.setdefault(name.split("::")[-1], []).append(aid)
            for s, ids in shorts.items():
                if s not in anchors and len(ids) == 1:
                    anchors[s] = ids[0]
            items = []
            for (name, rng), elines in entries:
                elines = [link_callgraph_line(ln, anchors, name, fnmap, cur_sid)
                          if CALLGRAPH_RE.match(ln) else ln for ln in elines]
                rng_html = ""
                if rng:
                    nums = re.findall(r"\d+", rng)
                    if code_href and nums:
                        # Code pages anchor individual lines as #L123. Link to
                        # the start of the range so browser scrolling works.
                        frag = f"#L{nums[0]}"
                        rng_html = f'<a class="fn-range" href="{code_href}{frag}" title="{esc(path)} {esc(rng)}">{esc(rng)} ↗</a>'
                    else:
                        rng_html = f'<span class="fn-range">{esc(rng)}</span>'
                items.append(
                    f'<details class="fn" id="{anchors[name]}">'
                    f'<summary><code>{esc(name)}</code>{rng_html}</summary>'
                    f'<div class="fn-body">{render_blocks(elines, ctx)}</div></details>'
                )
            fn_html = (
                f'<div class="fns"><div class="fns-head">{L["fn_details"]}'
                f'<span class="pill">{len(entries)}</span></div>{"".join(items)}</div>'
            )
    slug = slugify(path)
    tags_div = f'<div class="tags">{tags_html}</div>' if tags_html else ""
    src_link = ""
    if path in FILE_SET and "code_prefix" in ctx:
        src_link = (
            f'<a class="src-link" href="{ctx["code_prefix"]}{path}.html" '
            f'title="{esc(path)}">{L["view_source"]}</a>'
        )
    return (
        f'<details class="file" id="{slug}">'
        f'<summary><span class="chev"></span><code class="fpath">{esc(path)}</code>{src_link}</summary>'
        f'<div class="file-body">'
        f"{tags_div}{render_blocks(prose, ctx)}{fn_html}</div></details>"
    )


def render_files_section(lines, ctx, L, fnmap=None, cur_sid=None):
    out = []
    for heading, blk in split_h3(lines):
        m = re.match(r"^`(.+)`$", heading)
        if m:
            out.append(render_file_block(m.group(1), blk, ctx, L, fnmap, cur_sid))
        else:
            out.append(f'<h3 class="group">{render_inline(heading, ctx)}</h3>')
            out.append(f'<div class="group-desc">{render_blocks(blk, ctx)}</div>')
    return "\n".join(out)


SUBSTAGE_ITEM = re.compile(
    r"^-\s*\[(.+?)\]\((stage-[\d.]+)\.md\)\s*`stage-[\d.]+`\s*(?:[·（(].*?)?—\s*(\d+)\s*(?:files|个文件)"
)


def render_substages(lines, ctx, L, files_word):
    cards = []
    for line in lines:
        m = SUBSTAGE_ITEM.match(line.strip())
        if m:
            title, sid, count = m.group(1), m.group(2), m.group(3)
            cards.append(
                f'<a class="sub-card" href="{sid}.html">'
                f'<span class="sub-num">{sid.replace("stage-", "")}</span>'
                f'<span class="sub-title">{esc(title)}</span>'
                f'<span class="sub-count">{count} {files_word}</span></a>'
            )
    if not cards:
        return render_blocks(lines, ctx)
    return '<div class="sub-grid">' + "".join(cards) + "</div>"


REG_ITEM = re.compile(r"^-\s*`(reg-[a-z0-9-]+)`\s*—\s*(.*)$")


def render_registers_section(lines, ctx, L):
    items = []
    for line in lines:
        m = REG_ITEM.match(line.strip())
        if m:
            rid, desc = m.group(1), m.group(2)
            items.append(
                f'<li><a class="reg" href="{ctx["register_href"]}#{rid}"><code>{rid}</code></a>'
                f'<span class="reg-desc">{render_inline(desc, ctx)}</span></li>'
            )
    if not items:
        return render_blocks(lines, ctx)
    return (
        f'<details class="registers"><summary><span class="chev"></span>'
        f'{L["registers_short"]}<span class="pill">{len(items)}</span></summary>'
        f'<ul class="reg-list">{"".join(items)}</ul></details>'
    )


# --------------------------------------------------------------------------
# page shells
# --------------------------------------------------------------------------

FONTS_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,500;0,8..60,600;0,8..60,700;1,8..60,400&amp;family=Inter:wght@400;500;600;700&amp;family=IBM+Plex+Mono:wght@400;500&amp;family=Noto+Serif+SC:wght@400;500;600;700&amp;family=Noto+Sans+SC:wght@400;500;700&amp;display=swap" rel="stylesheet">'
)

THEME_SCRIPT = (
    "<script>try{var t=localStorage.getItem('hh-theme');"
    "if(t==='dark'||(!t&&matchMedia('(prefers-color-scheme: dark)').matches))"
    "document.documentElement.classList.add('dark');}catch(e){}</script>"
)

BRAND_MARK = """<span class="brand-mark" aria-hidden="true">
<svg viewBox="0 0 32 32" width="22" height="22" fill="none">
<rect x="4" y="3" width="17" height="26" rx="3" class="bm-cover"/>
<rect x="8" y="3" width="17" height="26" rx="3" class="bm-page"/>
<line x1="12" y1="10" x2="21" y2="10" class="bm-line"/>
<line x1="12" y1="15" x2="21" y2="15" class="bm-line"/>
<line x1="12" y1="20" x2="18" y2="20" class="bm-line"/>
</svg>
</span>"""

THEME_BTN = """<button id="theme-toggle" class="icon-btn" aria-label="Toggle dark mode" title="Toggle theme">
<svg class="icon-sun" viewBox="0 0 24 24" width="17" height="17"><circle cx="12" cy="12" r="4.5"/><g><line x1="12" y1="1.5" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22.5"/><line x1="1.5" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22.5" y2="12"/><line x1="4.2" y1="4.2" x2="6" y2="6"/><line x1="18" y1="18" x2="19.8" y2="19.8"/><line x1="19.8" y1="4.2" x2="18" y2="6"/><line x1="6" y1="18" x2="4.2" y2="19.8"/></g></svg>
<svg class="icon-moon" viewBox="0 0 24 24" width="17" height="17"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/></svg>
</button>"""


CODEPANE = """<aside class="codepane" id="codepane" hidden>
<div class="codepane__bar">
<code class="codepane__path" id="codepane-path"></code>
<a class="codepane__act" id="codepane-open" href="#" target="_blank" title="Open in full page">⤢</a>
<button class="codepane__act" id="codepane-close" title="Close (Esc)">✕</button>
</div>
<div class="codepane__body">
<div class="codepane__resize" id="codepane-resize"></div>
<iframe class="codepane__frame" id="codepane-frame" title="source code"></iframe>
</div>
</aside>"""


HOME_ICON = """<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11.4 12 4.2l9 7.2"/><path d="M5.8 9.8V19.8h12.4V9.8"/></svg>"""

BACK_ICON = """<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5.5"/><path d="m11 5.5-6.5 6.5 6.5 6.5"/></svg>"""

REG_ICON = """<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 3 7.8l9 4.8 9-4.8z"/><path d="m3 12.2 9 4.8 9-4.8"/><path d="m3 16.4 9 4.8 9-4.8"/></svg>"""

FNIDX_ICON = """<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6h12"/><path d="M9 12h12"/><path d="M9 18h12"/><path d="M3.5 6h.01"/><path d="M3.5 12h.01"/><path d="M3.5 18h.01"/></svg>"""


def page_shell(title, html_lang, body, rel_assets, extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="{html_lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
{FONTS_LINK}
<link rel="stylesheet" href="{rel_assets}/style.css">
{THEME_SCRIPT}
{extra_head}
</head>
<body>
<div class="progress-bar" id="progress-bar" aria-hidden="true"></div>
{body}
<script src="{rel_assets}/app.js"></script>
</body>
</html>"""


def topbar(L, index_href, register_href, current=""):
    return f"""<header class="topbar">
<div class="topbar__inner">
<a class="topbar__brand" href="{index_href}">{BRAND_MARK}<span>{L['handbook']}</span></a>
<nav class="topbar__nav">
<a href="{index_href}" class="topbar__link topbar__link--icon">{HOME_ICON}<span>{L['back_home']}</span></a>
<a href="{register_href}" class="topbar__link topbar__link--icon{' on' if current == 'register' else ''}">{REG_ICON}<span>{L['registers']}</span></a>
<a href="fnindex.html" class="topbar__link topbar__link--icon{' on' if current == 'fnindex' else ''}">{FNIDX_ICON}<span>{L['fnindex']}</span></a>
{THEME_BTN}
</nav>
</div>
</header>"""


def sidebar_tree(nodes, current_sid, L, files_word):
    """Compact nested nav; ancestors of current open."""
    def anc(sid):
        parts = sid.replace("stage-", "").split(".")
        return {"stage-" + ".".join(parts[:k]) for k in range(1, len(parts))}

    open_ids = anc(current_sid) if current_sid else set()

    def rec(nds):
        out = []
        for nd in nds:
            cls = "cur" if nd.sid == current_sid else ""
            link = f'<a class="s-link {cls}" href="{nd.sid}.html"><span class="s-num">{nd.num}</span>{esc(nd.title)}</a>'
            if nd.children:
                op = " open" if nd.sid in open_ids or nd.sid == current_sid else ""
                out.append(
                    f'<details class="s-node"{op}><summary><span class="chev"></span>{link}</summary>'
                    f'<div class="s-kids">{rec(nd.children)}</div></details>'
                )
            else:
                out.append(f'<div class="s-leaf">{link}</div>')
        return "".join(out)

    return f'<nav class="side-tree">{rec(nodes)}</nav>'


def breadcrumb(node, by_id, L):
    parts = node.num.split(".")
    crumbs = [f'<a href="index.html">{L["back"]}</a>']
    for k in range(1, len(parts)):
        pid = "stage-" + ".".join(parts[:k])
        p = by_id.get(pid)
        if p:
            crumbs.append(f'<a href="{pid}.html">{esc(p.title)}</a>')
    return '<nav class="crumbs">' + '<span class="sep">›</span>'.join(crumbs) + "</nav>"


# --------------------------------------------------------------------------
# source code pages (handbook -> code jump, Studio-style)
# --------------------------------------------------------------------------

RUST_KW = frozenset(
    "as async await break const continue crate dyn else enum extern false fn for if impl in "
    "let loop match mod move mut pub ref return self Self static struct super trait true type "
    "unsafe use where while union".split()
)

RUST_TOK = re.compile(
    r"""(?P<bcom>/\*.*?\*/|/\*.*$)
      | (?P<com>//.*$)
      | (?P<str>"(?:\\.|[^"\\])*(?:"|$))
      | (?P<life>'[A-Za-z_]\w*\b)
      | (?P<char>'(?:\\.|[^'\\])')
      | (?P<attr>\#!?\[[^\]]*\]?)
      | (?P<mac>\b[A-Za-z_]\w*!)
      | (?P<num>\b(?:0[xbo][\dA-Fa-f_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)\w*)
      | (?P<word>\b[A-Za-z_]\w*\b)
    """,
    re.X,
)

TOK_CLS = {"bcom": "c", "com": "c", "str": "s", "char": "s", "life": "m",
           "attr": "m", "mac": "m", "num": "n"}


def highlight_rust_line(line, state):
    """-> (html, state). state: True while inside an unterminated block comment."""
    out = []
    pos = 0
    if state:
        end = line.find("*/")
        if end == -1:
            return f'<b class=c>{esc(line)}</b>', True
        out.append(f'<b class=c>{esc(line[:end + 2])}</b>')
        pos = end + 2
        state = False
    while pos < len(line):
        m = RUST_TOK.search(line, pos)
        if not m:
            out.append(esc(line[pos:]))
            break
        if m.start() > pos:
            out.append(esc(line[pos:m.start()]))
        kind = m.lastgroup
        text = m.group()
        if kind == "bcom" and not text.endswith("*/"):
            state = True
        if kind == "word":
            if text in RUST_KW:
                out.append(f"<b class=k>{esc(text)}</b>")
            else:
                out.append(esc(text))
        else:
            cls = TOK_CLS.get(kind)
            out.append(f"<b class={cls}>{esc(text)}</b>" if cls else esc(text))
        pos = m.end()
    return "".join(out), state


def collect_file_set():
    for cfg in LANGS.values():
        for f in cfg["src"].glob("stage-*.md"):
            for m in re.finditer(r"^### `([^`]+)`$", f.read_text(encoding="utf-8"), re.M):
                p = m.group(1)
                if (CODE_SRC / p).exists():
                    FILE_SET.add(p)


def build_code_pages(force=False):
    built = skipped = 0
    for path in sorted(FILE_SET):
        src_file = CODE_SRC / path
        out = SITE / "code" / (path + ".html")
        if not force and out.exists() and out.stat().st_mtime >= src_file.stat().st_mtime:
            skipped += 1
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        text = src_file.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        is_rust = path.endswith(".rs")
        state = False
        rendered = []
        for i, line in enumerate(lines, 1):
            if is_rust:
                h, state = highlight_rust_line(line, state)
            else:
                h = esc(line)
            rendered.append(f'<i id="L{i}">{h}</i>')
        depth = 1 + path.count("/")  # levels below site root: code/ + parent dirs
        rel = "../" * depth
        body = f"""<header class="topbar">
<div class="topbar__inner">
<a class="topbar__brand" href="{rel}index.html">{BRAND_MARK}<span>Codex&nbsp;Handbook</span></a>
<nav class="topbar__nav">
<a href="{rel}index.html" class="topbar__link topbar__link--icon" id="back-btn">{BACK_ICON}<span>Back · 返回</span></a>
<span class="topbar__link code-langtag">{'Rust' if is_rust else path.rsplit('.', 1)[-1]}</span>
{THEME_BTN}
</nav>
</div>
</header>
<main class="codepage">
<div class="code-head">
<code class="fpath">{esc(path)}</code>
<span class="code-meta">{len(lines)} lines</span>
</div>
<div class="codebox"><pre id="codepre">{''.join(rendered)}</pre></div>
</main>"""
        out.write_text(
            page_shell(path, "en", body, rel + "assets"), encoding="utf-8"
        )
        built += 1
    print(f"[code] {built} pages built, {skipped} up-to-date, {len(FILE_SET)} total")


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------

def build_fn_index(src, order, fnmap, outdir, tree, L, cfg):
    """Disambiguation page for fn names documented in more than one place."""
    full, short = fnmap
    ambig = set()
    for nd in order:
        f = src / f"{nd.sid}.md"
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if not CALLGRAPH_RE.match(line):
                continue
            # `link_callgraph_line()` links ambiguous tokens found anywhere on
            # the call-graph line, so fnindex must create anchors using the same
            # scope. Previously this only scanned parenthesized neighbour lists,
            # leaving a few `fnindex.html#fn-*` links without matching sections.
            for tok in FN_TOKEN_RE.findall(line):
                if len(full.get(tok, [])) > 1 or (tok not in full and len(short.get(tok, [])) > 1):
                    ambig.add(tok)
    by_id = {nd.sid: nd for nd in order}
    secs = []
    for tok in sorted(ambig, key=str.lower):
        locs = full.get(tok) or short.get(tok) or []
        items = []
        for sid, aid, path, name in locs:
            stage = by_id.get(sid)
            stitle = esc(stage.title) if stage else sid
            items.append(
                f'<li><a href="{sid}.html#{aid}"><code>{esc(name)}</code></a>'
                f'<span class="fni-path"><code>{esc(path)}</code></span>'
                f'<span class="fni-stage">{sid.replace("stage-", "")} · {stitle}</span></li>'
            )
        secs.append(
            f'<section class="fni" id="fn-{slugify(tok)}">'
            f'<h3><code>{esc(tok)}</code><span class="pill">{len(locs)} {L["candidates"]}</span></h3>'
            f'<ul>{"".join(items)}</ul></section>'
        )
    body = f"""{topbar(L, f'../index.html?lang={"zh" if cfg["html_lang"].startswith("zh") else "en"}', 'register.html', current='fnindex')}
<div class="layout">
<aside class="sidebar"><div class="side-inner">{sidebar_tree(tree, None, L, cfg["files_word"])}</div></aside>
<main class="content"><h1>{L['fnindex']}</h1>
<p class="fni-note">{L['fnindex_note']}</p>
<input class="filter fni-filter" id="fni-filter" type="search" placeholder="{L['filter_fn']}">
<div id="fni-results"></div>
{''.join(secs)}</main>
{CODEPANE}
</div>"""
    lang_key = "zh" if cfg["html_lang"].startswith("zh") else "en"
    import json as _json
    data = {name: [f"{sid}|{path}" for sid, aid, path, _n in locs]
            for name, locs in full.items()}
    (SITE / "assets" / f"fnidx-{lang_key}.js").write_text(
        "window.FNIDX=" + _json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";",
        encoding="utf-8",
    )
    (outdir / "fnindex.html").write_text(
        page_shell(L["fnindex"], cfg["html_lang"], body, "../assets",
                   extra_head=f'<script src="../assets/fnidx-{lang_key}.js"></script>'),
        encoding="utf-8",
    )
    print(f"[fnindex] {len(ambig)} ambiguous names, {len(full)} searchable")


def build_lang(lang):
    cfg = LANGS[lang]
    L = cfg["labels"]
    src = cfg["src"]
    outdir = SITE / lang
    outdir.mkdir(parents=True, exist_ok=True)

    def make_ctx(index_href="index.html", register_href="register.html", stage_prefix="",
                 code_prefix="../code/"):
        def link(url):
            if url.endswith(".md"):
                base = url[:-3]
                name = base.split("/")[-1]
                if name == "index" or name == "overview":
                    return index_href
                if name == "register":
                    return register_href
                if name.startswith("stage-"):
                    return f"{stage_prefix}{name}.html"
            return url

        return {"link": link, "register_href": register_href, "code_prefix": code_prefix}

    tree = parse_index((src / "index.md").read_text(encoding="utf-8"))
    order = list(flatten(tree))
    by_id = {nd.sid: nd for nd in order}

    # ---- stage pages
    ctx = make_ctx()
    fnmap = collect_fn_map(src, [nd.sid for nd in order])
    build_fn_index(src, order, fnmap, outdir, tree, L, cfg)
    missing = []
    for pos, nd in enumerate(order):
        md_path = src / f"{nd.sid}.md"
        if not md_path.exists():
            missing.append(nd.sid)
            continue
        lines = md_path.read_text(encoding="utf-8").splitlines()
        title = nd.title
        m = re.match(r"^#\s+(.*?)\s*`(stage-[\d.]+)`\s*$", lines[0]) if lines else None
        body_lines = lines[1:] if m else lines
        if m:
            title = m.group(1).strip()

        intro, sections = split_sections(body_lines)
        parts = []
        parts.append(breadcrumb(nd, by_id, L))
        badge = '<span class="badge-x">✕</span>' if nd.badge else ""
        parts.append(
            f'<div class="stage-head"><h1>{esc(title)}</h1>'
            f'<div class="meta"><span class="chip mono">{nd.sid}</span>'
            f'<span class="chip">{nd.count} {cfg["files_word"]}</span>{badge}</div></div>'
        )
        parts.append(f'<div class="intro">{render_blocks(intro, ctx)}</div>')

        files_html = None
        for sec_title, sec_lines in sections:
            if sec_title in L["substages"]:
                parts.append(f'<h2 class="sec">{esc(sec_title)}</h2>')
                parts.append(render_substages(sec_lines, ctx, L, cfg["files_word"]))
            elif sec_title.startswith("📊"):
                parts.append(render_registers_section(sec_lines, ctx, L))
            elif sec_title in L["files_in_stage"]:
                files_html = render_files_section(sec_lines, ctx, L, fnmap, nd.sid)
            else:
                parts.append(f'<h2 class="sec">{render_inline(sec_title, ctx)}</h2>')
                parts.append(render_blocks(sec_lines, ctx))
        if files_html:
            nfiles = files_html.count('<details class="file"')
            parts.append(
                f'<div class="files-bar"><h2 class="sec">{next(iter(L["files_in_stage"]))}'
                f'<span class="pill">{nfiles}</span></h2>'
                f'<span class="spacer"></span>'
                f'<button class="tool" data-x="expand">{L["expand"]}</button>'
                f'<button class="tool" data-x="collapse">{L["collapse"]}</button></div>'
            )
            parts.append(files_html)

        # prev / next
        prev_nd = order[pos - 1] if pos > 0 else None
        next_nd = order[pos + 1] if pos + 1 < len(order) else None
        pn = ['<nav class="pn">']
        if prev_nd:
            pn.append(
                f'<a class="pn-prev" href="{prev_nd.sid}.html"><span>← {L["prev"]}</span>'
                f'<strong>{esc(prev_nd.title)}</strong></a>'
            )
        else:
            pn.append("<span></span>")
        if next_nd:
            pn.append(
                f'<a class="pn-next" href="{next_nd.sid}.html"><span>{L["next"]} →</span>'
                f'<strong>{esc(next_nd.title)}</strong></a>'
            )
        pn.append("</nav>")
        parts.append("".join(pn))

        body = f"""{topbar(L, f'../index.html?lang={lang}', 'register.html')}
<div class="layout">
<aside class="sidebar"><div class="side-inner">{sidebar_tree(tree, nd.sid, L, cfg["files_word"])}</div></aside>
<main class="content">{''.join(parts)}</main>
{CODEPANE}
</div>"""
        (outdir / f"{nd.sid}.html").write_text(
            page_shell(f"{nd.sid} · {title}", cfg["html_lang"], body, "../assets"),
            encoding="utf-8",
        )

    # ---- register page
    reg_md = src / "register.md"
    if reg_md.exists():
        lines = reg_md.read_text(encoding="utf-8").splitlines()
        reg_html = render_blocks(lines[1:], ctx, heading_shift=-1, register_anchor_col=True)
        body = f"""{topbar(L, f'../index.html?lang={lang}', 'register.html', current='register')}
<div class="layout">
<aside class="sidebar"><div class="side-inner">{sidebar_tree(tree, None, L, cfg["files_word"])}</div></aside>
<main class="content"><h1>{L['registers']}</h1>{reg_html}</main>
</div>"""
        (outdir / "register.html").write_text(
            page_shell(L["registers"], cfg["html_lang"], body, "../assets"), encoding="utf-8"
        )

    # ---- per-language index (redirect target = landing section)
    (outdir / "index.html").write_text(
        f'<!DOCTYPE html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=../index.html?lang={lang}">',
        encoding="utf-8",
    )

    # ---- landing tree (rich)
    lctx = make_ctx(index_href=f"index.html?lang={lang}", register_href=f"{lang}/register.html",
                    stage_prefix=f"{lang}/", code_prefix="code/")

    def rich(nds, depth=0):
        out = []
        for nd in nds:
            badge = '<span class="badge-x" title="cross-cutting">✕</span>' if nd.badge else ""
            kids = f'<div class="t-kids">{rich(nd.children, depth + 1)}</div>' if nd.children else ""
            desc = render_blocks(nd.desc_lines, lctx)
            # Default-expanded so the index shows the full stage tree at a glance
            # (all 62/140 stages), instead of only the top-level rows. The
            # Expand/Collapse-all buttons still toggle everything.
            op = " open" if nd.children else ""
            out.append(
                f'<details class="t-node d{depth}"{op} data-title="{esc(nd.title.lower())} {nd.sid}">'
                f'<summary><span class="chev"></span><span class="t-num">{nd.num}</span>'
                f'<a class="t-link" href="{lang}/{nd.sid}.html">{esc(nd.title)}</a>{badge}'
                f'<span class="t-count">{nd.count} {cfg["files_word"]}</span></summary>'
                f'<div class="t-body">{desc}{kids}</div></details>'
            )
        return "".join(out)

    # overview text
    ov_html = ""
    ov = src / "overview.md"
    if ov.exists():
        ov_lines = ov.read_text(encoding="utf-8").splitlines()
        intro, sections = split_sections(ov_lines[1:])
        for t, ls in sections:
            if "See also" in t or "另见" in t or "相关" in t:
                continue
            ov_html += render_blocks(ls, lctx)

    def codex_ascii_overview() -> str:
        """Terminus-style landing overview: prose + ASCII diagrams + top-stage map.

        The per-language `overview.md` remains the source of the prose. This adds
        a stable visual map on the landing page so readers can understand the
        whole Codex lifecycle before diving into the stage tree.
        """
        if lang == "zh":
            lifecycle = """[入口识别与模式分发]
          |
          v
[环境探测 / 进程加固 / 本地资源准备]
          |
          v
[配置 + 账号 + 权限 + 模型/插件目录装配]
          |
          v
[服务端 / 终端界面 / 会话建立]
          |
          v
[事件主循环：接收输入、定位线程、恢复上下文]
          |
          v
[组装提示词：历史 + 规则 + 工具 + 记忆 + 工作区]
          |
          v
[调用模型并处理流式输出]
          |
          v
[审批 / 沙箱 / 工具执行 / 文件修改 / 外部服务]
          |
          v
[结果归并：更新 UI、保存记录、写入状态]
          |
          v
[关闭收尾：阻止新任务、排空队列、保存最终状态]"""
            turn = """[用户或客户端输入]
          |
          v
[路由到会话/线程]
          |
          v
[拼上下文包和可用工具清单]
          |
          v
[发送给模型]
          |
          v
{模型需要做动作吗？}
    | 否                         | 是
    v                            v
[整理回复并更新界面]       [检查权限/是否要用户批准]
                                  |
                                  v
                         [在沙箱或受控环境里执行]
                                  |
                                  v
                         [把工具结果交回模型/会话]
                                  |
                                  v
                       {任务结束了吗？}
                         | 是        | 否
                         v           v
                   [保存并收尾]   [进入下一轮]"""
            title = "1. 系统总览"
            diagrams = "2. 两张 ASCII 图"
            life_title = "图 A · 生命周期"
            turn_title = "图 B · 一次对话回合"
            stage_title = "3. 主流程地图"
        else:
            lifecycle = """[entrypoint + runtime-mode dispatch]
          |
          v
[environment discovery / hardening / local resources]
          |
          v
[config + auth + permissions + model/plugin catalogs]
          |
          v
[server / TUI / session startup]
          |
          v
[event loop: receive input, locate thread, restore context]
          |
          v
[prompt assembly: history + rules + tools + memory + workspace]
          |
          v
[model call and streaming response handling]
          |
          v
[approval / sandbox / tool execution / file edits / external services]
          |
          v
[result projection: UI updates, transcripts, persisted state]
          |
          v
[shutdown: block new work, drain queues, save final state]"""
            turn = """[user or client input]
          |
          v
[route to session/thread]
          |
          v
[build context bundle and tool catalog]
          |
          v
[send request to model]
          |
          v
{does the model need action?}
    | no                         | yes
    v                            v
[format reply + update UI]   [check permission / ask user if needed]
                                  |
                                  v
                         [run in sandbox or controlled environment]
                                  |
                                  v
                         [feed tool result back to session/model]
                                  |
                                  v
                       {is the task done?}
                         | yes       | no
                         v           v
                    [persist + end] [next turn]"""
            title = "1. System Overview"
            diagrams = "2. Two ASCII Diagrams"
            life_title = "Diagram A · Lifecycle"
            turn_title = "Diagram B · One Conversation Turn"
            stage_title = "3. Main Flow Map"

        def first_sentence(lines):
            text = " ".join(s.strip() for s in lines if s.strip())
            if not text:
                return ""
            parts = re.split(r"(?<=[。.!?])\s+", text, maxsplit=1)
            return parts[0]

        items = []
        for nd in tree:
            desc = first_sentence(nd.desc_lines)
            desc_html = render_inline(desc, lctx) if desc else ""
            items.append(
                f"<li><strong>{esc(nd.title)}</strong>"
                f"{': ' + desc_html if desc_html else ''}</li>"
            )

        return (
            f"<h4>{title}</h4>"
            f"{ov_html}"
            f"<h4>{diagrams}</h4>"
            f"<h5>{life_title}</h5><pre><code>{esc(lifecycle)}</code></pre>"
            f"<h5>{turn_title}</h5><pre><code>{esc(turn)}</code></pre>"
            f"<h4>{stage_title}</h4><ul>{''.join(items)}</ul><hr>"
        )

    if ov_html:
        ov_html = codex_ascii_overview()

    total_files = sum(nd.count for nd in tree)
    n_stages = len(order)
    if missing:
        print(f"[{lang}] WARNING missing stage files: {missing}", file=sys.stderr)
    print(f"[{lang}] {n_stages} stages, {total_files} files, landing tree ok")
    return {
        "tree_html": rich(tree),
        "overview_html": ov_html,
        "n_stages": n_stages,
        "total_files": total_files,
        "labels": L,
        "cfg": cfg,
    }


def build_landing(data):
    sections = []
    for lang in ("zh", "en"):
        d = data[lang]
        L = d["labels"]
        kicker = "System Handbook" if lang == "en" else "系统手册 · System Handbook"
        sections.append(f"""<section class="lang-sec" data-lang="{lang}" hidden>
<div class="hero">
<p class="hero__kicker">{kicker}</p>
<h1>{L['handbook']}</h1>
<p class="hero-sub">
<span class="stat"><strong>{d['n_stages']}</strong> {L['stages']}</span>
<span class="dot">·</span>
<span class="stat"><strong>{d['total_files']}</strong> {L['files']}</span>
<span class="dot">·</span>
<a href="{lang}/register.html">{L['registers']} →</a>
<span class="dot">·</span>
<a href="{lang}/fnindex.html">{L['fnindex']} →</a>
</p>
</div>
<details class="overview" open><summary><span class="chev"></span>{L['overview']}</summary>
<div class="overview-body">{d['overview_html']}</div></details>
<div class="tree-bar">
<h2>{L['index']}</h2>
<input class="filter" type="search" placeholder="{L['filter']}" data-tree="tree-{lang}">
<button class="tool" data-x="expand">{L['expand']}</button>
<button class="tool" data-x="collapse">{L['collapse']}</button>
</div>
<div class="tree" id="tree-{lang}">{d['tree_html']}</div>
</section>""")

    body = f"""<header class="topbar">
<div class="topbar__inner">
<span class="topbar__brand">{BRAND_MARK}<span>Codex&nbsp;Handbook</span></span>
<nav class="topbar__nav">
<div class="seg" id="lang-seg" role="tablist" aria-label="Language">
<button data-lang="zh">中文</button>
<button data-lang="en">EN</button>
</div>
{THEME_BTN}
</nav>
</div>
</header>
<main class="landing">{''.join(sections)}</main>"""
    (SITE / "index.html").write_text(
        page_shell("Codex System Handbook", "zh-CN", body, "assets"), encoding="utf-8"
    )


def main():
    (SITE / "assets").mkdir(parents=True, exist_ok=True)
    collect_file_set()
    collect_macro_map()
    print(f"[code] {len(FILE_SET)} documented source files found under {CODE_SRC}, "
          f"{len(MACRO_MAP)} repo macros")
    build_code_pages(force="--force-code" in sys.argv)
    data = {lang: build_lang(lang) for lang in ("en", "zh")}
    build_landing(data)
    print("site ->", SITE)


if __name__ == "__main__":
    main()
