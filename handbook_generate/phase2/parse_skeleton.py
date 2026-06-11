# -*- coding: utf-8 -*-
"""Step 0 — Parse skeleton.md into a closed stage table.

Reads ``phase2/skeleton.md`` and produces an ordered ``stages_table`` mapping
``stage_id`` → ``description``. The IDs are taken from the backticked tokens
inside ``## Stage`` / ``### Sub-stage`` / ``### Side Flow`` / ``### Cross-cut``
headings, plus state-register and subsystem tables.

For Phase 2's LLM step, only stage / sub-stage / side / crosscut IDs are needed.
State-register and subsystem IDs are returned separately for validator use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Heading detection: any heading line that contains a backticked token whose
# value matches the stage-ID prefix we care about.
_STAGE_PREFIXES = ("stage-", "side-", "crosscut-")
_REGISTER_PREFIX = "reg-"
_SUBSYSTEM_PREFIX = "subsys-"

_HEADING_RE = re.compile(r"^#{2,4}\s+(.*)$")
_BACKTICK_ID_RE = re.compile(r"`([a-zA-Z][a-zA-Z0-9.\-]*)`")


@dataclass
class StageEntry:
    stage_id: str
    title: str  # the heading text minus the backticked ID
    description: str  # the first paragraph immediately after the heading

    def short(self) -> str:
        """One-line summary used in LLM prompts."""
        first_sentence = self.description.split(". ")[0]
        if not first_sentence.endswith("."):
            first_sentence += "."
        return f"{self.stage_id} — {self.title}: {first_sentence}"


@dataclass
class SkeletonTable:
    stages: dict[str, StageEntry] = field(default_factory=dict)
    registers: dict[str, str] = field(default_factory=dict)
    subsystems: dict[str, str] = field(default_factory=dict)

    @property
    def stage_ids(self) -> list[str]:
        return list(self.stages.keys())

    @property
    def register_ids(self) -> list[str]:
        return list(self.registers.keys())

    @property
    def subsystem_ids(self) -> list[str]:
        return list(self.subsystems.keys())

    def to_prompt_block(self) -> str:
        """Render the stage list for inclusion in an LLM prompt."""
        lines = ["Available stages (use these IDs exactly):"]
        for entry in self.stages.values():
            lines.append(f"  - {entry.short()}")
        lines.append("")
        lines.append("Available subsystem refs:")
        for sid, desc in self.subsystems.items():
            lines.append(f"  - {sid}: {desc}")
        return "\n".join(lines)


def parse_skeleton(skeleton_path: Path) -> SkeletonTable:
    text = skeleton_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    table = SkeletonTable()
    current_heading_id: str | None = None
    current_heading_title: str = ""
    current_desc_buffer: list[str] = []
    in_description = False

    def flush_current() -> None:
        nonlocal current_heading_id, current_desc_buffer, in_description
        if current_heading_id and any(
            current_heading_id.startswith(p) for p in _STAGE_PREFIXES
        ):
            desc = " ".join(_l.strip() for _l in current_desc_buffer if _l.strip())
            table.stages[current_heading_id] = StageEntry(
                stage_id=current_heading_id,
                title=current_heading_title,
                description=desc,
            )
        current_heading_id = None
        current_desc_buffer = []
        in_description = False

    for raw in lines:
        heading_match = _HEADING_RE.match(raw)
        if heading_match:
            # Hand off the previous heading's description before starting a new one.
            flush_current()
            heading_text = heading_match.group(1).strip()
            id_match = _BACKTICK_ID_RE.search(heading_text)
            if id_match and any(
                id_match.group(1).startswith(p) for p in _STAGE_PREFIXES
            ):
                current_heading_id = id_match.group(1)
                # Heading shape: "Stage `stage-1` — Configuration Crystallization"
                # After stripping the backticked ID, we get "Stage  — Configuration Crystallization".
                # Sequence: strip ID, then drop the heading-type prefix word, then drop the em-dash.
                title = _BACKTICK_ID_RE.sub("", heading_text)
                title = re.sub(
                    r"^\s*(Stage|Sub-stage|Side Flow|Cross-cut)\s*", "", title
                )
                title = re.sub(r"^\s*—\s*", "", title).strip()
                current_heading_title = title
                in_description = True
            continue

        if in_description:
            stripped = raw.strip()
            if stripped.startswith("|") or stripped.startswith("---"):
                # Table row or hr — we've left the description paragraph.
                in_description = False
                continue
            if stripped:
                current_desc_buffer.append(stripped)
            elif current_desc_buffer:
                # Blank line after we've captured something — close the paragraph.
                in_description = False

    flush_current()

    _parse_register_table(text, table)
    _parse_subsystem_table(text, table)
    return table


def _parse_register_table(text: str, table: SkeletonTable) -> None:
    """Pull register IDs from the State Registers markdown table."""
    section = _section_after(text, "## State Registers")
    if not section:
        return
    for row in section.splitlines():
        if not row.startswith("|"):
            continue
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if not cells:
            continue
        # First cell: `reg-...`
        id_match = _BACKTICK_ID_RE.search(cells[0])
        if id_match and id_match.group(1).startswith(_REGISTER_PREFIX):
            # Table shape is `| ID | Semantics |` — after stripping the
            # leading/trailing `|` and splitting on `|`, that's 2 cells.
            # The prior `>= 5` threshold was a wrong guess at column count
            # and silently swallowed every register's semantics.
            semantics = cells[-1] if len(cells) >= 2 else ""
            table.registers[id_match.group(1)] = semantics


def _parse_subsystem_table(text: str, table: SkeletonTable) -> None:
    """Pull subsystem IDs from the Subsystems markdown table."""
    section = _section_after(text, "## Subsystems")
    if not section:
        return
    for row in section.splitlines():
        if not row.startswith("|"):
            continue
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if not cells:
            continue
        id_match = _BACKTICK_ID_RE.search(cells[0])
        if id_match and id_match.group(1).startswith(_SUBSYSTEM_PREFIX):
            # Same shape bug as `_parse_register_table` above: the table is
            # `| ID | Role |` → 2 cells after split.
            role = cells[-1] if len(cells) >= 2 else ""
            table.subsystems[id_match.group(1)] = role


def _section_after(text: str, heading: str) -> str | None:
    """Return the text from ``heading`` up to (but excluding) the next ``## `` line."""
    start = text.find(heading)
    if start == -1:
        return None
    next_h = text.find("\n## ", start + len(heading))
    if next_h == -1:
        return text[start:]
    return text[start:next_h]


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--skeleton",
        default=str(Path(__file__).resolve().parents[1] / "skeleton.md"),
    )
    args = ap.parse_args(list(argv) if argv is not None else None)

    table = parse_skeleton(Path(args.skeleton))
    print(f"Found {len(table.stages)} stages:")
    for entry in table.stages.values():
        print(f"  {entry.stage_id:20s}  {entry.title}")
    print(f"\nFound {len(table.registers)} state registers:")
    for rid in table.registers:
        print(f"  {rid}")
    print(f"\nFound {len(table.subsystems)} subsystems:")
    for sid in table.subsystems:
        print(f"  {sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
