"""TypeScript language adapter (tree-sitter).

Concept mapping onto the IR:
  - top-level `function`            -> FunctionNode(class_name=None, is_method=False)
  - `class C { m() {} }`            -> method of C (class_name=C)
  - constructor / class fields      -> seed self-attr types: (C, field) -> Type
  - `import {X} from "..."`         -> import table

Call resolution (best-effort):
  this.m()        -> self_method
  this.field.m()  -> self_attr_method
  param.m()       -> param_method
  free()          -> internal_func (defined in a scanned module)
  else            -> boundary / unresolved
"""

from __future__ import annotations

from pathlib import Path

from base import LanguageAdapter, TSNode, collect_line_spans, parse_tree, register
from ir import CallEdge, FunctionNode, ModuleAnalysis

_GENERIC_TYPES = frozenset({
    "number", "string", "boolean", "any", "unknown", "void", "never",
    "object", "Array", "Promise", "Map", "Set", "Record", "Date", "Object",
})

_SKIP_DIRS = {"node_modules", ".git", "dist", "build"}


def _core_type(node: TSNode | None) -> str | None:
    """Bare type name from a type_annotation / type node."""
    if node is None:
        return None
    if node.kind == "type_annotation":
        # ': T' -> T (first named child)
        kids = node.named_children()
        return _core_type(kids[0]) if kids else None
    k = node.kind
    if k in ("type_identifier", "identifier"):
        return node.text
    if k == "generic_type":
        nm = node.field("name") or node.first_of_kind("type_identifier")
        return _core_type(nm) if nm else None
    ids = node.descendants_of_kind("type_identifier")
    return ids[0].text if ids else None


class _TsModule:
    def __init__(self, module_id: str, file_rel: str):
        self.module_id = module_id
        self.file = file_rel
        self.imports: dict[str, str] = {}
        self.type_names: set[str] = set()
        self.free_functions: set[str] = set()
        self.methods_by_type: dict[str, set[str]] = {}
        self.field_types: dict[tuple[str, str], str] = {}
        self.functions: list[tuple[FunctionNode, TSNode | None]] = []


class TypeScriptAdapter(LanguageAdapter):
    name = "typescript"
    extensions = (".ts", ".tsx")

    def discover(self, source_root: Path) -> list[Path]:
        out: list[Path] = []
        for ext in self.extensions:
            for p in sorted(source_root.rglob(f"*{ext}")):
                if any(part in _SKIP_DIRS for part in p.relative_to(source_root).parts):
                    continue
                if p.name.endswith(".d.ts"):
                    continue
                out.append(p)
        return out

    def _module_id(self, file_rel: str) -> str:
        stem = file_rel
        for ext in self.extensions:
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        return stem.replace("/", ".")

    def analyze(self, files: list[Path], source_root: Path) -> ModuleAnalysis:
        modules: dict[str, _TsModule] = {}
        for path in files:
            rel = str(path.relative_to(source_root)) if path.is_absolute() else str(path)
            root = parse_tree("typescript", (source_root / rel).read_text(encoding="utf-8", errors="replace"))
            m = _TsModule(self._module_id(rel), rel)
            self._scan(root, m)
            modules[m.module_id] = m

        type_to_module: dict[str, str] = {}
        for m in modules.values():
            for t in m.type_names:
                type_to_module.setdefault(t, m.module_id)

        all_functions: list[FunctionNode] = []
        edges: list[CallEdge] = []
        for m in modules.values():
            for node, body in m.functions:
                all_functions.append(node)
                if body is not None:
                    edges.extend(self._resolve(node, body, m, type_to_module))
        return ModuleAnalysis(functions=all_functions, edges=edges)

    # ---- scan ----

    def _scan(self, container: TSNode, m: _TsModule) -> None:
        for child in container.named_children():
            k = child.kind
            if k == "export_statement":
                self._scan(child, m)  # unwrap export
            elif k == "import_statement":
                self._record_import(child, m)
            elif k == "function_declaration":
                self._record_function(child, m, owner=None)
            elif k == "class_declaration":
                self._record_class(child, m)
            elif k in ("lexical_declaration", "variable_declaration"):
                # const helper = (..) => {..}
                for decl in child.children_of_kind("variable_declarator"):
                    val = decl.field("value")
                    if val is not None and val.kind in ("arrow_function", "function_expression"):
                        self._record_arrow(decl, val, m)

    def _record_import(self, node: TSNode, m: _TsModule) -> None:
        source = node.first_of_kind("string")
        src_text = source.text.strip("\"'") if source else ""
        clause = node.first_of_kind("import_clause")
        if clause is None:
            return
        for spec in clause.descendants_of_kind("import_specifier"):
            name = spec.field("name")
            alias = spec.field("alias")
            local = (alias or name).text if name else None
            if local:
                m.imports[local] = f"{src_text}::{name.text}" if name else src_text
        ns = clause.first_of_kind("namespace_import")
        if ns is not None:
            ident = ns.first_of_kind("identifier")
            if ident:
                m.imports[ident.text] = src_text

    def _record_class(self, node: TSNode, m: _TsModule) -> None:
        name_node = node.field("name")
        if name_node is None:
            return
        cls = name_node.text
        m.type_names.add(cls)
        body = node.field("body")
        if body is None:
            return
        for member in body.named_children():
            if member.kind == "method_definition":
                mname = member.field("name")
                if mname is None:
                    continue
                if mname.text == "constructor":
                    self._record_ctor_fields(member, cls, m)
                self._record_function(member, m, owner=cls)
            elif member.kind in ("public_field_definition", "property_signature", "field_definition"):
                fname = member.field("name")
                if fname is None:
                    continue
                val = member.field("value")
                if val is not None and val.kind in ("arrow_function", "function_expression"):
                    # Arrow/function class field, e.g. `go = async () => {...}`,
                    # is a method in all but syntax — record it as one so its
                    # node and call edges aren't lost.
                    m.methods_by_type.setdefault(cls, set()).add(fname.text)
                    node = self._make_node(fname.text, cls, m, member, val, is_async=self._is_async(val))
                    m.functions.append((node, val.field("body")))
                else:
                    ftype = _core_type(member.field("type"))
                    if ftype and ftype not in _GENERIC_TYPES:
                        m.field_types[(cls, fname.text)] = ftype

    def _record_ctor_fields(self, ctor: TSNode, cls: str, m: _TsModule) -> None:
        params = ctor.field("parameters")
        if params is None:
            return
        for p in params.named_children():
            # parameter_property: `private session: Session`
            pat = p.field("pattern") or p.first_of_kind("identifier")
            ftype = _core_type(p.field("type"))
            if pat is not None and ftype and ftype not in _GENERIC_TYPES:
                m.field_types[(cls, pat.text)] = ftype

    def _record_arrow(self, decl: TSNode, fn: TSNode, m: _TsModule) -> None:
        name_node = decl.field("name")
        if name_node is None:
            return
        name = name_node.text
        m.free_functions.add(name)
        node = self._make_node(name, None, m, decl, fn, is_async=self._is_async(fn))
        m.functions.append((node, fn.field("body")))

    def _record_function(self, fn: TSNode, m: _TsModule, *, owner) -> None:
        name_node = fn.field("name")
        if name_node is None:
            return
        name = name_node.text
        if owner:
            m.methods_by_type.setdefault(owner, set()).add(name)
        else:
            m.free_functions.add(name)
        node = self._make_node(name, owner, m, fn, fn, is_async=self._is_async(fn))
        m.functions.append((node, fn.field("body")))

    def _make_node(self, name, owner, m, decl_node, fn_node, *, is_async) -> FunctionNode:
        qualname = f"{owner}.{name}" if owner else name
        body = fn_node.field("body")
        if body is not None:
            sig = decl_node.text[: body.start_byte - decl_node.start_byte].strip()
        else:
            sig = decl_node.text.split("{")[0].strip()
        reads, writes = self._self_attrs(body) if owner else ([], [])
        return FunctionNode(
            id=f"{m.module_id}.{qualname}",
            name=name,
            qualname=qualname,
            file=m.file,
            line_start=decl_node.start_row + 1,
            line_end=decl_node.end_row + 1,
            signature=sig[:200],
            is_async=is_async,
            is_method=bool(owner),
            class_name=owner,
            decorators=[d.text for d in decl_node.children_of_kind("decorator")],
            used_self_attrs_read=reads,
            used_self_attrs_written=writes,
            params_types=self._param_types(fn_node),
        )

    def _is_async(self, fn: TSNode) -> bool:
        return any(c.text == "async" for c in fn.children())

    def _param_types(self, fn: TSNode) -> dict[str, str]:
        out: dict[str, str] = {}
        params = fn.field("parameters")
        if params is None:
            return out
        for p in params.named_children():
            pat = p.field("pattern") or p.first_of_kind("identifier")
            ty = _core_type(p.field("type"))
            if pat is not None and pat.kind == "identifier" and ty and ty not in _GENERIC_TYPES:
                out[pat.text] = ty
        return out

    def _self_attrs(self, body: TSNode | None) -> tuple[list[str], list[str]]:
        if body is None:
            return [], []
        reads: set[str] = set()
        writes: set[str] = set()
        for me in body.descendants_of_kind("member_expression"):
            obj = me.field("object")
            prop = me.field("property")
            if obj is not None and obj.kind == "this" and prop is not None:
                reads.add(prop.text)
        for asn in body.descendants_of_kind("assignment_expression"):
            left = asn.field("left")
            if left is not None and left.kind == "member_expression":
                o = left.field("object")
                pr = left.field("property")
                if o is not None and o.kind == "this" and pr is not None:
                    writes.add(pr.text)
        return sorted(reads), sorted(writes)

    # ---- resolve ----

    def _resolve(self, caller, body, m, type_to_module) -> list[CallEdge]:
        edges: list[CallEdge] = []
        for node, is_await in _iter_calls(body):
            fn_expr = node.field("function")
            if fn_expr is None:
                continue
            e = self._resolve_one(fn_expr, is_await, caller, m, type_to_module, node.start_row + 1)
            if e is not None:
                edges.append(e)
        return edges

    def _resolve_one(self, fn_expr, is_await, caller, m, type_to_module, line):
        raw = fn_expr.text[:80]
        k = fn_expr.kind
        if k == "identifier":
            name = fn_expr.text
            if name in m.free_functions:
                return CallEdge(caller.id, f"{m.module_id}.{name}", is_await, "internal_func", line, raw)
            if name in m.imports:
                return CallEdge(caller.id, f"boundary:{m.imports[name]}", is_await, "boundary", line, raw)
            return CallEdge(caller.id, f"unresolved:{name}", is_await, "unresolved", line, raw)

        if k == "member_expression":
            obj = fn_expr.field("object")
            prop = fn_expr.field("property")
            if obj is None or prop is None:
                return CallEdge(caller.id, f"unresolved:{raw}", is_await, "unresolved", line, raw)
            attr = prop.text

            # this.m()
            if obj.kind == "this" and caller.class_name:
                if attr in m.methods_by_type.get(caller.class_name, set()):
                    return CallEdge(caller.id, f"{m.module_id}.{caller.class_name}.{attr}", is_await, "self_method", line, raw)
                return CallEdge(caller.id, f"unresolved:this.{attr}", is_await, "unresolved", line, raw)

            # this.field.m()
            if obj.kind == "member_expression":
                inner_o = obj.field("object")
                inner_p = obj.field("property")
                if inner_o is not None and inner_o.kind == "this" and inner_p is not None and caller.class_name:
                    ftype = m.field_types.get((caller.class_name, inner_p.text))
                    if ftype:
                        tmod = type_to_module.get(ftype)
                        if tmod:
                            return CallEdge(caller.id, f"{tmod}.{ftype}.{attr}", is_await, "self_attr_method", line, raw)
                        return CallEdge(caller.id, f"boundary:{ftype}.{attr}", is_await, "boundary", line, raw)
                    return CallEdge(caller.id, f"unresolved:this.{inner_p.text}.{attr}", is_await, "unresolved", line, raw)

            # param.m()
            if obj.kind == "identifier":
                ptype = caller.params_types.get(obj.text)
                if ptype:
                    tmod = type_to_module.get(ptype)
                    if tmod:
                        return CallEdge(caller.id, f"{tmod}.{ptype}.{attr}", is_await, "param_method", line, raw)
                    return CallEdge(caller.id, f"boundary:{ptype}.{attr}", is_await, "boundary", line, raw)
                if obj.text in m.imports:
                    return CallEdge(caller.id, f"boundary:{m.imports[obj.text]}.{attr}", is_await, "boundary", line, raw)
                return CallEdge(caller.id, f"unresolved:{obj.text}.{attr}", is_await, "unresolved", line, raw)

            return CallEdge(caller.id, f"unresolved:{raw}", is_await, "unresolved", line, raw)

        return CallEdge(caller.id, f"unresolved:{raw}", is_await, "unresolved", line, raw)

    def statement_spans(self, file_path, qualname):
        root = parse_tree("typescript", Path(file_path).read_text(encoding="utf-8", errors="replace"))
        leaf = qualname.split(".")[-1]
        # regular functions + class methods
        for fn in root.descendants_of_kind("function_declaration", "method_definition"):
            nm = fn.field("name")
            if nm is not None and nm.text == leaf:
                body = fn.field("body")
                if body is not None:
                    return collect_line_spans(body)
        # arrow/function-expression bound to a name: class field `go = () => {}`
        # or top-level `const helper = () => {}`
        for holder in root.descendants_of_kind(
            "public_field_definition", "field_definition", "variable_declarator"
        ):
            nm = holder.field("name")
            val = holder.field("value")
            if (nm is not None and nm.text == leaf and val is not None
                    and val.kind in ("arrow_function", "function_expression")):
                body = val.field("body")
                if body is not None:
                    return collect_line_spans(body)
        return None


def _iter_calls(body: TSNode):
    results: list[tuple[TSNode, bool]] = []

    def walk(node: TSNode, inside_await: bool):
        k = node.kind
        if k in ("function_declaration", "function_expression", "arrow_function", "method_definition"):
            if node is not body:
                return
        if k == "await_expression":
            for c in node.children():
                walk(c, True)
            return
        if k == "call_expression":
            results.append((node, inside_await))
            for c in node.children():
                walk(c, False)
            return
        for c in node.children():
            walk(c, inside_await)

    walk(body, False)
    return results


register("typescript", TypeScriptAdapter, TypeScriptAdapter.extensions)
