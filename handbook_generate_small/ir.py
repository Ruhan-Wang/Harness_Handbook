"""Language-agnostic intermediate representation for the multilang handbook
pipeline.

Every language adapter (Python via `ast`, Rust/TS/Go via tree-sitter) parses
source into these three node/edge kinds. `phase1/build_graph.py` then assembles
them into a `graph.json` whose schema is **byte-for-byte compatible** with the
legacy `handbook_generate/phase1` output, so the unchanged Phase 2 / Phase 3
can consume it without modification.

The field set here is a deliberate copy of the legacy dataclasses in
`handbook_generate/phase1/extract_graph.py` — keep them in sync. The only thing
that varies per language is *who fills these in* (the adapter), not the shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FunctionNode:
    """One internal function / method, or a synthesized node (e.g. a dataclass
    __init__ referenced by an edge but never written as an explicit def)."""

    id: str  # e.g. "terminus_2.Terminus2._run_agent_loop"
    name: str
    qualname: str  # e.g. "Terminus2._run_agent_loop" (relative to module)
    file: str  # relative to source_root
    line_start: int
    line_end: int
    signature: str
    is_async: bool
    is_method: bool
    class_name: Optional[str]
    decorators: list[str]
    kind: str = "internal"
    # True for nodes not directly extracted from a definition in the source
    # (e.g. @dataclass __init__). line_start/line_end are 0 and not meaningful.
    synthetic: bool = False
    used_self_attrs_read: list[str] = field(default_factory=list)
    used_self_attrs_written: list[str] = field(default_factory=list)
    # param_name -> resolved type name (bare class name or qualified path).
    params_types: dict[str, str] = field(default_factory=dict)


@dataclass
class BoundaryNode:
    """One external ("boundary") function that an internal function calls."""

    id: str  # "boundary:<qualname>"
    name: str  # leaf segment, e.g. "call", "Step", "search"
    qualname: str  # full dotted path, e.g. "harbor.llms.base.BaseLLM.call"
    module: str  # package/module path only (without trailing class)
    class_name: str  # owning class if it's a method, else ""
    kind: str = "boundary"


@dataclass
class CallEdge:
    """A resolved (or unresolved) call from one function to another node.

    call_type is one of:
      self_method | self_attr_method | param_method | internal_func |
      internal_constructor | boundary | boundary_constructor | unresolved

    Unresolved edges carry a callee_id of the form "unresolved:<hint>" and are
    partitioned out of the kept graph by build_graph (they land in
    dropped_calls.json instead).
    """

    caller_id: str
    callee_id: str
    is_await: bool
    call_type: str
    line: int
    raw: str  # source text of the call expression head (<= 80 chars)


@dataclass
class ModuleAnalysis:
    """What an adapter returns from analyze(): the language-agnostic IR for a
    whole set of source files. `phase1/build_graph.py` takes it from here.

    `edges` contains *all* edges including unresolved ones; partitioning into
    kept/dropped is the build step's job (it is identical across languages).
    """

    functions: list[FunctionNode]
    edges: list[CallEdge]
