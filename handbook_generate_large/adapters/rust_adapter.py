"""Rust language adapter (tree-sitter).

Mirrors the structure of the Python adapter — a two-pass design (scan modules,
then resolve calls) — but over a tree-sitter Rust grammar instead of `ast`.

Mapping of Rust concepts onto the language-agnostic IR:

  - `fn` at module level            -> FunctionNode(class_name=None, is_method=False)
  - `impl Type { fn m(&self) }`     -> method of Type (class_name=Type, is_method=True)
  - `trait T { fn m(&self){..} }`   -> default method of T (declarations w/o body skipped)
  - `struct S { f: T }`             -> seeds self-attr types: (S, f) -> T
  - `use a::b::C`                   -> import table  C -> a::b::C
  - module id                       -> file path with '/' -> '::', plus nested `mod`

Call resolution is best-effort (no full type inference):
  self.m()            -> self_method        (m defined on the impl's type)
  self.field.m()      -> self_attr_method    (field type known from the struct)
  param.m()           -> param_method        (param type from the signature)
  Type::assoc()       -> internal_constructor / internal_func / boundary
  free()              -> internal_func        (defined in some scanned module)
  mac!()              -> boundary
  everything else     -> unresolved:<hint>
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from base import LanguageAdapter, TSNode, collect_line_spans, parse_tree, register
from ir import CallEdge, FunctionNode, ModuleAnalysis

# Rust primitive / generic-container types whose methods carry no architectural
# signal — dropped from param/field typing (mirrors Python's _GENERIC_BUILTIN).
_GENERIC_TYPES = frozenset({
    "i8", "i16", "i32", "i64", "i128", "isize",
    "u8", "u16", "u32", "u64", "u128", "usize",
    "f32", "f64", "bool", "char", "str", "String",
    "Vec", "Box", "Option", "Result", "HashMap", "HashSet", "BTreeMap",
    "Rc", "Arc", "RefCell", "Cell", "Mutex", "RwLock", "Cow",
})

_SKIP_DIRS = {"target", ".git", "node_modules"}


def _core_type_name(node: TSNode | None) -> str | None:
    """Extract a bare type name from a type node (peeling refs/generics/scopes)."""
    if node is None:
        return None
    k = node.kind
    if k == "type_identifier":
        return node.text
    if k == "primitive_type":
        return node.text
    if k == "reference_type":
        return _core_type_name(node.field("type") or node.first_of_kind(
            "type_identifier", "generic_type", "reference_type", "scoped_type_identifier"))
    if k == "generic_type":
        return _core_type_name(node.field("type") or node.first_of_kind(
            "type_identifier", "scoped_type_identifier"))
    if k == "scoped_type_identifier":
        # a::b::C -> C
        last = None
        for c in node.children():
            if c.kind == "type_identifier":
                last = c.text
        return last
    # fallback: first nested type_identifier
    ids = node.descendants_of_kind("type_identifier")
    return ids[0].text if ids else None


class _RustModule:
    def __init__(self, module_id: str, file_rel: str):
        self.module_id = module_id
        self.file = file_rel
        self.imports: dict[str, str] = {}            # local name -> full path
        self.type_names: set[str] = set()            # struct/enum/trait/union names
        self.free_functions: set[str] = set()
        self.methods_by_type: dict[str, set[str]] = {}
        self.field_types: dict[tuple[str, str], str] = {}  # (type, field) -> type name
        # functions paired with their body node for the call pass
        self.functions: list[tuple[FunctionNode, TSNode | None]] = []


class RustAdapter(LanguageAdapter):
    name = "rust"
    extensions = (".rs",)

    def discover(self, source_root: Path) -> list[Path]:
        out: list[Path] = []
        for p in sorted(source_root.rglob("*.rs")):
            if any(part in _SKIP_DIRS for part in p.relative_to(source_root).parts):
                continue
            out.append(p)
        return out

    # ---- pass 1: scan a module ----

    def _module_id(self, file_rel: str) -> str:
        stem = file_rel[:-3] if file_rel.endswith(".rs") else file_rel
        parts = [p for p in stem.split("/") if p not in ("", "mod", "lib", "main")]
        return "::".join(parts) if parts else stem.replace("/", "::")

    def _scan_module(self, root: TSNode, file_rel: str) -> _RustModule:
        mod = _RustModule(self._module_id(file_rel), file_rel)
        self._scan_items(root, mod, container=None, prefix="")
        return mod

    def _scan_items(self, container_node: TSNode, mod: _RustModule, container, prefix: str) -> None:
        """Walk the direct items of a source_file / declaration_list."""
        pending_attrs: list[str] = []
        for child in container_node.named_children():
            k = child.kind
            if k == "attribute_item":
                pending_attrs.append(child.text)
                continue
            if k == "use_declaration":
                self._record_use(child, mod)
            elif k == "struct_item" or k == "union_item":
                self._record_struct(child, mod)
            elif k == "enum_item":
                name = self._type_name_of(child)
                if name:
                    mod.type_names.add(name)
            elif k == "trait_item":
                self._record_trait(child, mod, prefix)
            elif k == "impl_item":
                self._record_impl(child, mod, prefix)
            elif k == "function_item":
                self._record_function(child, mod, owner=None, prefix=prefix,
                                      decorators=pending_attrs, is_method=False)
            elif k == "mod_item":
                self._record_mod(child, mod, prefix)
            pending_attrs = []

    def _record_mod(self, node: TSNode, mod: _RustModule, prefix: str) -> None:
        name = self._type_name_of(node) or (node.field("name").text if node.field("name") else "")
        body = node.field("body")
        if body is not None and name:
            new_prefix = f"{prefix}{name}::"
            self._scan_items(body, mod, container=None, prefix=new_prefix)

    def _record_use(self, node: TSNode, mod: _RustModule) -> None:
        # Handle the common shapes: `use a::b::C;` and `use a::b::C as D;`.
        arg = node.field("argument") or node.named_children()[0] if node.named_children() else None
        if arg is None:
            return
        self._collect_use(arg, mod, base="")

    def _collect_use(self, node: TSNode, mod: _RustModule, base: str) -> None:
        k = node.kind
        if k == "scoped_identifier" or k == "identifier":
            full = node.text.replace(" ", "")
            local = full.split("::")[-1]
            mod.imports[local] = (base + full) if base else full
        elif k == "use_as_clause":
            path = node.field("path")
            alias = node.field("alias")
            if path is not None and alias is not None:
                full = (base + path.text).replace(" ", "")
                mod.imports[alias.text] = full
        elif k == "scoped_use_list":
            path = node.field("path")
            lst = node.field("list") or node.first_of_kind("use_list")
            new_base = (base + (path.text + "::" if path else "")).replace(" ", "")
            if lst is not None:
                for item in lst.named_children():
                    self._collect_use(item, mod, base=new_base)
        elif k == "use_list":
            for item in node.named_children():
                self._collect_use(item, mod, base=base)
        # use_wildcard (a::*) -> nothing useful to bind

    def _type_name_of(self, node: TSNode) -> str | None:
        nm = node.field("name")
        if nm is not None:
            return nm.text
        ti = node.first_of_kind("type_identifier")
        return ti.text if ti else None

    def _record_struct(self, node: TSNode, mod: _RustModule) -> None:
        name = self._type_name_of(node)
        if not name:
            return
        mod.type_names.add(name)
        body = node.field("body") or node.first_of_kind("field_declaration_list")
        if body is None:
            return
        for fd in body.children_of_kind("field_declaration"):
            fname = fd.field("name")
            ftype = _core_type_name(fd.field("type"))
            if fname is not None and ftype and ftype not in _GENERIC_TYPES:
                mod.field_types[(name, fname.text)] = ftype

    def _record_trait(self, node: TSNode, mod: _RustModule, prefix: str) -> None:
        name = self._type_name_of(node)
        if not name:
            return
        mod.type_names.add(name)
        body = node.field("body") or node.first_of_kind("declaration_list")
        if body is None:
            return
        for fn in body.children_of_kind("function_item"):
            if fn.field("body") is None:
                continue  # bare declaration, no implementation
            self._record_function(fn, mod, owner=name, prefix=prefix, decorators=[],
                                  is_method=self._has_self_param(fn))

    def _record_impl(self, node: TSNode, mod: _RustModule, prefix: str) -> None:
        type_name = _core_type_name(node.field("type"))
        if not type_name:
            return
        body = node.field("body") or node.first_of_kind("declaration_list")
        if body is None:
            return
        pending: list[str] = []
        for child in body.named_children():
            if child.kind == "attribute_item":
                pending.append(child.text)
                continue
            if child.kind == "function_item":
                self._record_function(child, mod, owner=type_name, prefix=prefix,
                                      decorators=pending, is_method=self._has_self_param(child))
            pending = []

    def _has_self_param(self, fn: TSNode) -> bool:
        params = fn.field("parameters")
        if params is None:
            return False
        return params.first_of_kind("self_parameter") is not None or "self" in (
            params.named_children()[0].text if params.named_child_count else "")

    def _is_async(self, fn: TSNode) -> bool:
        mods = fn.first_of_kind("function_modifiers")
        return mods is not None and "async" in mods.text

    def _signature(self, fn: TSNode, is_async: bool) -> str:
        name = fn.field("name").text if fn.field("name") else "?"
        params = fn.field("parameters").text if fn.field("parameters") else "()"
        ret = fn.field("return_type")
        ret_s = f" -> {ret.text}" if ret is not None else ""
        prefix = "async fn " if is_async else "fn "
        return f"{prefix}{name}{params}{ret_s}"

    def _param_types(self, fn: TSNode) -> dict[str, str]:
        out: dict[str, str] = {}
        params = fn.field("parameters")
        if params is None:
            return out
        for p in params.children_of_kind("parameter"):
            pat = p.field("pattern")
            ty = _core_type_name(p.field("type"))
            if pat is not None and ty and ty not in _GENERIC_TYPES:
                out[pat.text] = ty
        return out

    def _self_attrs(self, fn: TSNode) -> tuple[list[str], list[str]]:
        body = fn.field("body")
        if body is None:
            return [], []
        reads: set[str] = set()
        writes: set[str] = set()
        for fe in body.descendants_of_kind("field_expression"):
            val = fe.field("value")
            field = fe.field("field")
            if val is not None and val.kind == "self" and field is not None:
                reads.add(field.text)
        for asn in body.descendants_of_kind("assignment_expression", "compound_assignment_expr"):
            left = asn.field("left")
            if left is not None and left.kind == "field_expression":
                v = left.field("value")
                f = left.field("field")
                if v is not None and v.kind == "self" and f is not None:
                    writes.add(f.text)
        return sorted(reads), sorted(writes)

    def _record_function(self, fn: TSNode, mod: _RustModule, *, owner, prefix: str,
                         decorators: list[str], is_method: bool) -> None:
        name_node = fn.field("name")
        if name_node is None:
            return
        name = name_node.text
        is_async = self._is_async(fn)
        if owner:
            qualname = f"{prefix}{owner}::{name}"
            class_name = f"{prefix}{owner}" if prefix else owner
            mod.methods_by_type.setdefault(owner, set()).add(name)
        else:
            qualname = f"{prefix}{name}"
            class_name = None
            mod.free_functions.add(name)
        node_id = f"{mod.module_id}::{qualname}"
        reads, writes = self._self_attrs(fn) if is_method else ([], [])
        node = FunctionNode(
            id=node_id,
            name=name,
            qualname=qualname,
            file=mod.file,
            line_start=fn.start_row + 1,
            line_end=fn.end_row + 1,
            signature=self._signature(fn, is_async),
            is_async=is_async,
            is_method=is_method,
            class_name=class_name,
            decorators=decorators,
            used_self_attrs_read=reads,
            used_self_attrs_written=writes,
            params_types=self._param_types(fn),
        )
        mod.functions.append((node, fn.field("body")))

    def _merge_module(self, dst: _RustModule, src: _RustModule) -> None:
        """Fold `src` into `dst` (they share a module_id). First-seen wins on
        per-function id collisions, which only happen if both files define a
        same-named free fn — vanishingly rare for a lib.rs / main.rs pair."""
        dst.imports.update({k: v for k, v in src.imports.items() if k not in dst.imports})
        dst.type_names |= src.type_names
        dst.free_functions |= src.free_functions
        for t, ms in src.methods_by_type.items():
            dst.methods_by_type.setdefault(t, set()).update(ms)
        dst.field_types.update(src.field_types)
        existing_ids = {n.id for n, _ in dst.functions}
        for node, body in src.functions:
            if node.id not in existing_ids:
                dst.functions.append((node, body))
                existing_ids.add(node.id)

    # ---- pass 2: resolve calls ----

    def analyze(self, files: list[Path], source_root: Path) -> ModuleAnalysis:
        modules: dict[str, _RustModule] = {}
        for path in files:
            rel = str(path.relative_to(source_root)) if path.is_absolute() else str(path)
            root = parse_tree("rust", (source_root / rel).read_text(encoding="utf-8", errors="replace"))
            m = self._scan_module(root, rel)
            if m.module_id in modules:
                # Sibling files collapse to one module_id (lib.rs/main.rs/mod.rs
                # all strip to the crate-root id). Merge instead of overwriting,
                # so a thin main.rs no longer erases the whole lib.rs.
                self._merge_module(modules[m.module_id], m)
            else:
                modules[m.module_id] = m

        # cross-module: bare type name -> module id
        type_to_module: dict[str, str] = {}
        for m in modules.values():
            for t in m.type_names:
                type_to_module.setdefault(t, m.module_id)

        # cross-module free-function index: (module_tail, fn_name) -> {node ids}.
        # Lets a call like `dispatcher::select_handlers` or an imported bare
        # `select_handlers()` resolve to the real internal node instead of a
        # boundary stub — but ONLY when the (tail, name) pair is unique, so we
        # never reintroduce the bare-name cross-file collision.
        func_index = self._build_func_index(modules)

        all_functions: list[FunctionNode] = []
        edges: list[CallEdge] = []
        for m in modules.values():
            for node, body in m.functions:
                all_functions.append(node)
                if body is not None:
                    edges.extend(self._resolve_calls(node, body, m, modules,
                                                     type_to_module, func_index))

        return ModuleAnalysis(functions=all_functions, edges=edges)

    def _build_func_index(self, modules) -> dict:
        by_modtail_name: dict[tuple[str, str], set[str]] = defaultdict(set)
        for m in modules.values():
            modtail = m.module_id.split("::")[-1]
            for node, _body in m.functions:
                if node.class_name is None:  # free function only (methods resolve via type)
                    by_modtail_name[(modtail, node.name)].add(node.id)
        return {"by_modtail_name": by_modtail_name}

    def _resolve_free_call(self, leaf: str, owner: str, func_index: dict) -> str | None:
        """Resolve a free-function call to an internal node id via its module
        tail + name. Returns the id only when the match is UNIQUE (otherwise the
        call stays boundary — guessing would mis-attribute same-named functions)."""
        ids = func_index["by_modtail_name"].get((owner, leaf))
        if ids and len(ids) == 1:
            return next(iter(ids))
        return None

    def _resolve_calls(self, caller: FunctionNode, body: TSNode, mod: _RustModule,
                       modules, type_to_module, func_index) -> list[CallEdge]:
        edges: list[CallEdge] = []
        for node, is_await in _iter_calls(body):
            if node.kind == "macro_invocation":
                mac = node.field("macro")
                raw = (mac.text if mac else node.text)[:80]
                qual = mac.text.replace(" ", "") if mac else "macro"
                edges.append(CallEdge(caller.id, f"boundary:{qual}!", is_await, "boundary", node.start_row + 1, raw))
                continue
            fn_expr = node.field("function")
            if fn_expr is None:
                continue
            edge = self._resolve_one(fn_expr, is_await, caller, mod, type_to_module,
                                     func_index, node.start_row + 1)
            if edge is not None:
                edges.append(edge)
        return edges

    def _resolve_one(self, fn_expr: TSNode, is_await: bool, caller: FunctionNode,
                     mod: _RustModule, type_to_module, func_index, line: int):
        raw = fn_expr.text[:80]
        k = fn_expr.kind

        # bare name: free()  -> local function / imported
        if k == "identifier":
            name = fn_expr.text
            if name in mod.free_functions:
                return CallEdge(caller.id, f"{mod.module_id}::{name}", is_await, "internal_func", line, raw)
            if name in mod.imports:
                qual = mod.imports[name]
                head = qual.split("::")[0]
                # an imported local type used as a tuple-struct constructor, etc.
                if name in type_to_module:
                    tmod = type_to_module[name]
                    return CallEdge(caller.id, f"{tmod}::{name}::new", is_await, "internal_constructor", line, raw)
                # imported free function: try to resolve to a real internal node
                # via its import path's (module-tail, name) before falling back.
                segs = [s for s in qual.split("::") if s]
                leaf = segs[-1] if segs else name
                owner = segs[-2] if len(segs) >= 2 else ""
                rid = self._resolve_free_call(leaf, owner, func_index)
                if rid:
                    return CallEdge(caller.id, rid, is_await, "internal_func", line, raw)
                return CallEdge(caller.id, f"boundary:{qual}", is_await, "boundary", line, raw)
            return CallEdge(caller.id, f"unresolved:{name}", is_await, "unresolved", line, raw)

        # Type::assoc() or path::func()
        if k == "scoped_identifier":
            segs = [s for s in fn_expr.text.replace(" ", "").split("::") if s]
            if not segs:
                return CallEdge(caller.id, f"unresolved:{raw}", is_await, "unresolved", line, raw)
            leaf = segs[-1]
            owner = segs[-2] if len(segs) >= 2 else ""
            if owner in type_to_module:
                tmod = type_to_module[owner]
                ctype = "internal_constructor" if leaf in ("new", "default", "from") else "internal_func"
                return CallEdge(caller.id, f"{tmod}::{owner}::{leaf}", is_await, ctype, line, raw)
            # `module::func()` — owner is a module path segment, not a type. Try
            # to resolve to an internal free function by (module-tail, name).
            rid = self._resolve_free_call(leaf, owner, func_index)
            if rid:
                return CallEdge(caller.id, rid, is_await, "internal_func", line, raw)
            ctype = "boundary_constructor" if (owner[:1].isupper() and leaf in ("new", "default", "from")) else "boundary"
            return CallEdge(caller.id, f"boundary:{fn_expr.text.replace(' ', '')}", is_await, ctype, line, raw)

        # field_expression: self.m() / self.attr.m() / x.m()
        if k == "field_expression":
            base = fn_expr.field("value")
            field = fn_expr.field("field")
            if field is None or base is None:
                return CallEdge(caller.id, f"unresolved:{raw}", is_await, "unresolved", line, raw)
            attr = field.text

            # self.m()
            if base.kind == "self" and caller.class_name:
                bare_owner = caller.class_name.split("::")[-1]
                if attr in mod.methods_by_type.get(bare_owner, set()):
                    return CallEdge(caller.id, f"{mod.module_id}::{caller.class_name}::{attr}",
                                    is_await, "self_method", line, raw)
                return CallEdge(caller.id, f"unresolved:self.{attr}", is_await, "unresolved", line, raw)

            # self.field.m()
            if base.kind == "field_expression":
                inner_base = base.field("value")
                inner_field = base.field("field")
                if inner_base is not None and inner_base.kind == "self" and inner_field is not None and caller.class_name:
                    bare_owner = caller.class_name.split("::")[-1]
                    ftype = mod.field_types.get((bare_owner, inner_field.text))
                    if ftype:
                        tmod = type_to_module.get(ftype)
                        if tmod:
                            return CallEdge(caller.id, f"{tmod}::{ftype}::{attr}", is_await, "self_attr_method", line, raw)
                        return CallEdge(caller.id, f"boundary:{ftype}::{attr}", is_await, "boundary", line, raw)
                    return CallEdge(caller.id, f"unresolved:self.{inner_field.text}.{attr}", is_await, "unresolved", line, raw)

            # x.m() where x is a typed parameter
            if base.kind == "identifier":
                ptype = caller.params_types.get(base.text)
                if ptype:
                    tmod = type_to_module.get(ptype)
                    if tmod:
                        return CallEdge(caller.id, f"{tmod}::{ptype}::{attr}", is_await, "param_method", line, raw)
                    return CallEdge(caller.id, f"boundary:{ptype}::{attr}", is_await, "boundary", line, raw)
                return CallEdge(caller.id, f"unresolved:{base.text}.{attr}", is_await, "unresolved", line, raw)

            return CallEdge(caller.id, f"unresolved:{raw}", is_await, "unresolved", line, raw)

        return CallEdge(caller.id, f"unresolved:{raw}", is_await, "unresolved", line, raw)

    def statement_spans(self, file_path, qualname):
        root = parse_tree("rust", Path(file_path).read_text(encoding="utf-8", errors="replace"))
        leaf = qualname.split("::")[-1]
        for fn in root.descendants_of_kind("function_item"):
            nm = fn.field("name")
            if nm is not None and nm.text == leaf:
                body = fn.field("body")
                if body is not None:
                    return collect_line_spans(body)
        return None


def _iter_calls(body: TSNode):
    """Yield (call_or_macro_node, is_await) within body, skipping nested fn /
    closure bodies. is_await is True when the call sits directly under `.await`."""
    results: list[tuple[TSNode, bool]] = []

    def walk(node: TSNode, inside_await: bool):
        k = node.kind
        if k in ("function_item", "closure_expression"):
            return  # own scope
        if k == "await_expression":
            for c in node.children():
                walk(c, True)
            return
        if k == "call_expression":
            results.append((node, inside_await))
            for c in node.children():
                walk(c, False)
            return
        if k == "macro_invocation":
            results.append((node, inside_await))
            return
        for c in node.children():
            walk(c, inside_await)

    walk(body, False)
    return results


register("rust", RustAdapter, RustAdapter.extensions)
