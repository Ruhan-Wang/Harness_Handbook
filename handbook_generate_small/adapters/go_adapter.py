"""Go language adapter (tree-sitter).

Concept mapping onto the IR:
  - `func F()`                       -> FunctionNode(class_name=None, is_method=False)
  - `func (r *T) M()`                -> method of T (class_name=T); `r` is the
                                        receiver var that plays the role of self
  - `type T struct { f F }`          -> seed self-attr types: (T, f) -> F
  - `import "pkg"` / `import a "pkg"` -> import table

Call resolution (best-effort), where `r` is the receiver var of the method:
  r.M()        -> self_method
  r.field.M()  -> self_attr_method
  param.M()    -> param_method
  F()          -> internal_func (defined in a scanned file)
  pkg.F()      -> boundary
  else         -> unresolved
"""

from __future__ import annotations

from pathlib import Path

from base import LanguageAdapter, TSNode, collect_line_spans, parse_tree, register
from ir import CallEdge, FunctionNode, ModuleAnalysis

_GENERIC_TYPES = frozenset({
    "int", "int8", "int16", "int32", "int64",
    "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
    "float32", "float64", "complex64", "complex128",
    "byte", "rune", "string", "bool", "error", "any",
})

_SKIP_DIRS = {".git", "vendor"}


def _core_type(node: TSNode | None) -> str | None:
    """Bare type name, peeling *T, []T, map[..]T, qualified pkg.T -> T."""
    if node is None:
        return None
    k = node.kind
    if k == "type_identifier":
        return node.text
    if k == "pointer_type":
        return _core_type(node.named_children()[0]) if node.named_child_count else None
    if k == "qualified_type":
        nm = node.field("name")
        return nm.text if nm else None
    if k in ("slice_type", "array_type"):
        el = node.field("element")
        return _core_type(el)
    ids = node.descendants_of_kind("type_identifier")
    return ids[-1].text if ids else None


class _GoModule:
    def __init__(self, module_id: str, file_rel: str):
        self.module_id = module_id
        self.file = file_rel
        self.imports: dict[str, str] = {}        # local pkg name -> import path
        self.type_names: set[str] = set()
        self.free_functions: set[str] = set()
        self.methods_by_type: dict[str, set[str]] = {}
        self.field_types: dict[tuple[str, str], str] = {}
        # (FunctionNode, body, receiver_var)
        self.functions: list[tuple[FunctionNode, TSNode | None, str | None]] = []


class GoAdapter(LanguageAdapter):
    name = "go"
    extensions = (".go",)

    def discover(self, source_root: Path) -> list[Path]:
        out: list[Path] = []
        for p in sorted(source_root.rglob("*.go")):
            rel_parts = p.relative_to(source_root).parts
            if any(part in _SKIP_DIRS for part in rel_parts):
                continue
            if p.name.endswith("_test.go"):
                continue
            out.append(p)
        return out

    def _module_id(self, file_rel: str) -> str:
        stem = file_rel[:-3] if file_rel.endswith(".go") else file_rel
        return stem.replace("/", ".")

    def analyze(self, files: list[Path], source_root: Path) -> ModuleAnalysis:
        modules: dict[str, _GoModule] = {}
        for path in files:
            rel = str(path.relative_to(source_root)) if path.is_absolute() else str(path)
            root = parse_tree("go", (source_root / rel).read_text(encoding="utf-8", errors="replace"))
            m = _GoModule(self._module_id(rel), rel)
            self._scan(root, m)
            modules[m.module_id] = m

        type_to_module: dict[str, str] = {}
        for m in modules.values():
            for t in m.type_names:
                type_to_module.setdefault(t, m.module_id)

        all_functions: list[FunctionNode] = []
        edges: list[CallEdge] = []
        for m in modules.values():
            for node, body, recv in m.functions:
                all_functions.append(node)
                if body is not None:
                    edges.extend(self._resolve(node, body, recv, m, type_to_module))
        return ModuleAnalysis(functions=all_functions, edges=edges)

    # ---- scan ----

    def _scan(self, root: TSNode, m: _GoModule) -> None:
        for child in root.named_children():
            k = child.kind
            if k == "import_declaration":
                self._record_imports(child, m)
            elif k == "type_declaration":
                self._record_types(child, m)
            elif k == "function_declaration":
                self._record_func(child, m)
            elif k == "method_declaration":
                self._record_method(child, m)

    def _record_imports(self, node: TSNode, m: _GoModule) -> None:
        for spec in node.descendants_of_kind("import_spec"):
            path_node = spec.field("path")
            name_node = spec.field("name")
            if path_node is None:
                continue
            path = path_node.text.strip("\"")
            local = name_node.text if name_node is not None else path.split("/")[-1]
            m.imports[local] = path

    def _record_types(self, node: TSNode, m: _GoModule) -> None:
        for spec in node.children_of_kind("type_spec"):
            name = spec.field("name")
            ty = spec.field("type")
            if name is None:
                continue
            m.type_names.add(name.text)
            if ty is not None and ty.kind == "struct_type":
                flist = ty.first_of_kind("field_declaration_list")
                if flist is not None:
                    for fd in flist.children_of_kind("field_declaration"):
                        ftype = _core_type(fd.field("type"))
                        if not ftype or ftype in _GENERIC_TYPES:
                            continue
                        for nm in fd.children_of_kind("field_identifier"):
                            m.field_types[(name.text, nm.text)] = ftype

    def _signature(self, fn: TSNode) -> str:
        body = fn.field("body")
        if body is not None:
            return fn.text[: body.start_byte - fn.start_byte].strip()[:200]
        return fn.text.split("{")[0].strip()[:200]

    def _param_types(self, fn: TSNode) -> dict[str, str]:
        out: dict[str, str] = {}
        params = fn.field("parameters")
        if params is None:
            return out
        for p in params.children_of_kind("parameter_declaration"):
            ty = _core_type(p.field("type"))
            if not ty or ty in _GENERIC_TYPES:
                continue
            for nm in p.children_of_kind("identifier"):
                out[nm.text] = ty
        return out

    def _record_func(self, fn: TSNode, m: _GoModule) -> None:
        name_node = fn.field("name")
        if name_node is None:
            return
        name = name_node.text
        m.free_functions.add(name)
        node = self._make_node(name, None, m, fn, recv_var=None)
        m.functions.append((node, fn.field("body"), None))

    def _record_method(self, fn: TSNode, m: _GoModule) -> None:
        name_node = fn.field("name")
        recv = fn.field("receiver")
        if name_node is None or recv is None:
            return
        recv_param = recv.first_of_kind("parameter_declaration")
        if recv_param is None:
            return
        owner = _core_type(recv_param.field("type"))
        if not owner:
            return
        recv_id = recv_param.first_of_kind("identifier")
        recv_var = recv_id.text if recv_id else None
        name = name_node.text
        m.methods_by_type.setdefault(owner, set()).add(name)
        node = self._make_node(name, owner, m, fn, recv_var=recv_var)
        m.functions.append((node, fn.field("body"), recv_var))

    def _make_node(self, name, owner, m, fn, *, recv_var) -> FunctionNode:
        qualname = f"{owner}.{name}" if owner else name
        reads, writes = ([], [])
        if owner and recv_var:
            reads, writes = self._self_attrs(fn.field("body"), recv_var)
        return FunctionNode(
            id=f"{m.module_id}.{qualname}",
            name=name,
            qualname=qualname,
            file=m.file,
            line_start=fn.start_row + 1,
            line_end=fn.end_row + 1,
            signature=self._signature(fn),
            is_async=False,  # Go has no async/await
            is_method=bool(owner),
            class_name=owner,
            decorators=[],
            used_self_attrs_read=reads,
            used_self_attrs_written=writes,
            params_types=self._param_types(fn),
        )

    def _self_attrs(self, body: TSNode | None, recv_var: str) -> tuple[list[str], list[str]]:
        if body is None:
            return [], []
        reads: set[str] = set()
        writes: set[str] = set()
        for sel in body.descendants_of_kind("selector_expression"):
            op = sel.field("operand")
            fld = sel.field("field")
            if op is not None and op.kind == "identifier" and op.text == recv_var and fld is not None:
                reads.add(fld.text)
        for asn in body.descendants_of_kind("assignment_statement"):
            left = asn.field("left")
            if left is not None:
                for sel in left.descendants_of_kind("selector_expression"):
                    op = sel.field("operand")
                    fld = sel.field("field")
                    if op is not None and op.kind == "identifier" and op.text == recv_var and fld is not None:
                        writes.add(fld.text)
        return sorted(reads), sorted(writes)

    # ---- resolve ----

    def _resolve(self, caller, body, recv_var, m, type_to_module) -> list[CallEdge]:
        edges: list[CallEdge] = []
        for node, _ in _iter_calls(body):
            fn_expr = node.field("function")
            if fn_expr is None:
                continue
            e = self._resolve_one(fn_expr, caller, recv_var, m, type_to_module, node.start_row + 1)
            if e is not None:
                edges.append(e)
        return edges

    def _resolve_one(self, fn_expr, caller, recv_var, m, type_to_module, line):
        raw = fn_expr.text[:80]
        k = fn_expr.kind
        if k == "identifier":
            name = fn_expr.text
            if name in m.free_functions:
                return CallEdge(caller.id, f"{m.module_id}.{name}", False, "internal_func", line, raw)
            return CallEdge(caller.id, f"unresolved:{name}", False, "unresolved", line, raw)

        if k == "selector_expression":
            op = fn_expr.field("operand")
            fld = fn_expr.field("field")
            if op is None or fld is None:
                return CallEdge(caller.id, f"unresolved:{raw}", False, "unresolved", line, raw)
            attr = fld.text

            # r.M()
            if op.kind == "identifier" and recv_var and op.text == recv_var and caller.class_name:
                if attr in m.methods_by_type.get(caller.class_name, set()):
                    return CallEdge(caller.id, f"{m.module_id}.{caller.class_name}.{attr}", False, "self_method", line, raw)
                return CallEdge(caller.id, f"unresolved:{recv_var}.{attr}", False, "unresolved", line, raw)

            # r.field.M()
            if op.kind == "selector_expression":
                inner_op = op.field("operand")
                inner_f = op.field("field")
                if (inner_op is not None and inner_op.kind == "identifier" and recv_var
                        and inner_op.text == recv_var and inner_f is not None and caller.class_name):
                    ftype = m.field_types.get((caller.class_name, inner_f.text))
                    if ftype:
                        tmod = type_to_module.get(ftype)
                        if tmod:
                            return CallEdge(caller.id, f"{tmod}.{ftype}.{attr}", False, "self_attr_method", line, raw)
                        return CallEdge(caller.id, f"boundary:{ftype}.{attr}", False, "boundary", line, raw)
                    return CallEdge(caller.id, f"unresolved:{recv_var}.{inner_f.text}.{attr}", False, "unresolved", line, raw)

            # param.M() or pkg.F()
            if op.kind == "identifier":
                ptype = caller.params_types.get(op.text)
                if ptype:
                    tmod = type_to_module.get(ptype)
                    if tmod:
                        return CallEdge(caller.id, f"{tmod}.{ptype}.{attr}", False, "param_method", line, raw)
                    return CallEdge(caller.id, f"boundary:{ptype}.{attr}", False, "boundary", line, raw)
                if op.text in m.imports:
                    return CallEdge(caller.id, f"boundary:{m.imports[op.text]}.{attr}", False, "boundary", line, raw)
                return CallEdge(caller.id, f"unresolved:{op.text}.{attr}", False, "unresolved", line, raw)

            return CallEdge(caller.id, f"unresolved:{raw}", False, "unresolved", line, raw)

        return CallEdge(caller.id, f"unresolved:{raw}", False, "unresolved", line, raw)

    def statement_spans(self, file_path, qualname):
        root = parse_tree("go", Path(file_path).read_text(encoding="utf-8", errors="replace"))
        leaf = qualname.split(".")[-1]
        for fn in root.descendants_of_kind("function_declaration", "method_declaration"):
            nm = fn.field("name")
            if nm is not None and nm.text == leaf:
                body = fn.field("body")
                if body is not None:
                    return collect_line_spans(body)
        return None


def _iter_calls(body: TSNode):
    results: list[tuple[TSNode, bool]] = []

    def walk(node: TSNode):
        k = node.kind
        if k in ("func_literal", "function_declaration", "method_declaration") and node is not body:
            return
        if k == "call_expression":
            results.append((node, False))
            for c in node.children():
                walk(c)
            return
        for c in node.children():
            walk(c)

    walk(body)
    return results


register("go", GoAdapter, GoAdapter.extensions)
