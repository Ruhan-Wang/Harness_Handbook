"""lang_layer.py — the multi-language substrate for resync_handbook.

resync used to hardwire its four language-specific primitives to Python's stdlib
`ast`: function line spans, a syntax gate, a rename body-fingerprint, and a fresh
call graph. This module makes them language-agnostic by delegating to the
handbook_generate_small adapters (Python via `ast`, Rust/TS/Go/... via tree-sitter),
which already emit a language-neutral IR (functions + resolved call edges) and a
`graph.json` byte-compatible with the phase-1 the classifier consumes.

Design:
  - Python keeps an `ast`-native path here, byte-identical to resync's original
    helpers, so a Python target's behavior is unchanged and needs no tree-sitter.
  - Every other language rides `get_adapter(lang)` from the small pipeline.
  - Import-light: the adapters (and tree-sitter) load LAZILY on first non-Python
    use, so a Python-only run never imports tree-sitter.

Qualname conventions line up by construction: the Python adapter emits dotted
`Class.method.inner` (what resync's mapping already uses), and the non-Python
adapters emit their own convention (Rust `Type::method`); since a language's
handbook mapping is built by the SAME adapters, span-lookup-by-qualname matches.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent                 # .../final_handbook_as_helper/pipeline
HELPER_ROOT = HERE.parent
REPO_ROOT = HELPER_ROOT.parent                         # .../Harness_Handbook
_SMALL = Path(__import__("os").environ.get("HANDBOOK_MULTILANG_ROOT") or
              (REPO_ROOT / "handbook_generate_small"))

# directory names skipped when scanning a tree (VCS / deps / build output)
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "target", "build", "dist",
    "out", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "venv", ".venv", "env", ".env", "site-packages", ".idea", ".vscode",
}

_READY = False


def _ensure_adapters() -> None:
    """Put the small pipeline's adapters + phase1 on sys.path (once). Only the
    NON-Python paths need this, so it is called lazily. Deliberately does NOT add
    the small pipeline's phase2/phase3 dirs — resync imports those from its own
    (terminus) pipeline and the names would collide."""
    global _READY
    if _READY:
        return
    for p in (str(_SMALL), str(_SMALL / "adapters"), str(_SMALL / "phase1")):
        if p not in sys.path:
            sys.path.insert(0, p)
    _READY = True


def ext_of(glob_or_ext: str) -> str:
    """Extract the file suffix from a source glob or extension:
    '*.py' -> '.py', '.rs' -> '.rs', '**/*.py' -> '.py', 'src/*.rs' -> '.rs'."""
    base = glob_or_ext.rsplit("/", 1)[-1]              # drop any directory part
    return "." + base.rsplit(".", 1)[-1] if "." in base else base


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ─── 1. function line spans: qualname -> (start, end), 1-based inclusive ──────

def _py_spans(py_path: Path) -> dict[str, tuple[int, int]]:
    """Faithful copy of resync's original `_ast_spans`: module-level, methods AND
    nested defs (dotted path), decorators included in the span."""
    spans: dict[str, tuple[int, int]] = {}

    def walk(body, prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{node.name}"
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                spans[name] = (start, node.end_lineno)
                walk(node.body, f"{name}.")
            elif isinstance(node, ast.ClassDef):
                walk(node.body, f"{prefix}{node.name}.")

    walk(ast.parse(py_path.read_text()).body, "")
    return spans


def spans(path: Path, lang: str) -> dict[str, tuple[int, int]]:
    """qualname -> (start, end) for every function/method in one source file."""
    if lang == "python":
        return _py_spans(path)
    _ensure_adapters()
    from base import get_adapter                        # noqa: E402

    path = Path(path).resolve()                          # absolute → adapter's
    #                                relative_to(source_root) yields just the name
    analysis = get_adapter(lang).analyze([path], path.parent)
    return {fn.qualname: (fn.line_start, fn.line_end) for fn in analysis.functions}


# ─── 2. syntax check for one file ─────────────────────────────────────────────

def syntax_ok(path: Path, lang: str, src: str | None = None) -> bool:
    """True when `src` parses cleanly. Python: stdlib compile(). Other languages:
    tree-sitter parse with no ERROR node."""
    if src is None:
        src = path.read_text(encoding="utf-8", errors="replace")
    if lang == "python":
        try:
            compile(src, path.name, "exec")
            return True
        except SyntaxError:
            return False
    _ensure_adapters()
    from base import parse_tree                          # noqa: E402

    try:
        root = parse_tree(lang, src)
    except Exception:                                    # noqa: BLE001
        return False
    return not root.descendants_of_kind("ERROR")


# ─── 3. rename body fingerprint (position- and name-independent) ──────────────

def _is_sig_line(line: str, lang: str) -> bool:
    """Does this line open the function definition (carrying its name)? The
    fingerprint hashes everything BELOW it so a rename (name changes on this line,
    body identical) still matches."""
    s = line.strip()
    if lang == "python":
        return s.startswith(("def ", "async def "))
    if lang == "rust":
        return "fn " in s and (
            s.startswith(("fn ", "async fn ", "pub ", "pub(", "const ",
                          "unsafe ", "extern ", "default ")))
    if lang == "go":
        return s.startswith("func ")
    if lang in ("typescript", "javascript"):
        return ("function" in s) or ("=>" in s) or (
            s.split("(", 1)[0].strip().replace("async", "").strip().isidentifier()
            and s.endswith("{"))
    return False


def body_fingerprint(lines: list[str], lang: str) -> str:
    """Hash of everything below the signature line; falls back to skipping the
    first line when no signature line is recognized."""
    for i, ln in enumerate(lines):
        if _is_sig_line(ln, lang):
            return _sha1("\n".join(lines[i + 1:]))
    return _sha1("\n".join(lines[1:]))


# ─── 4. fresh call graph over an edited tree ─────────────────────────────────

def fresh_graph(code_dir: Path, lang: str, default_ext: str) -> dict:
    """Re-extract the call graph from the edited tree via the language adapter and
    assemble it into the phase-1 `graph.json` shape (nodes keyed by id + caller/
    callee edges). Returns an empty graph on any failure — the classifier degrades
    to no caller/callee context rather than crashing."""
    try:
        _ensure_adapters()
        from base import get_adapter                     # noqa: E402
        import build_graph as bg                         # noqa: E402

        code_dir = Path(code_dir).resolve()             # absolute discovered paths
        adapter = get_adapter(lang)
        files = adapter.discover(code_dir)
        analysis = adapter.analyze(files, code_dir)
        kept, _dropped = bg.partition_edges(analysis.edges)
        nodes = bg.build_node_table(analysis.functions, kept, default_ext)
        return {"nodes": nodes,
                "edges": [{"caller_id": e.caller_id, "callee_id": e.callee_id}
                          for e in kept]}
    except Exception:                                    # noqa: BLE001
        return {"nodes": {}, "edges": []}


def supported_languages() -> list[str]:
    """Languages with a registered adapter (python is always available; the rest
    depend on tree-sitter being installed)."""
    try:
        _ensure_adapters()
        from base import available_languages             # noqa: E402

        return available_languages()
    except Exception:                                    # noqa: BLE001
        return ["python"]
