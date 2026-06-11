# -*- coding: utf-8 -*-
"""Structured handbook document tree (the JSON intermediate).

The progressive-disclosure hierarchy is EXPLICIT data here, not implicit in
markdown formatting:

    HandbookDoc
      ├─ overview            (Tier 1)  — L1
      ├─ stages[id]          (Tier 2)  — L2, a tree via parent/children
      │     └─ functions[]   (Tier 3)  — L3
      └─ registers                     — appendix

Renderers (render_doc.py) walk this tree to emit either linear markdown or
nested-collapsible HTML — neither has to reverse-engineer structure from text.
Each generated node also carries its critic `score` + `findings` so the
loop's output is auditable.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class FunctionNode:
    """Tier 3 — one function (or one function's regions) within a stage."""
    qualname: str
    type_kind: str = "single"            # "single" | "multi_region"
    translation: dict = field(default_factory=dict)   # the structured Tier-3 JSON
    score: dict | None = None            # critic verdict (overall + per-criterion)
    findings: list = field(default_factory=list)


@dataclass
class StageNode:
    """Tier 2 — one stage. Tree shape via parent/children (ids)."""
    id: str
    chapter: str                         # gap-free render-time number, e.g. "4.2"
    title: str
    parent: str | None = None
    children: list = field(default_factory=list)       # child stage ids
    logical_md: str = ""                 # Tier 2 narrative
    score: dict | None = None
    findings: list = field(default_factory=list)
    functions: list = field(default_factory=list)      # list[FunctionNode]
    members_count: int = 0


@dataclass
class HandbookDoc:
    meta: dict = field(default_factory=dict)
    overview_md: str = ""                # Tier 1
    overview_score: dict | None = None
    overview_findings: list = field(default_factory=list)
    stages: dict = field(default_factory=dict)         # id -> StageNode
    order: list = field(default_factory=list)          # render order of stage ids
    registers_md: str = ""
    coherence_findings: list = field(default_factory=list)

    # ── serialization ────────────────────────────────────────────────────────

    def to_json(self) -> dict:
        return {
            "meta": self.meta,
            "overview": {
                "content_md": self.overview_md,
                "score": self.overview_score,
                "findings": self.overview_findings,
            },
            "stages": {sid: _stage_to_json(s) for sid, s in self.stages.items()},
            "order": self.order,
            "registers_md": self.registers_md,
            "coherence_findings": self.coherence_findings,
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def read(cls, path: Path) -> "HandbookDoc":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        stages = {}
        for sid, s in d.get("stages", {}).items():
            fns = [FunctionNode(**f) for f in s.get("functions", [])]
            s2 = {k: v for k, v in s.items() if k != "functions"}
            stages[sid] = StageNode(functions=fns, **s2)
        ov = d.get("overview", {})
        return cls(
            meta=d.get("meta", {}),
            overview_md=ov.get("content_md", ""),
            overview_score=ov.get("score"),
            overview_findings=ov.get("findings", []),
            stages=stages,
            order=d.get("order", []),
            registers_md=d.get("registers_md", ""),
            coherence_findings=d.get("coherence_findings", []),
        )

    # ── tree helpers ─────────────────────────────────────────────────────────

    def top_level(self) -> list:
        return [self.stages[s] for s in self.order
                if s in self.stages and not self.stages[s].parent]


def _stage_to_json(s: StageNode) -> dict:
    d = asdict(s)
    return d
