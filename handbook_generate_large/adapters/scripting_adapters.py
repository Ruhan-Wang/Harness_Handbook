"""Scripting-language adapters: Starlark, Shell (bash), PowerShell.

These languages have a weak "call graph" notion — no classes/methods, and in
Shell/PowerShell most "calls" are external commands, not function calls. So
these adapters use a simple free-function model:

  - one FunctionNode per function definition (class_name=None, is_method=False)
  - a call edge for every call/command:
      * name matches a function defined anywhere in the scanned set -> internal_func
      * otherwise -> boundary (external command / loaded symbol — real dependency
        info, e.g. "this script calls git/cargo")

Included so a mixed repo (e.g. codex: Rust + Python + Starlark + Shell +
PowerShell) has none of its files silently dropped from the handbook.
"""

from __future__ import annotations

from pathlib import Path

from base import LanguageAdapter, TSNode, collect_line_spans, parse_tree, register
from ir import CallEdge, FunctionNode, ModuleAnalysis


class _ScriptAdapter(LanguageAdapter):
    ts_lang: str = ""       # tree-sitter grammar name
    fn_kind: str = ""       # function-definition node kind
    sep: str = "."          # id/qualname separator

    # ---- per-language hooks ----
    def _fn_name(self, fn: TSNode):
        return fn.field("name")

    def _fn_body(self, fn: TSNode):
        return fn.field("body")

    def _calls(self, body: TSNode):
        """Yield (callee_name, line, raw) for each call/command in body."""
        raise NotImplementedError

    # ---- shared ----
    def _module_id(self, file_rel: str) -> str:
        stem = file_rel
        for ext in self.extensions:
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        return stem.replace("/", ".")

    def _signature(self, fn: TSNode, body: TSNode | None) -> str:
        if body is not None:
            return fn.text[: body.start_byte - fn.start_byte].strip()[:200]
        return fn.text.split("\n")[0].strip()[:200]

    def analyze(self, files: list[Path], source_root: Path) -> ModuleAnalysis:
        modules = []  # list of dicts: {id, funcs:[(node,body)], names:set}
        for path in files:
            rel = str(path.relative_to(source_root)) if path.is_absolute() else str(path)
            root = parse_tree(self.ts_lang, (source_root / rel).read_text(encoding="utf-8", errors="replace"))
            mid = self._module_id(rel)
            funcs = []
            names = set()
            for fn in root.descendants_of_kind(self.fn_kind):
                nm = self._fn_name(fn)
                if nm is None or not nm.text.strip():
                    continue
                name = nm.text.strip()
                body = self._fn_body(fn)
                node = FunctionNode(
                    id=f"{mid}{self.sep}{name}", name=name, qualname=name, file=rel,
                    line_start=fn.start_row + 1, line_end=fn.end_row + 1,
                    signature=self._signature(fn, body),
                    is_async=False, is_method=False, class_name=None, decorators=[],
                    used_self_attrs_read=[], used_self_attrs_written=[], params_types={},
                )
                funcs.append((node, body))
                names.add(name)
            modules.append({"id": mid, "funcs": funcs, "names": names})

        name_to_module: dict[str, str] = {}
        for m in modules:
            for n in m["names"]:
                name_to_module.setdefault(n, m["id"])

        all_funcs: list[FunctionNode] = []
        edges: list[CallEdge] = []
        for m in modules:
            for node, body in m["funcs"]:
                all_funcs.append(node)
                if body is None:
                    continue
                for cname, line, raw in self._calls(body):
                    if cname in name_to_module:
                        edges.append(CallEdge(node.id, f"{name_to_module[cname]}{self.sep}{cname}",
                                              False, "internal_func", line, raw))
                    else:
                        edges.append(CallEdge(node.id, f"boundary:{cname}", False, "boundary", line, raw))
        return ModuleAnalysis(functions=all_funcs, edges=edges)

    def statement_spans(self, file_path, qualname):
        root = parse_tree(self.ts_lang, Path(file_path).read_text(encoding="utf-8", errors="replace"))
        leaf = qualname.split(self.sep)[-1]
        for fn in root.descendants_of_kind(self.fn_kind):
            nm = self._fn_name(fn)
            if nm is not None and nm.text.strip() == leaf:
                body = self._fn_body(fn)
                if body is not None:
                    return collect_line_spans(body)
        return None


class StarlarkAdapter(_ScriptAdapter):
    name = "starlark"
    extensions = (".star", ".bzl", ".bazel")
    ts_lang = "starlark"
    fn_kind = "function_definition"

    def _calls(self, body: TSNode):
        for c in body.descendants_of_kind("call"):
            fn = c.field("function")
            if fn is None:
                continue
            name = fn.text.split(".")[-1].strip()
            if name:
                yield name, c.start_row + 1, fn.text[:80]


class _CommandAdapter(_ScriptAdapter):
    """Shell + PowerShell: calls are `command` nodes with a `command_name`."""

    def _calls(self, body: TSNode):
        for c in body.descendants_of_kind("command"):
            cn = c.first_of_kind("command_name")
            if cn is None:
                continue
            tok = cn.text.strip().split()
            if not tok:
                continue
            name = tok[0].split("/")[-1]  # /usr/bin/git -> git
            if name:
                yield name, c.start_row + 1, cn.text[:80]


class ShellAdapter(_CommandAdapter):
    name = "shell"
    extensions = (".sh", ".bash")
    ts_lang = "bash"
    fn_kind = "function_definition"


class PowerShellAdapter(_CommandAdapter):
    name = "powershell"
    extensions = (".ps1", ".psm1", ".psd1")
    ts_lang = "powershell"
    fn_kind = "function_statement"

    def _fn_name(self, fn: TSNode):
        return fn.field("name") or fn.first_of_kind("function_name")

    def _fn_body(self, fn: TSNode):
        return fn.field("body") or fn.first_of_kind("script_block")


register("starlark", StarlarkAdapter, StarlarkAdapter.extensions)
register("shell", ShellAdapter, ShellAdapter.extensions)
register("powershell", PowerShellAdapter, PowerShellAdapter.extensions)
