# -*- coding: utf-8 -*-
"""load_inputs.py — Phase 3 input loading + stage-tree assembly.

Phase 3 consumes the Phase 2 artifacts (read-only) and builds the hierarchical
stage tree the bottom-up narration walks:

  cards/                 per-file deep cards (the handbook leaf content)
  skeleton.yaml          stage hierarchy (id/title/description/parent/children)
  file_stage.json        file -> stage buckets (every file assigned)
  stage_organization.yaml per-stage intra-grouping + ordered_files (leaf stages)

`load_all` returns one bundle; `StageTree` exposes the parent/children walk and
the per-stage file/group lookups the driver needs. Nothing here calls the LLM —
it is pure structural loading.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "shared"))
sys.path.insert(0, str(_HERE.parent / "phase2"))

import read_files  # noqa: E402  (phase2; reuse load_cards)
import skeleton_yaml  # noqa: E402  (shared)

logger = logging.getLogger(__name__)


@dataclass
class StageTree:
    """The skeleton stages organized as a navigable tree.

    `stages_by_id` keeps the canonical stage dicts (id/title/description/parent/
    children/crosscut). `order` preserves skeleton order (lifecycle order), so a
    top-level / sibling listing reads startup -> teardown, not alphabetical.
    """
    stages_by_id: dict[str, dict]
    order: list[str]                      # all stage ids, skeleton order
    top_level: list[str]                  # parent-less stage ids, skeleton order
    children_of: dict[str, list[str]]     # stage id -> child ids (skeleton order)
    buckets: dict[str, list[str]]         # stage id -> files DIRECTLY in it
    organization: dict[str, dict]         # stage id -> {title, groups, ordered_files}
    cards: dict[str, dict]                # file path -> deep card
    metadata: dict[str, Any] = field(default_factory=dict)

    def title(self, sid: str) -> str:
        s = self.stages_by_id.get(sid, {})
        return s.get("title") or sid

    def description(self, sid: str) -> str:
        return (self.stages_by_id.get(sid, {}) or {}).get("description", "")

    def is_crosscut(self, sid: str) -> bool:
        return bool((self.stages_by_id.get(sid, {}) or {}).get("crosscut"))

    def children(self, sid: str) -> list[str]:
        return self.children_of.get(sid, [])

    def direct_files(self, sid: str) -> list[str]:
        """Files assigned DIRECTLY to this stage (not its descendants).

        Ordered by the stage's organization (callers-before-callees + LLM
        grouping) when available, else the raw bucket order.
        """
        org = self.organization.get(sid)
        if org and org.get("ordered_files"):
            return list(org["ordered_files"])
        return list(self.buckets.get(sid, []))

    def groups(self, sid: str) -> list[dict]:
        """The organization sub-groups for this stage (empty if none)."""
        org = self.organization.get(sid)
        return list(org.get("groups", [])) if org else []

    def subtree_file_count(self, sid: str) -> int:
        """Total files in this stage's whole subtree (self + descendants)."""
        n = len(self.buckets.get(sid, []))
        for c in self.children_of.get(sid, []):
            n += self.subtree_file_count(c)
        return n


def build_stage_tree(skeleton: dict, file_stage: dict, organization: dict,
                     cards: dict) -> StageTree:
    """Assemble a StageTree from the loaded artifacts.

    The skeleton already carries `children`, but we re-derive children_of from
    `parent` to be robust to any stale `children` lists, while keeping skeleton
    ORDER (the stages list is lifecycle-ordered)."""
    stages = skeleton.get("stages", []) or []
    stages_by_id = {s["id"]: s for s in stages}
    order = [s["id"] for s in stages]

    children_of: dict[str, list[str]] = {sid: [] for sid in order}
    top_level: list[str] = []
    for s in stages:                       # preserve skeleton order
        parent = s.get("parent")
        if parent and parent in stages_by_id:
            children_of[parent].append(s["id"])
        else:
            top_level.append(s["id"])

    buckets = file_stage.get("buckets", {}) or {}
    org_stages = organization.get("stages", {}) or {}
    return StageTree(
        stages_by_id=stages_by_id,
        order=order,
        top_level=top_level,
        children_of=children_of,
        buckets=buckets,
        organization=org_stages,
        cards=cards,
        metadata=skeleton.get("metadata", {}) or {},
    )


def load_all(phase2_dir: Path) -> StageTree:
    """Read every Phase 2 artifact under `phase2_dir` and build the StageTree.

    Expects: cards/, skeleton.yaml, file_stage.json, stage_organization.yaml.
    """
    phase2_dir = Path(phase2_dir)
    cards_dir = phase2_dir / "cards"
    skeleton_path = phase2_dir / "skeleton.yaml"
    file_stage_path = phase2_dir / "file_stage.json"
    org_path = phase2_dir / "stage_organization.yaml"

    missing = [str(p) for p in (cards_dir, skeleton_path, file_stage_path, org_path)
               if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Phase 3 needs Phase 2 outputs; missing: " + ", ".join(missing))

    cards = read_files.load_cards(cards_dir)
    skeleton = skeleton_yaml.load_yaml(skeleton_path)
    file_stage = json.loads(file_stage_path.read_text(encoding="utf-8"))
    import yaml
    organization = yaml.safe_load(org_path.read_text(encoding="utf-8")) or {}

    tree = build_stage_tree(skeleton, file_stage, organization, cards)
    logger.info("phase3 load: %d stages (%d top-level), %d cards, %d non-empty buckets",
                len(tree.order), len(tree.top_level), len(cards),
                sum(1 for v in tree.buckets.values() if v))
    return tree
