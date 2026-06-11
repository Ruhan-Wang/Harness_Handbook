#!/usr/bin/env python3
"""
Phase 1: extract L0 call graph from the Terminus 2 harness.

Mechanically walks the AST of every source file under
`harbor/src/harbor/agents/terminus_2/` and emits:

  - graph.json         the function call index (internal + boundary nodes
                       only, all edges go named-function → named-function)
  - functions.csv      flat function index with blank Phase-2 columns
                       (unit_id / unit_name / responsibility)
  - graph.dot          Graphviz visualization source (subgraph per file)
  - dropped_calls.json audit log of calls that didn't make it into the
                       main graph, grouped by why (inherited_method /
                       self_attr_unknown / builtin / local_var_method /
                       string_literal_method / bare_name)

Edge policy: the graph keeps only edges whose callee resolves to a
named def-defined function — either an internal harness function or a
boundary function in a known module (harbor.*, stdlib, etc.).  Calls
where we can't name the callee (closures, inherited methods on unscanned
base classes, methods on local variables, builtins, str/list methods)
are partitioned out and written to dropped_calls.json with a category
so Phase 2 can audit them.

Resolution strategy (best-effort, pure static AST):
  - self.foo()                  → method `foo` on the caller's class
  - self._attr.foo()            → if `_attr` was assigned `ClassName(...)`
                                  or `self.method() -> Type` anywhere in
                                  the class, resolve via that type
  - param.foo()                 → if the caller annotates `param: T`,
                                  resolve to `T.foo` (param_method edge)
  - bare_name()                 → same-module function/class, then
                                  module-level/function-local imports,
                                  else unresolved
  - Module.foo() / Class.foo()  → imports table, else unresolved
  - Constructors (Class(...))   → edge to `Class.__init__`

Type-learning policy:
  - Two-phase: every method's return annotation is collected before any
    self-attr type is inferred, so order of definitions doesn't matter.
  - First-write wins: a method's reassignment to `self.X` won't override
    the type learned in __init__.
  - Generic builtin types (str, int, list, dict, ...) are NOT recorded
    as attr/param types — `response.find(...)` etc. would just produce
    noise (str.find, list.append, …) without architectural meaning.

Known limitations:
  - No MRO / inheritance resolution: `self.logger.X` (inherited from
    BaseAgent) lands in dropped_calls.json::inherited_method.
  - No type inference for plain locals: `x = Foo(); x.bar()` -> dropped.
  - No conditional / branch annotation on edges; no decorator expansion.
  - Aliased imports of internal classes (`from .x import Foo as Bar`)
    are not routed back to internal.
"""

from __future__ import annotations

import ast
import csv
import json
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------- config ----------

# Source root + output dir are env-overridable so the same extractor can run
# against an arbitrary repo opened in Handbook Studio. Falls back to the original
# terminus_2 layout when the env vars are absent.
_DEFAULT_HARNESS_DIR = (
    Path(__file__).resolve().parents[2]
    / "harbor"
    / "src"
    / "harbor"
    / "agents"
    / "terminus_2"
)
HARNESS_DIR = Path(os.environ.get("HANDBOOK_SOURCE_ROOT", str(_DEFAULT_HARNESS_DIR)))
OUTPUT_DIR = Path(os.environ.get("HANDBOOK_PHASE1_OUT", str(Path(__file__).resolve().parent)))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _discover_harness_files(root: Path) -> list[str]:
    """Return source .py files (relative to root) to scan.

    When HANDBOOK_SOURCE_ROOT is provided we auto-discover every .py file under
    it, skipping tests, caches, and virtualenvs. Otherwise we use the curated
    terminus_2 file list.
    """
    skip_dirs = {
        "__pycache__", ".git", ".venv", "venv", "node_modules",
        "tests", "test", ".handbook", "build", "dist",
    }
    files: list[str] = []
    for p in sorted(root.rglob("*.py")):
        if any(part in skip_dirs for part in p.relative_to(root).parts[:-1]):
            continue
        files.append(str(p.relative_to(root)))
    return files


# Curated list for the original terminus_2 harness.
_TERMINUS2_FILES = [
    "terminus_2.py",
    "tmux_session.py",
    "terminus_json_plain_parser.py",
    "terminus_xml_plain_parser.py",
    "asciinema_handler.py",
    "__init__.py",
]

if os.environ.get("HANDBOOK_SOURCE_ROOT"):
    HARNESS_FILES = _discover_harness_files(HARNESS_DIR)
else:
    HARNESS_FILES = _TERMINUS2_FILES


# ---------- data classes ----------


@dataclass
class FunctionNode:
    id: str  # e.g. "terminus_2.Terminus2._run_agent_loop"
    name: str
    qualname: str  # e.g. "Terminus2._run_agent_loop"
    file: str  # relative to HARNESS_DIR
    line_start: int
    line_end: int
    signature: str
    is_async: bool
    is_method: bool
    class_name: Optional[str]
    decorators: list[str]
    kind: str = "internal"
    # Set to True for nodes that were not directly extracted from a `def` in
    # the source — e.g. @dataclass classes whose __init__ is generated at
    # runtime. These appear as edge targets so we synthesize a node, but
    # callers should not try to read line_start/line_end as exact source
    # locations (we set them to 0).
    synthetic: bool = False
    used_self_attrs_read: list[str] = field(default_factory=list)
    used_self_attrs_written: list[str] = field(default_factory=list)
    # param_name -> resolved type name (bare class name or qualified module path).
    # Used by CallExtractor to resolve `param.method()` calls inside the body.
    params_types: dict[str, str] = field(default_factory=dict)


@dataclass
class BoundaryNode:
    id: str  # "boundary:<qualname>"
    name: str        # leaf segment, e.g. "call", "Step", "search"
    qualname: str    # full dotted path, e.g. "harbor.llms.base.BaseLLM.call"
    module: str      # package/module path only (without trailing class)
    class_name: str  # owning class if it's a method, else ""
    kind: str = "boundary"


@dataclass
class CallEdge:
    caller_id: str
    callee_id: str
    is_await: bool
    call_type: str  # self_method | self_attr_method | internal_func |
    # internal_constructor | boundary | boundary_constructor |
    # unresolved
    line: int
    raw: str  # source text of the call expression head


# ---------- module scanning ----------


class ModuleScanner(ast.NodeVisitor):
    """First pass: collect functions, classes, imports, self-attr type hints
    from one module."""

    def __init__(self, file_relative: str):
        self.file = file_relative
        # module_id used for namespacing internal nodes
        self.module_id = file_relative.removesuffix(".py")
        # local_name -> fully qualified module path (e.g., "Chat" -> "harbor.llms.chat.Chat")
        self.imports: dict[str, str] = {}
        # function_id -> local imports introduced inside that function's body
        # (e.g., `from xml.etree.ElementTree import Element` inside _build_skills_section).
        # These shadow module-level imports for that function only.
        self.local_imports: dict[str, dict[str, str]] = {}
        # class_name -> set of method names defined directly on that class
        self.class_methods: dict[str, set[str]] = defaultdict(set)
        # set of bare module-level function names defined in this module
        self.module_functions: set[str] = set()
        # set of class names defined in this module
        self.module_classes: set[str] = set()
        # FunctionNode list
        self.functions: list[FunctionNode] = []
        # (class_name, attr_name) -> resolved type name (qualified or local)
        self.self_attr_types: dict[tuple[str, str], str] = {}
        # (class_name, method_name) -> unparsed return annotation
        self._method_returns: dict[tuple[str, str], str] = {}
        # (class_name, fn_node) — processed after the whole module is visited
        self._pending_attr_learn: list[tuple[str, ast.AST]] = []
        # internal traversal context
        self._class_stack: list[str] = []
        self._func_stack: list[str] = []

    def finalize(self) -> None:
        """Run after .visit(tree). Learn self-attr types now that all method
        return annotations are known."""
        for class_name, fn in self._pending_attr_learn:
            self._learn_self_attr_types(fn, class_name)

    # ---- imports ----

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            self.imports[local] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            local = alias.asname or alias.name
            self.imports[local] = f"{module}.{alias.name}" if module else alias.name

    # ---- classes / functions ----

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.module_classes.add(node.name)
        for child in node.body:
            self.visit(child)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_function(node, is_async=True)

    def _record_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_async: bool
    ) -> None:
        class_name = self._class_stack[-1] if self._class_stack else None
        nesting = ".".join(self._func_stack)

        # Compute qualname. Four cases:
        #   - method inside a class:          Class.method
        #   - function nested inside a method: Class.outer.inner
        #   - function nested inside a top-level function: outer.inner
        #   - top-level free function:         func
        if class_name and nesting:
            qualname = f"{class_name}.{nesting}.{node.name}"
        elif class_name:
            qualname = f"{class_name}.{node.name}"
        elif nesting:
            qualname = f"{nesting}.{node.name}"
        else:
            qualname = node.name
        node_id = f"{self.module_id}.{qualname}"

        # Decorators -> string form
        decorators = [self._unparse(d) for d in node.decorator_list]

        # Signature (args + return annotation if present)
        try:
            args_src = ast.unparse(node.args)
        except Exception:
            args_src = "..."
        returns = (
            f" -> {self._unparse(node.returns)}" if getattr(node, "returns", None) else ""
        )
        prefix = "async def " if is_async else "def "
        signature = f"{prefix}{node.name}({args_src}){returns}"

        # End line (Python 3.8+ end_lineno).
        line_end = getattr(node, "end_lineno", node.lineno)

        # is_method: direct instance method of a class.
        # Nested functions inside methods are NOT methods of the enclosing class.
        is_method = (
            class_name is not None
            and not nesting
            and not self._is_staticmethod(decorators)
        )

        # Track self-attr reads/writes (skips nested function bodies internally).
        sa = _SelfAttrTracker()
        sa.scan(node)

        # Collect param types from annotations: `session: TmuxSession` makes
        # `session.X()` resolvable in this function's body.
        params_types = self._collect_param_types(node)

        fn = FunctionNode(
            id=node_id,
            name=node.name,
            qualname=qualname,
            file=self.file,
            line_start=node.lineno,
            line_end=line_end,
            signature=signature,
            is_async=is_async,
            is_method=is_method,
            class_name=class_name,
            decorators=decorators,
            used_self_attrs_read=sorted(sa.reads),
            used_self_attrs_written=sorted(sa.writes),
            params_types=params_types,
        )
        self.functions.append(fn)

        # Register membership only for direct class methods / top-level functions.
        # A function nested inside a method is neither (it's a closure).
        if class_name and not nesting:
            self.class_methods[class_name].add(node.name)
            ret_ann = self._unparse(node.returns) if node.returns else ""
            self._method_returns[(class_name, node.name)] = ret_ann
            # Defer self-attr type learning until *after* the whole module is
            # scanned, so return annotations from later-defined methods are
            # visible.
            self._pending_attr_learn.append((class_name, node))
        elif not class_name and not nesting:
            self.module_functions.add(node.name)

        # Collect imports introduced inside this function's body. They shadow
        # module-level imports for this function only. (e.g. `from
        # xml.etree.ElementTree import Element` inside `_build_skills_section`.)
        local_imp = self._collect_local_imports(node)
        if local_imp:
            self.local_imports[node_id] = local_imp

        # Descend into nested function defs only. Nested functions are
        # discovered through `_visit_for_nested`, not via NodeVisitor's
        # generic_visit (which we don't call here).
        self._func_stack.append(node.name)
        for child in node.body:
            self._visit_for_nested(child)
        self._func_stack.pop()

    def _visit_for_nested(self, node: ast.AST) -> None:
        """Recursively descend looking only for nested function defs."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._record_function(node, is_async=isinstance(node, ast.AsyncFunctionDef))
            return
        for child in ast.iter_child_nodes(node):
            self._visit_for_nested(child)

    def _collect_local_imports(
        self, fn_node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> dict[str, str]:
        """Walk the function body for Import / ImportFrom nodes.

        We descend into compound statements (if/try/with/for/while) but stop
        at nested function and lambda bodies — those have their own scope.
        """
        result: dict[str, str] = {}

        def walk(node: ast.AST) -> None:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            ):
                return
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name
                    result[local] = alias.name
                return
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    result[local] = (
                        f"{module}.{alias.name}" if module else alias.name
                    )
                return
            for child in ast.iter_child_nodes(node):
                walk(child)

        for stmt in fn_node.body:
            walk(stmt)
        return result

    # Generic stdlib types whose methods (str.find, dict.get, list.append, …)
    # don't carry meaningful harness-collaboration signal. Skipping these in
    # param inference keeps `response.find(...)`-style calls out of the graph.
    _GENERIC_BUILTIN_TYPES = frozenset({
        "str", "int", "float", "bool", "complex", "bytes", "bytearray",
        "list", "dict", "tuple", "set", "frozenset",
        "object", "type", "None", "Any",
    })

    def _collect_param_types(
        self, fn_node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> dict[str, str]:
        """For each annotated parameter, resolve its type name via imports.

        Strips `Optional[X]` / `X | None` to inner X. Drops generic containers
        (`list[X]`, `dict[K,V]`) and generic builtin types (str/int/...) since
        their methods don't help our call index.
        """
        result: dict[str, str] = {}
        args = fn_node.args
        all_args = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        if args.vararg is not None:
            all_args.append(args.vararg)
        if args.kwarg is not None:
            all_args.append(args.kwarg)
        for arg in all_args:
            if arg.annotation is None or arg.arg == "self":
                continue
            type_name = self._extract_type_from_annotation(arg.annotation)
            if not type_name:
                continue
            bare = type_name.rsplit(".", 1)[-1] if "." in type_name else type_name
            if bare in self._GENERIC_BUILTIN_TYPES:
                continue
            resolved = self.imports.get(type_name, type_name)
            result[arg.arg] = resolved
        return result

    @staticmethod
    def _extract_type_from_annotation(ann: ast.AST) -> str | None:
        """Pull a single bare type name out of an annotation expression.

        Handles:
          - Name("Foo")                 -> "Foo"
          - Attribute(Name("m"), "Foo") -> "m.Foo"
          - BinOp X | None              -> recurse into X
          - Subscript Optional[X]       -> recurse into X
        Returns None for generic containers, Union with multiple non-None
        members, and unsupported shapes.
        """
        if isinstance(ann, ast.Name):
            return ann.id
        if isinstance(ann, ast.Attribute):
            try:
                return ast.unparse(ann)
            except Exception:
                return None
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            # X | None or None | X (or X | Y, take the first non-None)
            for side in (ann.left, ann.right):
                if isinstance(side, ast.Constant) and side.value is None:
                    continue
                inner = ModuleScanner._extract_type_from_annotation(side)
                if inner is not None:
                    return inner
            return None
        if isinstance(ann, ast.Subscript):
            base = ann.value
            base_name = (
                base.id if isinstance(base, ast.Name)
                else (ast.unparse(base) if isinstance(base, ast.Attribute) else None)
            )
            # Optional[X] -> X
            if base_name in ("Optional", "typing.Optional"):
                return ModuleScanner._extract_type_from_annotation(ann.slice)
            # Generic containers: don't extract (methods like list.append aren't useful)
            return None
        return None

    # ---- helpers ----

    @staticmethod
    def _is_staticmethod(decorators: list[str]) -> bool:
        return any(d == "staticmethod" or d.startswith("staticmethod") for d in decorators)

    @staticmethod
    def _unparse(node: ast.AST | None) -> str:
        if node is None:
            return ""
        try:
            return ast.unparse(node)
        except Exception:
            return "<unparse-failed>"

    def _learn_self_attr_types(
        self, fn: ast.FunctionDef | ast.AsyncFunctionDef, class_name: str
    ) -> None:
        """Learn `self.<attr>` types from assignments inside *fn*.

        Two patterns are recognized:
          (a) `self.X = ClassName(...)` -> X has type ClassName
          (b) `self.X = self.method()` and `method` has a `-> Type` annotation
              -> X has type Type

        First-write wins: if X has already been typed, don't overwrite. This
        lets `__init__` win over a re-assignment in another method, which
        matches how Terminus 2 is actually structured.
        """
        for stmt in ast.walk(fn):
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            tgt = stmt.targets[0]
            if not (
                isinstance(tgt, ast.Attribute)
                and isinstance(tgt.value, ast.Name)
                and tgt.value.id == "self"
            ):
                continue
            attr_name = tgt.attr
            key = (class_name, attr_name)
            if key in self.self_attr_types:
                continue  # first-write wins

            if not isinstance(stmt.value, ast.Call):
                continue
            rhs_func = stmt.value.func

            # Pattern (a): direct constructor — ClassName(...)
            if isinstance(rhs_func, ast.Name):
                type_name = rhs_func.id
                bare = type_name.rsplit(".", 1)[-1] if "." in type_name else type_name
                if bare in self._GENERIC_BUILTIN_TYPES:
                    continue
                resolved = self.imports.get(type_name, type_name)
                self.self_attr_types[key] = resolved
                continue

            # Pattern (b): self.method() — look up return annotation
            if (
                isinstance(rhs_func, ast.Attribute)
                and isinstance(rhs_func.value, ast.Name)
                and rhs_func.value.id == "self"
            ):
                method_name = rhs_func.attr
                ret_ann = self._method_returns.get((class_name, method_name), "")
                if not ret_ann:
                    continue
                # Strip "| None" so X | None -> X; keep dotted names intact.
                bare = ret_ann.replace(" | None", "").strip()
                # Reject anything that isn't a plain `Foo` or `mod.Foo` —
                # generic containers like `list[Step]`, tuples, union types
                # don't yield useful method-resolution.
                if not bare or any(ch in bare for ch in "[]|, "):
                    continue
                last = bare.rsplit(".", 1)[-1]
                if last in self._GENERIC_BUILTIN_TYPES:
                    continue
                resolved = self.imports.get(bare, bare)
                self.self_attr_types[key] = resolved


class _SelfAttrTracker(ast.NodeVisitor):
    """Walks a function body, recording reads/writes of self.<attr>."""

    def __init__(self) -> None:
        self.reads: set[str] = set()
        self.writes: set[str] = set()

    def scan(self, fn_node: ast.AST) -> None:
        # Skip nested function defs so we only get *this* function's usage
        for child in ast.iter_child_nodes(fn_node):
            self._walk(child)

    def _walk(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return  # don't descend into nested defs
        self.visit(node)
        for c in ast.iter_child_nodes(node):
            self._walk(c)

    def visit_Assign(self, node: ast.Assign) -> None:
        for tgt in node.targets:
            self._record_write(tgt)
        # Don't descend manually; the walker handles children

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_write(node.target)
        if (
            isinstance(node.target, ast.Attribute)
            and isinstance(node.target.value, ast.Name)
            and node.target.value.id == "self"
        ):
            self.reads.add(node.target.attr)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._record_write(node.target)

    def _record_write(self, tgt: ast.AST) -> None:
        if (
            isinstance(tgt, ast.Attribute)
            and isinstance(tgt.value, ast.Name)
            and tgt.value.id == "self"
        ):
            self.writes.add(tgt.attr)
        elif isinstance(tgt, (ast.Tuple, ast.List)):
            for elt in tgt.elts:
                self._record_write(elt)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # plain read of self.X
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            # Don't double-record assignments here; only count reads
            # (writes are caught in visit_Assign). To avoid recording writes
            # as reads, we skip Attribute nodes whose ctx is Store.
            if not isinstance(getattr(node, "ctx", None), ast.Store):
                self.reads.add(node.attr)


# ---------- call extraction ----------


class CallExtractor:
    """Resolves Call nodes within each function body using context from all
    scanned modules + the cross-module self-attr-type map."""

    def __init__(
        self,
        modules: dict[str, ModuleScanner],
        self_attr_types: dict[tuple[str, str], str],
    ):
        self.modules = modules
        self.self_attr_types = self_attr_types
        # Cross-module lookup: class_name -> (module_id, full_id_prefix)
        self.class_to_module: dict[str, str] = {}
        for mod_id, mod in modules.items():
            for cls in mod.module_classes:
                self.class_to_module[cls] = mod_id

    def extract(self, fn: FunctionNode) -> list[CallEdge]:
        mod = self._module_for(fn.file)
        source_tree = _function_ast_index[(fn.file, fn.line_start, fn.name)]
        edges: list[CallEdge] = []
        for parent, node, is_await in _iter_calls(source_tree):
            edge = self._resolve_call(node, is_await, fn, mod)
            if edge is not None:
                edges.append(edge)
        return edges

    def _module_for(self, file_rel: str) -> ModuleScanner:
        mod_id = file_rel.removesuffix(".py")
        return self.modules[mod_id]

    def _resolve_call(
        self,
        node: ast.Call,
        is_await: bool,
        caller: FunctionNode,
        mod: ModuleScanner,
    ) -> Optional[CallEdge]:
        func = node.func
        line = node.lineno
        raw = self._safe_unparse(func)

        # Combined import lookup: function-local imports shadow module-level
        # imports for this caller. (Local imports were collected per-function
        # during the scan phase.)
        local_imp = mod.local_imports.get(caller.id, {})
        imports = {**mod.imports, **local_imp}

        # Case A: bare Name() — local function, import, or class constructor
        if isinstance(func, ast.Name):
            name = func.id
            # 1) constructor / function in same module?
            if name in mod.module_classes:
                callee_id = f"{mod.module_id}.{name}.__init__"
                return CallEdge(caller.id, callee_id, is_await, "internal_constructor", line, raw)
            if name in mod.module_functions:
                callee_id = f"{mod.module_id}.{name}"
                return CallEdge(caller.id, callee_id, is_await, "internal_func", line, raw)
            # 2) imported (local imports shadow module-level)?
            if name in imports:
                qual = imports[name]
                # Distinguish class constructor (heuristic: capitalized last segment)
                last = qual.rsplit(".", 1)[-1]
                if last and last[0].isupper():
                    # Could still be a constructor in another harness file → internal?
                    internal_class_mod = self.class_to_module.get(last)
                    if internal_class_mod and internal_class_mod != mod.module_id:
                        return CallEdge(
                            caller.id,
                            f"{internal_class_mod}.{last}.__init__",
                            is_await,
                            "internal_constructor",
                            line,
                            raw,
                        )
                    return CallEdge(
                        caller.id, f"boundary:{qual}", is_await, "boundary_constructor", line, raw
                    )
                # Imported function or symbol
                # Check if it points to another harness module's function
                if qual.split(".")[0] in {m.removesuffix(".py") for m in HARNESS_FILES}:
                    # e.g. "tmux_session.something" — internal cross-module
                    return CallEdge(caller.id, qual, is_await, "internal_func", line, raw)
                return CallEdge(caller.id, f"boundary:{qual}", is_await, "boundary", line, raw)
            # 3) builtins / unresolved
            return CallEdge(caller.id, f"unresolved:{name}", is_await, "unresolved", line, raw)

        # Case B: Attribute access — self.foo(), self.attr.foo(), Mod.foo()
        if isinstance(func, ast.Attribute):
            attr = func.attr
            base = func.value

            # B1: self.foo()
            if isinstance(base, ast.Name) and base.id == "self" and caller.class_name:
                cls = caller.class_name
                # Resolve in same class first
                if attr in mod.class_methods.get(cls, set()):
                    return CallEdge(
                        caller.id,
                        f"{mod.module_id}.{cls}.{attr}",
                        is_await,
                        "self_method",
                        line,
                        raw,
                    )
                # Possibly an inherited method we can't see — mark unresolved with hint
                return CallEdge(
                    caller.id,
                    f"unresolved:self.{attr}",
                    is_await,
                    "unresolved",
                    line,
                    raw,
                )

            # B2: self._attr.foo()
            if (
                isinstance(base, ast.Attribute)
                and isinstance(base.value, ast.Name)
                and base.value.id == "self"
                and caller.class_name
            ):
                outer_attr = base.attr
                type_name = self.self_attr_types.get((caller.class_name, outer_attr))
                if type_name:
                    # class_to_module is indexed by bare class name. type_name
                    # may be fully qualified (e.g. "harbor.agents.terminus_2.
                    # tmux_session.TmuxSession"), so look up by the last segment.
                    bare = (
                        type_name.rsplit(".", 1)[-1] if "." in type_name else type_name
                    )
                    target_mod = self.class_to_module.get(bare)
                    if target_mod:
                        return CallEdge(
                            caller.id,
                            f"{target_mod}.{bare}.{attr}",
                            is_await,
                            "self_attr_method",
                            line,
                            raw,
                        )
                    # External (boundary) type
                    return CallEdge(
                        caller.id,
                        f"boundary:{type_name}.{attr}",
                        is_await,
                        "boundary",
                        line,
                        raw,
                    )
                return CallEdge(
                    caller.id,
                    f"unresolved:self.{outer_attr}.{attr}",
                    is_await,
                    "unresolved",
                    line,
                    raw,
                )

            # B3: SomeModule.foo() / SomeClass.foo() / param.foo()
            if isinstance(base, ast.Name):
                base_name = base.id

                # B3a: parameter with known type — `session.send_keys()` where
                # the surrounding def has `session: TmuxSession`.
                param_type = caller.params_types.get(base_name)
                if param_type:
                    bare = (
                        param_type.rsplit(".", 1)[-1] if "." in param_type else param_type
                    )
                    target_mod = self.class_to_module.get(bare)
                    if target_mod:
                        return CallEdge(
                            caller.id,
                            f"{target_mod}.{bare}.{attr}",
                            is_await,
                            "param_method",
                            line,
                            raw,
                        )
                    return CallEdge(
                        caller.id,
                        f"boundary:{param_type}.{attr}",
                        is_await,
                        "boundary",
                        line,
                        raw,
                    )

                # B3b: imported name (module or class)
                if base_name in imports:
                    qual = imports[base_name]
                    # If it's an internal harness class, route internally
                    if base_name in self.class_to_module:
                        target_mod = self.class_to_module[base_name]
                        return CallEdge(
                            caller.id,
                            f"{target_mod}.{base_name}.{attr}",
                            is_await,
                            "internal_func",
                            line,
                            raw,
                        )
                    return CallEdge(
                        caller.id,
                        f"boundary:{qual}.{attr}",
                        is_await,
                        "boundary",
                        line,
                        raw,
                    )

                # Could be a local variable; unresolved
                return CallEdge(
                    caller.id,
                    f"unresolved:{base_name}.{attr}",
                    is_await,
                    "unresolved",
                    line,
                    raw,
                )

        # Case C: anything else (chained, subscripted, etc.)
        return CallEdge(
            caller.id,
            f"unresolved:{raw}",
            is_await,
            "unresolved",
            line,
            raw,
        )

    @staticmethod
    def _safe_unparse(node: ast.AST) -> str:
        try:
            src = ast.unparse(node)
            # Keep raw short
            return src if len(src) <= 80 else src[:77] + "..."
        except Exception:
            return "<unparse-failed>"


# ---------- ast helpers ----------


# Cached map from (file, line_start, name) -> ast.FunctionDef node.
# Built lazily during the second pass so call extraction has access to the
# exact subtree to traverse.
_function_ast_index: dict[tuple[str, int, str], ast.AST] = {}


def _iter_calls(fn_node: ast.AST):
    """Yield (parent, ast.Call, is_await) for every Call within fn_node's body,
    excluding nested function bodies and decorator expressions."""

    def walk(node: ast.AST, parent: ast.AST, inside_await: bool):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if node is fn_node:
                # Don't yield calls in the *decorators* of fn_node itself;
                # decorators are handled separately (recorded as strings).
                # Don't descend into decorator_list, but DO descend into body/args.
                for child in node.args.defaults + node.args.kw_defaults:
                    if child is not None:
                        yield from walk(child, node, False)
                for child in node.body:
                    yield from walk(child, node, False)
            # Otherwise: skip nested function bodies entirely
            return
        if isinstance(node, ast.Await):
            for child in ast.iter_child_nodes(node):
                yield from walk(child, node, True)
            return
        if isinstance(node, ast.Call):
            yield parent, node, inside_await
            for child in ast.iter_child_nodes(node):
                yield from walk(child, node, False)
            return
        for child in ast.iter_child_nodes(node):
            yield from walk(child, node, inside_await)

    yield from walk(fn_node, fn_node, False)


# ---------- pipeline ----------


def scan_all_modules() -> dict[str, ModuleScanner]:
    modules: dict[str, ModuleScanner] = {}
    for fname in HARNESS_FILES:
        path = HARNESS_DIR / fname
        if not path.exists():
            print(f"[skip] {fname} not found")
            continue
        scanner = ModuleScanner(file_relative=fname)
        tree = ast.parse(path.read_text())
        scanner.visit(tree)
        scanner.finalize()
        modules[scanner.module_id] = scanner
        # Index every function for the call-extraction pass
        _index_function_asts(tree, fname)
    return modules


def _index_function_asts(tree: ast.AST, file_relative: str) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _function_ast_index[(file_relative, node.lineno, node.name)] = node


def merge_self_attr_types(
    modules: dict[str, ModuleScanner],
) -> dict[tuple[str, str], str]:
    merged: dict[tuple[str, str], str] = {}
    for mod in modules.values():
        merged.update(mod.self_attr_types)
    return merged


def collect_all_functions(modules: dict[str, ModuleScanner]) -> list[FunctionNode]:
    funcs: list[FunctionNode] = []
    for mod in modules.values():
        funcs.extend(mod.functions)
    return funcs


def extract_all_edges(
    modules: dict[str, ModuleScanner],
    all_functions: list[FunctionNode],
    self_attr_types: dict[tuple[str, str], str],
) -> list[CallEdge]:
    extractor = CallExtractor(modules, self_attr_types)
    edges: list[CallEdge] = []
    for fn in all_functions:
        try:
            edges.extend(extractor.extract(fn))
        except KeyError:
            # Function not indexed (e.g., nested function inside list comp); skip
            continue
    return edges


# Bare builtin names used by `categorize_dropped` to bucket unresolved edges.
# Not exhaustive — just the offenders we observe in Terminus 2.
_BUILTIN_NAMES = {
    "len", "isinstance", "range", "enumerate", "zip", "list", "dict", "tuple",
    "set", "str", "int", "float", "bool", "type", "id", "print", "repr",
    "min", "max", "sum", "abs", "any", "all", "sorted", "reversed",
    "getattr", "setattr", "hasattr", "callable",
    "open", "iter", "next", "map", "filter",
    "RuntimeError", "ValueError", "TypeError", "TimeoutError", "Exception",
    "KeyError", "IndexError", "StopIteration", "NotImplementedError",
}


# ---------- edge partitioning ----------


def categorize_dropped(callee_id: str) -> str:
    """Categorize an unresolved edge by why we couldn't name its target.

    Categories (each Phase 2 may want to revisit a different way):
      - inherited_method: self.logger.X / self._logger.X (likely from BaseAgent)
      - self_attr_unknown: self._other.X (we know the attr exists but not its type)
      - string_literal_method: '...'.X(...) calls (str methods on a literal)
      - builtin: bare names that are builtin (len, isinstance, exceptions)
      - local_var_method: local_var.X(...) where local_var has no annotation
      - bare_name: bare names we couldn't tie to any function (closures, super, etc.)
    """
    name = callee_id.removeprefix("unresolved:")

    if name.startswith("self."):
        # self.logger.X / self._logger.X are almost always inherited from BaseAgent
        seg = name.split(".", 2)
        if len(seg) >= 2 and seg[1] in ("logger", "_logger"):
            return "inherited_method"
        return "self_attr_unknown"

    if name.startswith("'") or name.startswith('"'):
        return "string_literal_method"

    head = name.split(".")[0].split("(")[0].strip()
    if head in _BUILTIN_NAMES:
        return "builtin"

    if "." in name:
        return "local_var_method"

    return "bare_name"


def partition_edges(
    edges: list[CallEdge],
) -> tuple[list[CallEdge], list[CallEdge]]:
    """Split edges into (kept, dropped).

    Drop policy: any edge whose callee was not resolvable to a named function
    (call_type == 'unresolved'). Everything else is kept.

    Notes:
      - boundary edges to stdlib modules (re.search, asyncio.sleep, etc.) are
        kept — they have a real qualified name and represent meaningful
        cross-boundary dependencies.
      - boundary edges to bare builtins (len, isinstance) cannot exist under
        the current resolution rules — `len` is never imported, so it falls
        to unresolved.
    """
    kept: list[CallEdge] = []
    dropped: list[CallEdge] = []
    for e in edges:
        if e.call_type == "unresolved":
            dropped.append(e)
        else:
            kept.append(e)
    return kept, dropped


def _split_boundary_qualname(qual: str) -> tuple[str, str, str]:
    """Split a boundary qualname into (module_path, class_name, leaf_name).

    Heuristic: the first dotted segment that starts with a capital AND is
    not the final segment is treated as the class boundary. Everything
    before it is the module, the segment itself is the class, anything
    after is the leaf method name.

    Examples:
      harbor.llms.base.BaseLLM.call  -> ("harbor.llms.base", "BaseLLM", "call")
      harbor.models.trajectories.Step -> ("harbor.models.trajectories", "", "Step")
      re.search                       -> ("re", "", "search")

    Caveat: well-known Python modules that happen to be capitalized (such
    as `xml.etree.ElementTree`) are misclassified as classes. Phase 2 can
    correct these manually.
    """
    parts = qual.split(".")
    if not parts:
        return "", "", qual
    for i, p in enumerate(parts[:-1]):
        if p and p[0].isupper():
            return ".".join(parts[:i]), p, ".".join(parts[i + 1:])
    return ".".join(parts[:-1]), "", parts[-1]


def _synthesize_init_node(ref_id: str, in_deg: int, out_deg: int) -> dict:
    """Create a placeholder node for `Module.Class.__init__` references whose
    Class has no explicit `def __init__` in the source (typically @dataclass
    classes whose __init__ is generated at runtime).

    The node carries `synthetic=True` so consumers know line_start/line_end
    are not meaningful; Phase 2 should grep for the class definition itself
    if they need an exact source location.
    """
    base = ref_id.removesuffix(".__init__")
    module_id, _, class_qual = base.partition(".")
    return {
        "id": ref_id,
        "name": "__init__",
        "qualname": f"{class_qual}.__init__",
        "file": f"{module_id}.py" if module_id else "",
        "line_start": 0,
        "line_end": 0,
        "signature": "def __init__(self, ...)  # synthesized (no explicit __init__ in source)",
        "is_async": False,
        "is_method": True,
        "class_name": class_qual,
        "decorators": [],
        "kind": "internal",
        "synthetic": True,
        "used_self_attrs_read": [],
        "used_self_attrs_written": [],
        "params_types": {},
        "n_callees": out_deg,
        "n_callers": in_deg,
    }


def build_node_table(
    functions: list[FunctionNode], edges: list[CallEdge]
) -> dict[str, dict]:
    """Build the node table from internal functions + boundary nodes referenced
    by the *kept* edges. Unresolved nodes are excluded entirely — they live in
    dropped_calls.json now. Internal classes referenced as constructors but
    without an explicit __init__ in source (e.g. @dataclass) get a synthetic
    node so edges aren't dangling."""
    nodes: dict[str, dict] = {}
    for fn in functions:
        nodes[fn.id] = asdict(fn)

    # Pre-compute degrees from kept edges only.
    caller_count: dict[str, int] = defaultdict(int)
    callee_count: dict[str, int] = defaultdict(int)
    for e in edges:
        caller_count[e.caller_id] += 1
        callee_count[e.callee_id] += 1

    for nid, n in nodes.items():
        n["n_callees"] = caller_count.get(nid, 0)  # outgoing
        n["n_callers"] = callee_count.get(nid, 0)  # incoming

    # Add nodes referenced by any kept edge that we haven't materialised yet.
    referenced_ids = {e.callee_id for e in edges} | {e.caller_id for e in edges}
    for ref_id in sorted(referenced_ids):
        if ref_id in nodes:
            continue

        if ref_id.startswith("boundary:"):
            qual = ref_id.removeprefix("boundary:")
            module, class_name, leaf = _split_boundary_qualname(qual)
            nodes[ref_id] = asdict(
                BoundaryNode(
                    id=ref_id,
                    name=leaf or qual,
                    qualname=qual,
                    module=module,
                    class_name=class_name,
                    kind="boundary",
                )
            )
            nodes[ref_id]["n_callees"] = caller_count.get(ref_id, 0)
            nodes[ref_id]["n_callers"] = callee_count.get(ref_id, 0)
            continue

        # Internal constructor target with no explicit __init__ (e.g. dataclass).
        # Synthesize a node so the edge isn't dangling.
        if ref_id.endswith(".__init__"):
            nodes[ref_id] = _synthesize_init_node(
                ref_id,
                in_deg=callee_count.get(ref_id, 0),
                out_deg=caller_count.get(ref_id, 0),
            )
            continue

        # Note: ref_id starting with "unresolved:" cannot appear here, since
        # we partition unresolved edges out before calling this function.
    return nodes


def build_self_attrs_index(modules: dict[str, ModuleScanner]) -> dict[str, dict]:
    """Per (class_name) -> {attr_name: {read_in: [fn ids], written_in: [fn ids]}}"""
    index: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {"read_in": [], "written_in": []}))
    for mod in modules.values():
        for fn in mod.functions:
            if not fn.class_name:
                continue
            for attr in fn.used_self_attrs_read:
                index[fn.class_name][attr]["read_in"].append(fn.id)
            for attr in fn.used_self_attrs_written:
                index[fn.class_name][attr]["written_in"].append(fn.id)
    # convert default dicts -> plain dicts for json
    return {cls: {a: dict(v) for a, v in attrs.items()} for cls, attrs in index.items()}


# ---------- emit ----------


def emit_dropped_log(dropped: list[CallEdge]) -> None:
    """Write dropped (unresolved) edges to dropped_calls.json, grouped by why
    we couldn't name them. Phase 2 review uses this to spot false drops."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for e in dropped:
        cat = categorize_dropped(e.callee_id)
        grouped[cat].append(
            {
                "caller": e.caller_id,
                "callee_raw": e.callee_id.removeprefix("unresolved:"),
                "is_await": e.is_await,
                "line": e.line,
                "raw": e.raw,
            }
        )

    summary = {cat: len(items) for cat, items in grouped.items()}
    out = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_dropped": len(dropped),
            "by_category": summary,
            "category_explanations": {
                "inherited_method": "self.logger.X / self._logger.X — callee is on a base class we don't scan.",
                "self_attr_unknown": "self._attr.X — we know the attr exists but don't know its type.",
                "string_literal_method": "'X'.method() — call on a string literal; no named callee.",
                "builtin": "len/isinstance/exception constructors — Python builtins, no module namespace.",
                "local_var_method": "var.X() — var is a local variable with no type annotation.",
                "bare_name": "X() — closures, super(), and other names not tied to any def.",
            },
        },
        "edges_by_category": dict(grouped),
    }
    (OUTPUT_DIR / "dropped_calls.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2)
    )


def emit_json(
    functions: list[FunctionNode],
    nodes: dict[str, dict],
    edges: list[CallEdge],
    self_attrs: dict[str, dict],
) -> None:
    out = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "harness_dir": str(HARNESS_DIR),
            "scanned_files": HARNESS_FILES,
            "n_internal_functions": sum(
                1 for n in nodes.values() if n.get("kind") == "internal"
            ),
            "n_boundary_nodes": sum(
                1 for n in nodes.values() if n.get("kind") == "boundary"
            ),
            "n_edges": len(edges),
            "policy": (
                "Edges are emitted only when the callee resolves to a named "
                "function (internal or boundary). Unresolved/builtin calls "
                "live in dropped_calls.json."
            ),
        },
        "nodes": nodes,
        "edges": [asdict(e) for e in edges],
        "self_attrs": self_attrs,
    }
    (OUTPUT_DIR / "graph.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))


def emit_csv(functions: list[FunctionNode], nodes: dict[str, dict]) -> None:
    path = OUTPUT_DIR / "functions.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "id",
                "name",
                "qualname",
                "file",
                "line_start",
                "line_end",
                "class",
                "is_async",
                "is_method",
                "decorators",
                "signature",
                "n_callers",
                "n_callees",
                "n_self_attrs_read",
                "n_self_attrs_written",
                # blank columns for Phase 2 manual annotation
                "unit_id",
                "unit_name",
                "responsibility",
            ]
        )
        for fn in functions:
            n = nodes.get(fn.id, {})
            w.writerow(
                [
                    fn.id,
                    fn.name,
                    fn.qualname,
                    fn.file,
                    fn.line_start,
                    fn.line_end,
                    fn.class_name or "",
                    "true" if fn.is_async else "false",
                    "true" if fn.is_method else "false",
                    "|".join(fn.decorators),
                    fn.signature,
                    n.get("n_callers", 0),
                    n.get("n_callees", 0),
                    len(fn.used_self_attrs_read),
                    len(fn.used_self_attrs_written),
                    "",
                    "",
                    "",
                ]
            )


def emit_dot(nodes: dict[str, dict], edges: list[CallEdge]) -> None:
    """Graphviz output: subgraph per file for internal nodes, single sink for boundary."""
    lines: list[str] = []
    lines.append("digraph terminus2_callgraph {")
    lines.append('  rankdir=LR;')
    lines.append('  node [shape=box, style=rounded, fontname="Helvetica", fontsize=9];')
    lines.append('  edge [fontname="Helvetica", fontsize=8];')

    # group internal nodes by file; boundary nodes go to a single cluster
    by_file: dict[str, list[dict]] = defaultdict(list)
    boundary_nodes: list[dict] = []
    for n in nodes.values():
        if n.get("kind") == "internal":
            by_file[n["file"]].append(n)
        elif n.get("kind") == "boundary":
            boundary_nodes.append(n)
        # unresolved nodes don't exist in the kept graph anymore

    for i, (fname, ns) in enumerate(sorted(by_file.items())):
        lines.append(f'  subgraph cluster_{i} {{')
        lines.append(f'    label="{fname}";')
        lines.append('    style=dashed; color=gray60;')
        for n in ns:
            label = n["qualname"].replace('"', '\\"')
            lines.append(
                f'    "{n["id"]}" [label="{label}", fillcolor="#e8f0fe", style="filled,rounded"];'
            )
        lines.append("  }")

    if boundary_nodes:
        lines.append("  subgraph cluster_boundary {")
        lines.append('    label="boundary"; style=dashed; color=gray60;')
        for n in boundary_nodes:
            label = n["qualname"].replace('"', '\\"')
            lines.append(
                f'    "{n["id"]}" [label="{label}", fillcolor="#f0f0f0", style="filled,rounded", color=gray40, fontcolor=gray30];'
            )
        lines.append("  }")

    # edges (only kept edges reach here)
    for e in edges:
        attrs = []
        if e.is_await:
            attrs.append('color="#1a73e8"')
        if e.call_type == "boundary" or e.call_type == "boundary_constructor":
            attrs.append('style=dashed')
        attr_str = f" [{', '.join(attrs)}]" if attrs else ""
        lines.append(f'  "{e.caller_id}" -> "{e.callee_id}"{attr_str};')

    lines.append("}")
    (OUTPUT_DIR / "graph.dot").write_text("\n".join(lines))


# ---------- main ----------


def main() -> None:
    print(f"[scan] {HARNESS_DIR}")
    modules = scan_all_modules()
    print(f"[scan] {len(modules)} modules, files: {sorted(modules.keys())}")

    all_functions = collect_all_functions(modules)
    print(f"[scan] {len(all_functions)} functions/methods")

    self_attr_types = merge_self_attr_types(modules)
    print(f"[scan] {len(self_attr_types)} self-attr type bindings recovered")
    n_params = sum(len(fn.params_types) for fn in all_functions)
    print(f"[scan] {n_params} typed parameter bindings recovered")

    all_edges = extract_all_edges(modules, all_functions, self_attr_types)
    print(f"[extract] {len(all_edges)} call edges (all)")

    kept_edges, dropped_edges = partition_edges(all_edges)
    print(f"[partition] kept={len(kept_edges)}  dropped={len(dropped_edges)}")

    # Show kept edges by type
    type_counts: dict[str, int] = defaultdict(int)
    for e in kept_edges:
        type_counts[e.call_type] += 1
    type_summary = "  ".join(f"{t}={n}" for t, n in sorted(type_counts.items()))
    print(f"[partition] {type_summary}")

    # Show dropped edges by category
    drop_counts: dict[str, int] = defaultdict(int)
    for e in dropped_edges:
        drop_counts[categorize_dropped(e.callee_id)] += 1
    drop_summary = "  ".join(f"{c}={n}" for c, n in sorted(drop_counts.items()))
    print(f"[dropped] {drop_summary}")

    nodes = build_node_table(all_functions, kept_edges)
    n_int = sum(1 for n in nodes.values() if n.get("kind") == "internal")
    n_bnd = sum(1 for n in nodes.values() if n.get("kind") == "boundary")
    print(f"[graph] internal={n_int}  boundary={n_bnd}")

    self_attrs = build_self_attrs_index(modules)
    print(f"[graph] classes with tracked self-attrs: {len(self_attrs)}")

    emit_json(all_functions, nodes, kept_edges, self_attrs)
    emit_csv(all_functions, nodes)
    emit_dot(nodes, kept_edges)
    emit_dropped_log(dropped_edges)
    print(f"[done] outputs in {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
