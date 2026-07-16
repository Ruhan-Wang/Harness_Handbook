"""Phase 1 — language-agnostic graph assembly + emitters.

Takes a `ModuleAnalysis` (produced by any language adapter) and writes the four
artifacts with the SAME schema as the legacy
`handbook_generate/phase1/extract_graph.py`:

    graph.json · functions.csv · graph.dot · dropped_calls.json

Nothing here knows about Python/Rust/TS/Go — it only manipulates IR + strings.
This is the ~100% reusable half of the old Phase 1.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ir import BoundaryNode, CallEdge, FunctionNode, ModuleAnalysis


# Bare builtin names used to bucket unresolved edges (Python-flavored, but
# harmless for other languages — they simply won't match).
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
    name = callee_id[len("unresolved:"):] if callee_id.startswith("unresolved:") else callee_id

    if name.startswith("self."):
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


def partition_edges(edges: list[CallEdge]) -> tuple[list[CallEdge], list[CallEdge]]:
    kept: list[CallEdge] = []
    dropped: list[CallEdge] = []
    for e in edges:
        if e.call_type == "unresolved":
            dropped.append(e)
        else:
            kept.append(e)
    return kept, dropped


# ---------- node table ----------


def _split_boundary_qualname(qual: str) -> tuple[str, str, str]:
    parts = qual.split(".")
    if not parts:
        return "", "", qual
    for i, p in enumerate(parts[:-1]):
        if p and p[0].isupper():
            return ".".join(parts[:i]), p, ".".join(parts[i + 1:])
    return ".".join(parts[:-1]), "", parts[-1]


def _synthesize_init_node(ref_id: str, in_deg: int, out_deg: int, default_ext: str) -> dict:
    base = ref_id[:-len(".__init__")] if ref_id.endswith(".__init__") else ref_id
    module_id, _, class_qual = base.partition(".")
    return {
        "id": ref_id,
        "name": "__init__",
        "qualname": f"{class_qual}.__init__",
        "file": f"{module_id}{default_ext}" if module_id else "",
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
    functions: list[FunctionNode], edges: list[CallEdge], default_ext: str
) -> dict[str, dict]:
    nodes: dict[str, dict] = {}
    for fn in functions:
        nodes[fn.id] = asdict(fn)

    caller_count: dict[str, int] = defaultdict(int)
    callee_count: dict[str, int] = defaultdict(int)
    for e in edges:
        caller_count[e.caller_id] += 1
        callee_count[e.callee_id] += 1

    for nid, n in nodes.items():
        n["n_callees"] = caller_count.get(nid, 0)
        n["n_callers"] = callee_count.get(nid, 0)

    referenced_ids = {e.callee_id for e in edges} | {e.caller_id for e in edges}
    for ref_id in sorted(referenced_ids):
        if ref_id in nodes:
            continue
        if ref_id.startswith("boundary:"):
            qual = ref_id[len("boundary:"):]
            module, class_name, leaf = _split_boundary_qualname(qual)
            nodes[ref_id] = asdict(
                BoundaryNode(id=ref_id, name=leaf or qual, qualname=qual,
                             module=module, class_name=class_name, kind="boundary")
            )
            nodes[ref_id]["n_callees"] = caller_count.get(ref_id, 0)
            nodes[ref_id]["n_callers"] = callee_count.get(ref_id, 0)
            continue
        if ref_id.endswith(".__init__"):
            nodes[ref_id] = _synthesize_init_node(
                ref_id,
                in_deg=callee_count.get(ref_id, 0),
                out_deg=caller_count.get(ref_id, 0),
                default_ext=default_ext,
            )
            continue
    return nodes


def build_self_attrs_index(functions: list[FunctionNode]) -> dict[str, dict]:
    index: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"read_in": [], "written_in": []})
    )
    for fn in functions:
        if not fn.class_name:
            continue
        for attr in fn.used_self_attrs_read:
            index[fn.class_name][attr]["read_in"].append(fn.id)
        for attr in fn.used_self_attrs_written:
            index[fn.class_name][attr]["written_in"].append(fn.id)
    return {cls: {a: dict(v) for a, v in attrs.items()} for cls, attrs in index.items()}


# ---------- emit ----------


def emit_dropped_log(dropped: list[CallEdge], out_dir: Path) -> None:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for e in dropped:
        cat = categorize_dropped(e.callee_id)
        callee_raw = e.callee_id[len("unresolved:"):] if e.callee_id.startswith("unresolved:") else e.callee_id
        grouped[cat].append({
            "caller": e.caller_id,
            "callee_raw": callee_raw,
            "is_await": e.is_await,
            "line": e.line,
            "raw": e.raw,
        })
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
                "builtin": "len/isinstance/exception constructors — language builtins, no module namespace.",
                "local_var_method": "var.X() — var is a local variable with no type annotation.",
                "bare_name": "X() — closures, super(), and other names not tied to any def.",
            },
        },
        "edges_by_category": dict(grouped),
    }
    (out_dir / "dropped_calls.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))


def emit_json(nodes, edges, self_attrs, out_dir, source_root, scanned_files, lang) -> None:
    out = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "language": lang,
            "harness_dir": str(source_root),
            "scanned_files": scanned_files,
            "n_internal_functions": sum(1 for n in nodes.values() if n.get("kind") == "internal"),
            "n_boundary_nodes": sum(1 for n in nodes.values() if n.get("kind") == "boundary"),
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
    (out_dir / "graph.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))


def emit_csv(functions: list[FunctionNode], nodes: dict[str, dict], out_dir: Path) -> None:
    with open(out_dir / "functions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "id", "name", "qualname", "file", "line_start", "line_end", "class",
            "is_async", "is_method", "decorators", "signature",
            "n_callers", "n_callees", "n_self_attrs_read", "n_self_attrs_written",
            "unit_id", "unit_name", "responsibility",
        ])
        for fn in functions:
            n = nodes.get(fn.id, {})
            w.writerow([
                fn.id, fn.name, fn.qualname, fn.file, fn.line_start, fn.line_end,
                fn.class_name or "",
                "true" if fn.is_async else "false",
                "true" if fn.is_method else "false",
                "|".join(fn.decorators), fn.signature,
                n.get("n_callers", 0), n.get("n_callees", 0),
                len(fn.used_self_attrs_read), len(fn.used_self_attrs_written),
                "", "", "",
            ])


def emit_dot(nodes: dict[str, dict], edges: list[CallEdge], out_dir: Path) -> None:
    lines: list[str] = []
    lines.append("digraph callgraph {")
    lines.append("  rankdir=LR;")
    lines.append('  node [shape=box, style=rounded, fontname="Helvetica", fontsize=9];')
    lines.append('  edge [fontname="Helvetica", fontsize=8];')

    by_file: dict[str, list[dict]] = defaultdict(list)
    boundary_nodes: list[dict] = []
    for n in nodes.values():
        if n.get("kind") == "internal":
            by_file[n["file"]].append(n)
        elif n.get("kind") == "boundary":
            boundary_nodes.append(n)

    for i, (fname, ns) in enumerate(sorted(by_file.items())):
        lines.append(f"  subgraph cluster_{i} {{")
        lines.append(f'    label="{fname}";')
        lines.append("    style=dashed; color=gray60;")
        for n in ns:
            label = n["qualname"].replace('"', '\\"')
            lines.append(f'    "{n["id"]}" [label="{label}", fillcolor="#e8f0fe", style="filled,rounded"];')
        lines.append("  }")

    if boundary_nodes:
        lines.append("  subgraph cluster_boundary {")
        lines.append('    label="boundary"; style=dashed; color=gray60;')
        for n in boundary_nodes:
            label = n["qualname"].replace('"', '\\"')
            lines.append(f'    "{n["id"]}" [label="{label}", fillcolor="#f0f0f0", style="filled,rounded", color=gray40, fontcolor=gray30];')
        lines.append("  }")

    for e in edges:
        attrs = []
        if e.is_await:
            attrs.append('color="#1a73e8"')
        if e.call_type in ("boundary", "boundary_constructor"):
            attrs.append("style=dashed")
        attr_str = f" [{', '.join(attrs)}]" if attrs else ""
        lines.append(f'  "{e.caller_id}" -> "{e.callee_id}"{attr_str};')

    lines.append("}")
    (out_dir / "graph.dot").write_text("\n".join(lines))


# ---------- driver ----------


def build(
    analysis: ModuleAnalysis,
    *,
    source_root: Path,
    scanned_files: list[str],
    out_dir: Path,
    lang: str,
    default_ext: str,
    verbose: bool = True,
) -> dict:
    """Assemble + write all four artifacts. Returns summary stats."""
    out_dir.mkdir(parents=True, exist_ok=True)

    kept, dropped = partition_edges(analysis.edges)
    nodes = build_node_table(analysis.functions, kept, default_ext)
    self_attrs = build_self_attrs_index(analysis.functions)

    emit_json(nodes, kept, self_attrs, out_dir, source_root, scanned_files, lang)
    emit_csv(analysis.functions, nodes, out_dir)
    emit_dot(nodes, kept, out_dir)
    emit_dropped_log(dropped, out_dir)

    n_int = sum(1 for n in nodes.values() if n.get("kind") == "internal")
    n_bnd = sum(1 for n in nodes.values() if n.get("kind") == "boundary")
    stats = {
        "functions": len(analysis.functions),
        "edges_kept": len(kept),
        "edges_dropped": len(dropped),
        "internal_nodes": n_int,
        "boundary_nodes": n_bnd,
    }
    if verbose:
        type_counts: dict[str, int] = defaultdict(int)
        for e in kept:
            type_counts[e.call_type] += 1
        print(f"[build] functions={stats['functions']} kept={stats['edges_kept']} dropped={stats['edges_dropped']}")
        print(f"[build] internal={n_int} boundary={n_bnd}")
        print("[build] " + "  ".join(f"{t}={n}" for t, n in sorted(type_counts.items())))
        print(f"[done] outputs in {out_dir}/")
    return stats
