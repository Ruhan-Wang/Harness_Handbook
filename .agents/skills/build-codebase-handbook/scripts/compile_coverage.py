#!/usr/bin/env python3
"""Compile inventory plus non-overlapping stage rules into coverage.json."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path

STAGE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _validate_rules(data: dict) -> tuple[list[dict], dict[str, str]]:
    if data.get("schema_version") != 1:
        raise ValueError("stage rules schema_version must be 1")
    stages = data.get("stages")
    overrides = data.get("overrides", {})
    if not isinstance(stages, list) or not stages:
        raise ValueError("stage rules must contain a non-empty stages array")
    if not isinstance(overrides, dict):
        raise ValueError("overrides must be an object mapping exact paths to stage IDs")

    seen: set[str] = set()
    clean: list[dict] = []
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ValueError(f"stages[{index}] must be an object")
        stage_id = stage.get("id")
        includes = stage.get("include")
        excludes = stage.get("exclude", [])
        if not isinstance(stage_id, str) or not STAGE_ID.fullmatch(stage_id):
            raise ValueError(f"invalid stage ID at stages[{index}]: {stage_id!r}")
        if stage_id in seen:
            raise ValueError(f"duplicate stage ID: {stage_id}")
        if not isinstance(includes, list) or not includes or not all(
            isinstance(item, str) and item for item in includes
        ):
            raise ValueError(f"stage {stage_id} needs a non-empty include string array")
        if not isinstance(excludes, list) or not all(
            isinstance(item, str) and item for item in excludes
        ):
            raise ValueError(f"stage {stage_id} exclude must be a string array")
        seen.add(stage_id)
        clean.append({"id": stage_id, "include": includes, "exclude": excludes})

    clean_overrides: dict[str, str] = {}
    for path, stage_id in overrides.items():
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise ValueError(f"override path must be a non-empty relative path: {path!r}")
        if stage_id not in seen:
            raise ValueError(f"override for {path} uses unknown stage: {stage_id!r}")
        clean_overrides[path.replace("\\", "/")] = stage_id
    return clean, clean_overrides


def compile_coverage(inventory: dict, rule_data: dict) -> dict:
    if inventory.get("schema_version") != 1 or not isinstance(inventory.get("files"), list):
        raise ValueError("inventory must use schema_version 1 and contain a files array")
    stages, overrides = _validate_rules(rule_data)
    inventory_paths = {item.get("path") for item in inventory["files"]}

    unknown_overrides = sorted(set(overrides) - inventory_paths)
    if unknown_overrides:
        raise ValueError(
            "overrides reference paths absent from inventory: " + ", ".join(unknown_overrides)
        )

    assignments: list[dict] = []
    unmatched: list[str] = []
    ambiguous: list[tuple[str, list[str]]] = []
    for item in inventory["files"]:
        path = item.get("path")
        if not isinstance(path, str):
            raise ValueError("every inventory file needs a string path")
        if path in overrides:
            stage_id = overrides[path]
        else:
            matches = [
                stage["id"]
                for stage in stages
                if _matches(path, stage["include"])
                and not _matches(path, stage["exclude"])
            ]
            if not matches:
                unmatched.append(path)
                continue
            if len(matches) > 1:
                ambiguous.append((path, matches))
                continue
            stage_id = matches[0]
        assignments.append(
            {
                "path": path,
                "stage": stage_id,
                "language": item.get("language", "Other text"),
                "lines": item.get("lines", 0),
                "bytes": item.get("bytes", 0),
                "sha256": item.get("sha256", ""),
            }
        )

    if unmatched or ambiguous:
        details = []
        if unmatched:
            details.append(
                "unmatched files:\n  " + "\n  ".join(unmatched[:50])
                + (f"\n  ... and {len(unmatched) - 50} more" if len(unmatched) > 50 else "")
            )
        if ambiguous:
            rendered = [f"{path}: {', '.join(ids)}" for path, ids in ambiguous[:50]]
            details.append(
                "files matching multiple stages:\n  " + "\n  ".join(rendered)
                + (f"\n  ... and {len(ambiguous) - 50} more" if len(ambiguous) > 50 else "")
            )
        raise ValueError("\n".join(details))

    assignments.sort(key=lambda item: item["path"])
    canonical = json.dumps(assignments, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": 1,
        # Keep the generated artifact portable and avoid leaking the builder's
        # machine-specific absolute source path.
        "source_root": ".",
        "inventory_sha256": inventory.get("inventory_sha256"),
        "coverage_sha256": hashlib.sha256(canonical).hexdigest(),
        "summary": {
            "eligible_files": len(assignments),
            "eligible_lines": sum(item["lines"] for item in assignments),
            "stages": {
                stage["id"]: sum(1 for item in assignments if item["stage"] == stage["id"])
                for stage in stages
            },
        },
        "files": assignments,
        "skipped": inventory.get("skipped", []),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = compile_coverage(_load_json(args.inventory), _load_json(args.rules))
    except ValueError as exc:
        print(f"compile-coverage: error: {exc}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    summary = result["summary"]
    print(
        f"compile-coverage: wrote {args.output} "
        f"({summary['eligible_files']} files across {len(summary['stages'])} stages)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
