# -*- coding: utf-8 -*-
"""skeleton_yaml.py — canonical structured form of the skeleton.

Phase 2's Critic-Actor iteration needs to *mechanically modify* the skeleton
(add stages, merge stages, etc.). Markdown is too fragile to auto-edit, so the
canonical form is YAML. The markdown is re-generated from the YAML for human
reading.

This module provides:
  - convert_md_to_yaml(skeleton_md_path, skeleton_yaml_path)
      One-time bootstrap: parse skeleton.md (the existing artifact) and write
      a structured skeleton.yaml.
  - load_yaml(skeleton_yaml_path) -> SkeletonDoc
      Load the structured form for tooling.
  - save_yaml(doc, path) — write back after modifications.
  - render_md_from_yaml(doc, path) — regenerate the markdown for humans.

SkeletonDoc shape:
  {
    "metadata": {"version": 1},
    "stages": [
      {"id": "stage-1", "title": "...", "description": "...",
       "parent": null, "children": [...]},
      ...
    ],
    "state_registers": [...],
    "subsystems": [...],
  }
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parse_skeleton import parse_skeleton  # noqa: E402


# ─── YAML I/O ─────────────────────────────────────────────────────────────────


class _SkeletonDumper(yaml.SafeDumper):
    """Preserves key order; emits block style with readable multi-line strings."""


def _represent_str(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar(
            "tag:yaml.org,2002:str", data.rstrip("\n") + "\n", style="|"
        )
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


def _represent_list(dumper, data):
    # Short scalar lists in flow style for compactness.
    if data and len(data) <= 8 and all(
        isinstance(x, (int, float, bool)) or (isinstance(x, str) and len(x) <= 40)
        for x in data
    ):
        return dumper.represent_sequence(
            "tag:yaml.org,2002:seq", data, flow_style=True
        )
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=False)


_SkeletonDumper.add_representer(str, _represent_str)
_SkeletonDumper.add_representer(list, _represent_list)
_SkeletonDumper.ignore_aliases = lambda self, data: True  # type: ignore[assignment]


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def save_yaml(doc: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(
            doc,
            Dumper=_SkeletonDumper,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
            width=10000,
        ),
        encoding="utf-8",
    )


# ─── MD → YAML bootstrap ──────────────────────────────────────────────────────


def convert_md_to_yaml(md_path: Path, yaml_path: Path) -> dict:
    """One-time bootstrap. Parses the existing skeleton.md (via parse_skeleton)
    and writes a structured skeleton.yaml.

    parse_skeleton already extracts stage IDs, titles, descriptions, registers,
    and subsystems — we just reorganize that into the canonical YAML shape.
    """
    table = parse_skeleton(md_path)

    # Determine parent-child relationships from stage_id prefixes
    # (e.g., stage-4.1 is a child of stage-4; side-S1.1 is a child of side-S1).
    children_of: dict[str, list[str]] = {sid: [] for sid in table.stage_ids}
    parent_of: dict[str, str | None] = {sid: None for sid in table.stage_ids}
    for sid in table.stage_ids:
        # Strip trailing ".X" if it leaves a remaining id that exists.
        m = re.match(r"^(.*?)(\.\d+)$", sid)
        if m:
            candidate_parent = m.group(1)
            if candidate_parent in children_of:
                parent_of[sid] = candidate_parent
                children_of[candidate_parent].append(sid)

    stages: list[dict] = []
    for sid, entry in table.stages.items():
        stages.append(
            {
                "id": sid,
                "title": entry.title,
                "description": entry.description.strip() or "(no description)",
                "parent": parent_of.get(sid),
                "children": children_of.get(sid, []),
            }
        )

    state_registers = [
        {"id": rid, "semantics": semantics}
        for rid, semantics in table.registers.items()
    ]

    subsystems = [
        {"id": sid, "role": role} for sid, role in table.subsystems.items()
    ]

    doc = {
        "metadata": {"version": 1, "generated_from": str(md_path.name)},
        "stages": stages,
        "state_registers": state_registers,
        "subsystems": subsystems,
    }

    save_yaml(doc, yaml_path)
    return doc


# ─── YAML → MD render ─────────────────────────────────────────────────────────


def render_md_from_yaml(doc: dict, md_path: Path) -> None:
    """Regenerate skeleton.md from canonical skeleton.yaml.

    This is a structural render: title, ID-anchored heading, description.
    The richly-styled prose in the original skeleton.md (data-flow diagram,
    fill-in guidance, etc.) is NOT preserved — once we start mechanically
    editing, the canonical source of truth becomes the YAML.
    """
    project_title = doc.get("metadata", {}).get("title") or "Codebase"
    lines: list[str] = []
    lines.append(f"# {project_title} — Skeleton")
    lines.append("")
    lines.append(
        f"> Auto-generated from `skeleton.yaml` (do not hand-edit; edit YAML instead). "
        f"Version {doc['metadata'].get('version', 1)}."
    )
    lines.append("")

    main_stages = [s for s in doc["stages"] if s.get("parent") is None]
    main_stages = [
        s for s in main_stages if not s["id"].startswith(("side-", "crosscut-"))
    ]
    side_stages = [s for s in doc["stages"] if s["id"].startswith("side-") and s.get("parent") is None]
    crosscut_stages = [s for s in doc["stages"] if s["id"].startswith("crosscut-")]

    def _render_stage(stage: dict, level: int) -> None:
        heading_hashes = "#" * (level + 1)
        lines.append(f"{heading_hashes} {stage['title']}  `{stage['id']}`")
        lines.append("")
        lines.append(stage["description"])
        lines.append("")
        for child_id in stage.get("children", []):
            child = next((s for s in doc["stages"] if s["id"] == child_id), None)
            if child:
                _render_stage(child, level + 1)

    lines.append("## Main Flow")
    lines.append("")
    for s in main_stages:
        _render_stage(s, 2)

    if side_stages:
        lines.append("## Side Flows")
        lines.append("")
        for s in side_stages:
            _render_stage(s, 2)

    if crosscut_stages:
        lines.append("## Cross-cutting Concerns")
        lines.append("")
        for s in crosscut_stages:
            _render_stage(s, 2)

    if doc.get("state_registers"):
        lines.append("## State Registers")
        lines.append("")
        lines.append("| ID | Semantics |")
        lines.append("|---|---|")
        for r in doc["state_registers"]:
            lines.append(f"| `{r['id']}` | {r['semantics']} |")
        lines.append("")

    if doc.get("subsystems"):
        lines.append("## Subsystems")
        lines.append("")
        lines.append("| ID | Role |")
        lines.append("|---|---|")
        for sub in doc["subsystems"]:
            lines.append(f"| `{sub['id']}` | {sub['role']} |")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")


# ─── Convenience helpers used by Apply ────────────────────────────────────────


def stage_ids(doc: dict) -> list[str]:
    return [s["id"] for s in doc["stages"]]


def stage_by_id(doc: dict, sid: str) -> dict | None:
    for s in doc["stages"]:
        if s["id"] == sid:
            return s
    return None


def stage_short_descriptions(doc: dict) -> dict[str, str]:
    """For prompts: stage_id → 'title: first sentence of description'."""
    out: dict[str, str] = {}
    for s in doc["stages"]:
        first_sentence = s["description"].split(". ")[0].rstrip(".")
        out[s["id"]] = f"{s['title']}: {first_sentence}."
    return out


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "command",
        choices=["bootstrap", "render"],
        help="bootstrap: skeleton.md → skeleton.yaml; render: skeleton.yaml → skeleton.md",
    )
    here = Path(__file__).resolve()
    project = here.parents[3]
    ap.add_argument(
        "--md", type=Path, default=project / "handbook/phase2/skeleton.md"
    )
    ap.add_argument(
        "--yaml", type=Path, default=project / "handbook/phase2/skeleton.yaml"
    )
    args = ap.parse_args(argv)

    if args.command == "bootstrap":
        doc = convert_md_to_yaml(args.md, args.yaml)
        print(
            f"wrote {args.yaml}\n  {len(doc['stages'])} stages, "
            f"{len(doc.get('state_registers', []))} registers, "
            f"{len(doc.get('subsystems', []))} subsystems"
        )
    elif args.command == "render":
        doc = load_yaml(args.yaml)
        render_md_from_yaml(doc, args.md)
        print(f"wrote {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
