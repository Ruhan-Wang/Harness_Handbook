"""Language adapter contract + registry + tree-sitter loading.

A `LanguageAdapter` turns a set of source files into language-agnostic IR
(`ModuleAnalysis`). The *interface* is uniform across languages; the
*implementation* picks the best tool per language:

  - Python  -> the stdlib `ast` module (most precise; matches the legacy output)
  - Rust / TypeScript / Go -> tree-sitter (one backend, per-language grammar)

build_graph and the rest of the pipeline never import a concrete adapter — they
go through `get_adapter(lang)` / `adapter_for_file(path)`.
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Callable

from ir import ModuleAnalysis

# Directories to skip during discovery across all languages: VCS, dependency
# trees, build/cache output, virtualenvs. Per-language adapters add their own
# (e.g. *_test.go, *.d.ts) on top of this.
COMMON_SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "vendor", "target", "build", "dist", "out",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "venv", ".venv", "env", ".env", "site-packages",
    ".idea", ".vscode",
}


class LanguageAdapter(abc.ABC):
    """Parse source files of one language into the shared IR."""

    name: str = ""
    extensions: tuple[str, ...] = ()

    @abc.abstractmethod
    def analyze(self, files: list[Path], source_root: Path) -> ModuleAnalysis:
        """Parse `files` (paths relative to `source_root` are used as the IR
        `file` field) and return functions + resolved call edges."""
        raise NotImplementedError

    def statement_spans(self, file_path: Path, qualname: str) -> list[tuple[int, int]] | None:
        """Return [(start, end), ...] 1-based inclusive line spans of statements
        inside the named function's body — the legal boundaries Phase 2 snaps
        LLM-proposed region ranges to. Return None if unsupported or the
        function can't be located (Phase 2 then keeps the LLM range and flags
        it needs_review). Default: unsupported."""
        return None

    def discover(self, source_root: Path) -> list[Path]:
        """Default file discovery: every file under source_root whose suffix is
        one of `self.extensions`, skipping COMMON_SKIP_DIRS. Adapters may
        override to add language-specific filters (test files, .d.ts, ...)."""
        out: list[Path] = []
        for ext in self.extensions:
            for p in sorted(source_root.rglob(f"*{ext}")):
                if any(part in COMMON_SKIP_DIRS for part in p.relative_to(source_root).parts):
                    continue
                out.append(p)
        return out


# ---------- tree-sitter loading (shared by the non-Python adapters) ----------

_PARSER_CACHE: dict[str, object] = {}


def get_ts_parser(lang: str):
    """Return a configured tree-sitter Parser for `lang`.

    Primary path: tree-sitter-language-pack (one wheel, all grammars).
    Fallback: standalone tree_sitter_<lang> grammar packages.
    Raises a clear error if neither is installed.
    """
    if lang in _PARSER_CACHE:
        return _PARSER_CACHE[lang]

    # Primary: language pack
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore

        parser = get_parser(lang)
        _PARSER_CACHE[lang] = parser
        return parser
    except Exception:
        pass

    # Fallback: standalone grammar modules
    _STANDALONE = {
        "python": "tree_sitter_python",
        "rust": "tree_sitter_rust",
        "typescript": "tree_sitter_typescript",
        "go": "tree_sitter_go",
    }
    mod_name = _STANDALONE.get(lang)
    if mod_name is not None:
        try:
            import importlib

            from tree_sitter import Language, Parser  # type: ignore

            grammar = importlib.import_module(mod_name)
            # typescript module exposes language_typescript(); others language()
            if lang == "typescript":
                language = Language(grammar.language_typescript())
            else:
                language = Language(grammar.language())
            parser = Parser(language)
            _PARSER_CACHE[lang] = parser
            return parser
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                f"Failed to load tree-sitter grammar for {lang!r} via {mod_name}: {exc}"
            ) from exc

    raise RuntimeError(
        f"No tree-sitter grammar available for {lang!r}. Install either "
        f"'tree-sitter-language-pack' or the standalone grammar package."
    )


# ---------- tree-sitter node wrapper ----------
#
# The installed binding (tree-sitter-language-pack) exposes node accessors as
# *methods* (node.kind(), node.start_byte(), node.start_position().row) and has
# no .text/.children/.type. TSNode normalizes all of that into clean property
# access so the per-language adapters read naturally and stay immune to whether
# a given accessor is a method or a property across binding versions.


def _v(x):
    """Return x() if it's callable (method-style binding), else x (property)."""
    return x() if callable(x) else x


class TSNode:
    __slots__ = ("_n", "_src")

    def __init__(self, node, src: bytes):
        self._n = node
        self._src = src

    @property
    def raw(self):
        return self._n

    @property
    def kind(self) -> str:
        return _v(self._n.kind)

    @property
    def start_byte(self) -> int:
        return _v(self._n.start_byte)

    @property
    def end_byte(self) -> int:
        return _v(self._n.end_byte)

    @property
    def start_row(self) -> int:
        return _v(getattr(_v(self._n.start_position), "row"))

    @property
    def end_row(self) -> int:
        return _v(getattr(_v(self._n.end_position), "row"))

    @property
    def is_named(self) -> bool:
        return _v(self._n.is_named)

    @property
    def text(self) -> str:
        return self._src[self.start_byte:self.end_byte].decode("utf8", "replace")

    @property
    def child_count(self) -> int:
        return _v(self._n.child_count)

    @property
    def named_child_count(self) -> int:
        return _v(self._n.named_child_count)

    def child(self, i: int):
        c = self._n.child(i)
        return TSNode(c, self._src) if c is not None else None

    def field(self, name: str):
        c = self._n.child_by_field_name(name)
        return TSNode(c, self._src) if c is not None else None

    def children(self) -> list["TSNode"]:
        return [TSNode(self._n.child(i), self._src) for i in range(self.child_count)]

    def named_children(self) -> list["TSNode"]:
        return [TSNode(self._n.named_child(i), self._src) for i in range(self.named_child_count)]

    def children_of_kind(self, *kinds: str) -> list["TSNode"]:
        want = set(kinds)
        return [c for c in self.children() if c.kind in want]

    def first_of_kind(self, *kinds: str):
        want = set(kinds)
        for c in self.children():
            if c.kind in want:
                return c
        return None

    def descendants_of_kind(self, *kinds: str) -> list["TSNode"]:
        """Pre-order walk yielding every node whose kind is in `kinds`."""
        want = set(kinds)
        out: list[TSNode] = []
        stack = [self]
        while stack:
            n = stack.pop()
            if n.kind in want:
                out.append(n)
            # push children in reverse so traversal is left-to-right
            kids = n.children()
            for c in reversed(kids):
                stack.append(c)
        return out


def collect_line_spans(body: TSNode) -> list[tuple[int, int]]:
    """Every named node's 1-based (start, end) line span inside `body`, deduped
    and sorted. A superset of statement boundaries — fine as snap candidates:
    snap_range picks the nearest, extra candidates only sharpen it."""
    spans: set[tuple[int, int]] = set()
    stack = [body]
    while stack:
        n = stack.pop()
        for c in n.named_children():
            spans.add((c.start_row + 1, c.end_row + 1))
            stack.append(c)
    return sorted(spans)


def parse_tree(lang: str, source: str | bytes) -> TSNode:
    """Parse source and return the wrapped root node."""
    if isinstance(source, bytes):
        src_bytes = source
        src_str = source.decode("utf8", "replace")
    else:
        src_str = source
        src_bytes = source.encode("utf8")
    parser = get_ts_parser(lang)
    # Bindings disagree on whether parse() wants str or bytes; try both.
    try:
        tree = parser.parse(src_str)
    except TypeError:
        tree = parser.parse(src_bytes)
    return TSNode(_v(tree.root_node), src_bytes)


# ---------- adapter registry ----------

_REGISTRY: dict[str, Callable[[], LanguageAdapter]] = {}
_EXT_INDEX: dict[str, str] = {}


def register(name: str, factory: Callable[[], LanguageAdapter], extensions: tuple[str, ...]) -> None:
    _REGISTRY[name] = factory
    for ext in extensions:
        _EXT_INDEX[ext] = name


def get_adapter(lang: str) -> LanguageAdapter:
    if lang not in _REGISTRY:
        raise KeyError(
            f"Unknown language {lang!r}. Registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[lang]()


def adapter_for_file(path: Path) -> LanguageAdapter:
    ext = path.suffix
    if ext not in _EXT_INDEX:
        raise KeyError(f"No adapter registered for extension {ext!r}")
    return get_adapter(_EXT_INDEX[ext])


def available_languages() -> list[str]:
    return sorted(_REGISTRY)


def discover_all(source_root: Path) -> dict[str, list[Path]]:
    """For a mixed-language repo: every registered adapter's discovered files,
    keyed by language, skipping languages with no files. Each adapter applies
    its own skip rules (target/, node_modules/, *_test.go, *.d.ts, ...)."""
    result: dict[str, list[Path]] = {}
    for lang in available_languages():
        try:
            files = get_adapter(lang).discover(source_root)
        except Exception:
            continue
        if files:
            result[lang] = files
    return result


def _autoregister() -> None:
    """Import the concrete adapters so they self-register. Done lazily to avoid
    a hard import cycle and to tolerate a missing tree-sitter at import time
    (the Python adapter has no third-party dep)."""
    try:
        from python_adapter import PythonAdapter  # noqa: F401
    except Exception:
        pass
    try:
        from rust_adapter import RustAdapter  # noqa: F401
    except Exception:
        pass
    try:
        from typescript_adapter import TypeScriptAdapter  # noqa: F401
    except Exception:
        pass
    try:
        from go_adapter import GoAdapter  # noqa: F401
    except Exception:
        pass
    try:
        from scripting_adapters import (  # noqa: F401
            PowerShellAdapter, ShellAdapter, StarlarkAdapter,
        )
    except Exception:
        pass


_autoregister()
