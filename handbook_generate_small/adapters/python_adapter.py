"""Python language adapter.

Re-implements the legacy `handbook_generate/phase1/extract_graph.py` scanner +
call resolver behind the uniform `LanguageAdapter` interface, with two changes
only:

  1. No hardcoded HARNESS_DIR / HARNESS_FILES — files and source_root are
     passed in.
  2. The global `_function_ast_index` and the `HARNESS_FILES` cross-module
     check become per-adapter instance state (`internal_module_ids`).

The actual scanning/resolution semantics (self-attr typing, param typing,
cross-module resolution, bare-name heuristics) are a faithful copy of the
legacy code so the Python path produces a byte-compatible graph.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from typing import Optional

from base import LanguageAdapter, register
from ir import CallEdge, FunctionNode, ModuleAnalysis


# ---------- module scanning ----------


class _ModuleScanner(ast.NodeVisitor):
    """First pass: collect functions, classes, imports, self-attr type hints."""

    _GENERIC_BUILTIN_TYPES = frozenset({
        "str", "int", "float", "bool", "complex", "bytes", "bytearray",
        "list", "dict", "tuple", "set", "frozenset",
        "object", "type", "None", "Any",
    })

    def __init__(self, file_relative: str):
        self.file = file_relative
        self.module_id = file_relative[:-3] if file_relative.endswith(".py") else file_relative
        self.imports: dict[str, str] = {}
        self.local_imports: dict[str, dict[str, str]] = {}
        self.class_methods: dict[str, set[str]] = defaultdict(set)
        self.module_functions: set[str] = set()
        self.module_classes: set[str] = set()
        self.functions: list[FunctionNode] = []
        self.self_attr_types: dict[tuple[str, str], str] = {}
        self._method_returns: dict[tuple[str, str], str] = {}
        self._pending_attr_learn: list[tuple[str, ast.AST]] = []
        self._class_stack: list[str] = []
        self._func_stack: list[str] = []

    def finalize(self) -> None:
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

    def _record_function(self, node, *, is_async: bool) -> None:
        class_name = self._class_stack[-1] if self._class_stack else None
        nesting = ".".join(self._func_stack)

        if class_name and nesting:
            qualname = f"{class_name}.{nesting}.{node.name}"
        elif class_name:
            qualname = f"{class_name}.{node.name}"
        elif nesting:
            qualname = f"{nesting}.{node.name}"
        else:
            qualname = node.name
        node_id = f"{self.module_id}.{qualname}"

        decorators = [self._unparse(d) for d in node.decorator_list]

        try:
            args_src = ast.unparse(node.args)
        except Exception:
            args_src = "..."
        returns = (
            f" -> {self._unparse(node.returns)}" if getattr(node, "returns", None) else ""
        )
        prefix = "async def " if is_async else "def "
        signature = f"{prefix}{node.name}({args_src}){returns}"

        line_end = getattr(node, "end_lineno", node.lineno)

        is_method = (
            class_name is not None
            and not nesting
            and not self._is_staticmethod(decorators)
        )

        sa = _SelfAttrTracker()
        sa.scan(node)

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

        if class_name and not nesting:
            self.class_methods[class_name].add(node.name)
            ret_ann = self._unparse(node.returns) if node.returns else ""
            self._method_returns[(class_name, node.name)] = ret_ann
            self._pending_attr_learn.append((class_name, node))
        elif not class_name and not nesting:
            self.module_functions.add(node.name)

        local_imp = self._collect_local_imports(node)
        if local_imp:
            self.local_imports[node_id] = local_imp

        self._func_stack.append(node.name)
        for child in node.body:
            self._visit_for_nested(child)
        self._func_stack.pop()

    def _visit_for_nested(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._record_function(node, is_async=isinstance(node, ast.AsyncFunctionDef))
            return
        for child in ast.iter_child_nodes(node):
            self._visit_for_nested(child)

    def _collect_local_imports(self, fn_node) -> dict[str, str]:
        result: dict[str, str] = {}

        def walk(node: ast.AST) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
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
                    result[local] = f"{module}.{alias.name}" if module else alias.name
                return
            for child in ast.iter_child_nodes(node):
                walk(child)

        for stmt in fn_node.body:
            walk(stmt)
        return result

    def _collect_param_types(self, fn_node) -> dict[str, str]:
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
    def _extract_type_from_annotation(ann: ast.AST) -> Optional[str]:
        if isinstance(ann, ast.Name):
            return ann.id
        if isinstance(ann, ast.Attribute):
            try:
                return ast.unparse(ann)
            except Exception:
                return None
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            for side in (ann.left, ann.right):
                if isinstance(side, ast.Constant) and side.value is None:
                    continue
                inner = _ModuleScanner._extract_type_from_annotation(side)
                if inner is not None:
                    return inner
            return None
        if isinstance(ann, ast.Subscript):
            base = ann.value
            base_name = (
                base.id if isinstance(base, ast.Name)
                else (ast.unparse(base) if isinstance(base, ast.Attribute) else None)
            )
            if base_name in ("Optional", "typing.Optional"):
                return _ModuleScanner._extract_type_from_annotation(ann.slice)
            return None
        return None

    @staticmethod
    def _is_staticmethod(decorators: list[str]) -> bool:
        return any(d == "staticmethod" or d.startswith("staticmethod") for d in decorators)

    @staticmethod
    def _unparse(node: Optional[ast.AST]) -> str:
        if node is None:
            return ""
        try:
            return ast.unparse(node)
        except Exception:
            return "<unparse-failed>"

    def _learn_self_attr_types(self, fn, class_name: str) -> None:
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
                continue

            if not isinstance(stmt.value, ast.Call):
                continue
            rhs_func = stmt.value.func

            if isinstance(rhs_func, ast.Name):
                type_name = rhs_func.id
                bare = type_name.rsplit(".", 1)[-1] if "." in type_name else type_name
                if bare in self._GENERIC_BUILTIN_TYPES:
                    continue
                resolved = self.imports.get(type_name, type_name)
                self.self_attr_types[key] = resolved
                continue

            if (
                isinstance(rhs_func, ast.Attribute)
                and isinstance(rhs_func.value, ast.Name)
                and rhs_func.value.id == "self"
            ):
                method_name = rhs_func.attr
                ret_ann = self._method_returns.get((class_name, method_name), "")
                if not ret_ann:
                    continue
                bare = ret_ann.replace(" | None", "").strip()
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
        for child in ast.iter_child_nodes(fn_node):
            self._walk(child)

    def _walk(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        self.visit(node)
        for c in ast.iter_child_nodes(node):
            self._walk(c)

    def visit_Assign(self, node: ast.Assign) -> None:
        for tgt in node.targets:
            self._record_write(tgt)

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
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            if not isinstance(getattr(node, "ctx", None), ast.Store):
                self.reads.add(node.attr)


# ---------- call extraction ----------


class _CallExtractor:
    def __init__(self, modules, self_attr_types, internal_module_ids, function_ast_index):
        self.modules = modules
        self.self_attr_types = self_attr_types
        self.internal_module_ids = internal_module_ids
        self._function_ast_index = function_ast_index
        self.class_to_module: dict[str, str] = {}
        for mod_id, mod in modules.items():
            for cls in mod.module_classes:
                self.class_to_module[cls] = mod_id

    def extract(self, fn: FunctionNode) -> list[CallEdge]:
        mod = self._module_for(fn.file)
        source_tree = self._function_ast_index[(fn.file, fn.line_start, fn.name)]
        edges: list[CallEdge] = []
        for _parent, node, is_await in _iter_calls(source_tree):
            edge = self._resolve_call(node, is_await, fn, mod)
            if edge is not None:
                edges.append(edge)
        return edges

    def _module_for(self, file_rel: str):
        mod_id = file_rel[:-3] if file_rel.endswith(".py") else file_rel
        return self.modules[mod_id]

    def _resolve_call(self, node, is_await, caller, mod):
        func = node.func
        line = node.lineno
        raw = self._safe_unparse(func)

        local_imp = mod.local_imports.get(caller.id, {})
        imports = {**mod.imports, **local_imp}

        # Case A: bare Name()
        if isinstance(func, ast.Name):
            name = func.id
            if name in mod.module_classes:
                return CallEdge(caller.id, f"{mod.module_id}.{name}.__init__", is_await, "internal_constructor", line, raw)
            if name in mod.module_functions:
                return CallEdge(caller.id, f"{mod.module_id}.{name}", is_await, "internal_func", line, raw)
            if name in imports:
                qual = imports[name]
                last = qual.rsplit(".", 1)[-1]
                if last and last[0].isupper():
                    internal_class_mod = self.class_to_module.get(last)
                    if internal_class_mod and internal_class_mod != mod.module_id:
                        return CallEdge(caller.id, f"{internal_class_mod}.{last}.__init__", is_await, "internal_constructor", line, raw)
                    return CallEdge(caller.id, f"boundary:{qual}", is_await, "boundary_constructor", line, raw)
                if qual.split(".")[0] in self.internal_module_ids:
                    return CallEdge(caller.id, qual, is_await, "internal_func", line, raw)
                return CallEdge(caller.id, f"boundary:{qual}", is_await, "boundary", line, raw)
            return CallEdge(caller.id, f"unresolved:{name}", is_await, "unresolved", line, raw)

        # Case B: Attribute access
        if isinstance(func, ast.Attribute):
            attr = func.attr
            base = func.value

            # B1: self.foo()
            if isinstance(base, ast.Name) and base.id == "self" and caller.class_name:
                cls = caller.class_name
                if attr in mod.class_methods.get(cls, set()):
                    return CallEdge(caller.id, f"{mod.module_id}.{cls}.{attr}", is_await, "self_method", line, raw)
                return CallEdge(caller.id, f"unresolved:self.{attr}", is_await, "unresolved", line, raw)

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
                    bare = type_name.rsplit(".", 1)[-1] if "." in type_name else type_name
                    target_mod = self.class_to_module.get(bare)
                    if target_mod:
                        return CallEdge(caller.id, f"{target_mod}.{bare}.{attr}", is_await, "self_attr_method", line, raw)
                    return CallEdge(caller.id, f"boundary:{type_name}.{attr}", is_await, "boundary", line, raw)
                return CallEdge(caller.id, f"unresolved:self.{outer_attr}.{attr}", is_await, "unresolved", line, raw)

            # B3: SomeModule.foo() / SomeClass.foo() / param.foo()
            if isinstance(base, ast.Name):
                base_name = base.id

                param_type = caller.params_types.get(base_name)
                if param_type:
                    bare = param_type.rsplit(".", 1)[-1] if "." in param_type else param_type
                    target_mod = self.class_to_module.get(bare)
                    if target_mod:
                        return CallEdge(caller.id, f"{target_mod}.{bare}.{attr}", is_await, "param_method", line, raw)
                    return CallEdge(caller.id, f"boundary:{param_type}.{attr}", is_await, "boundary", line, raw)

                if base_name in imports:
                    qual = imports[base_name]
                    if base_name in self.class_to_module:
                        target_mod = self.class_to_module[base_name]
                        return CallEdge(caller.id, f"{target_mod}.{base_name}.{attr}", is_await, "internal_func", line, raw)
                    return CallEdge(caller.id, f"boundary:{qual}.{attr}", is_await, "boundary", line, raw)

                return CallEdge(caller.id, f"unresolved:{base_name}.{attr}", is_await, "unresolved", line, raw)

        # Case C: anything else
        return CallEdge(caller.id, f"unresolved:{raw}", is_await, "unresolved", line, raw)

    @staticmethod
    def _safe_unparse(node: ast.AST) -> str:
        try:
            src = ast.unparse(node)
            return src if len(src) <= 80 else src[:77] + "..."
        except Exception:
            return "<unparse-failed>"


def _iter_calls(fn_node: ast.AST):
    def walk(node, parent, inside_await):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if node is fn_node:
                for child in node.args.defaults + node.args.kw_defaults:
                    if child is not None:
                        yield from walk(child, node, False)
                for child in node.body:
                    yield from walk(child, node, False)
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


# ---------- adapter ----------


class PythonAdapter(LanguageAdapter):
    name = "python"
    extensions = (".py",)

    def analyze(self, files: list[Path], source_root: Path) -> ModuleAnalysis:
        modules: dict[str, _ModuleScanner] = {}
        function_ast_index: dict[tuple[str, int, str], ast.AST] = {}

        for path in files:
            rel = str(path.relative_to(source_root)) if path.is_absolute() else str(path)
            scanner = _ModuleScanner(file_relative=rel)
            tree = ast.parse((source_root / rel).read_text())
            scanner.visit(tree)
            scanner.finalize()
            modules[scanner.module_id] = scanner
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    function_ast_index[(rel, n.lineno, n.name)] = n

        self_attr_types: dict[tuple[str, str], str] = {}
        for mod in modules.values():
            self_attr_types.update(mod.self_attr_types)

        all_functions: list[FunctionNode] = []
        for mod in modules.values():
            all_functions.extend(mod.functions)

        internal_module_ids = set(modules.keys())
        extractor = _CallExtractor(modules, self_attr_types, internal_module_ids, function_ast_index)
        edges: list[CallEdge] = []
        for fn in all_functions:
            try:
                edges.extend(extractor.extract(fn))
            except KeyError:
                continue

        return ModuleAnalysis(functions=all_functions, edges=edges)

    def statement_spans(self, file_path, qualname):
        """Exact statement boundaries via `ast` (matches the legacy ast_snap)."""
        try:
            tree = ast.parse(Path(file_path).read_text(encoding="utf-8"))
        except SyntaxError:
            return None

        parts = qualname.split(".")
        top_names = {
            n.name for n in ast.iter_child_nodes(tree)
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        while parts and parts[0] not in top_names:
            parts.pop(0)
        if not parts:
            return None

        def collect(body):
            pairs = []
            for top in body:
                for node in ast.walk(top):
                    if isinstance(node, ast.stmt):
                        pairs.append((node.lineno, node.end_lineno or node.lineno))
            return sorted(set(pairs))

        if len(parts) == 1:
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == parts[0]:
                    return collect(node.body)
            return None

        class_name, *rest = parts
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == rest[-1]:
                        return collect(sub.body)
        return None


register("python", PythonAdapter, PythonAdapter.extensions)
