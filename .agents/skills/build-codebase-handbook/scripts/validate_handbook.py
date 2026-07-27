#!/usr/bin/env python3
"""Validate a generated handbook skill for structure, coverage, and freshness."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from compile_coverage import compile_coverage
from inventory import scan

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _load_json(path: Path, errors: list[str]) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read {path}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path} must contain a JSON object")
        return {}
    return value


def _check_skill_md(skill_dir: Path, errors: list[str]) -> str:
    path = skill_dir / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"cannot read {path}: {exc}")
        return ""
    match = FRONTMATTER.match(text)
    if not match:
        errors.append("SKILL.md needs YAML frontmatter delimited by ---")
        return ""
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            errors.append(f"unsupported SKILL.md frontmatter line: {line!r}")
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    if set(fields) != {"name", "description"}:
        errors.append("SKILL.md frontmatter must contain only name and description")
    if not NAME.fullmatch(fields.get("name", "")):
        errors.append("SKILL.md name must use lowercase letters, digits, and hyphens")
    if not fields.get("description"):
        errors.append("SKILL.md description must not be empty")
    description = fields.get("description", "").lower()
    if "use when" not in description or "do not use" not in description:
        errors.append(
            "SKILL.md description must include positive 'Use when' and "
            "negative 'Do not use' triggers"
        )
    if "references/index.md" not in text or "actual source" not in text.lower():
        errors.append("SKILL.md must route through references/index.md and actual source")
    return fields.get("name", "")


def _check_openai_yaml(skill_dir: Path, skill_name: str, errors: list[str]) -> None:
    path = skill_dir / "agents" / "openai.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    required = ("interface:", "display_name:", "short_description:", "default_prompt:")
    for field in required:
        if field not in text:
            errors.append(f"agents/openai.yaml is missing {field.removesuffix(':')}")
    short = re.search(r'^\s+short_description:\s+"([^"]*)"\s*$', text, re.MULTILINE)
    if not short:
        errors.append("agents/openai.yaml short_description must be quoted")
    elif not 25 <= len(short.group(1)) <= 64:
        errors.append("agents/openai.yaml short_description must be 25–64 characters")
    if skill_name and f"${skill_name}" not in text:
        errors.append("agents/openai.yaml default_prompt must mention the generated skill")


def validate(source_root: Path, skill_dir: Path, excludes: list[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    skill_dir = skill_dir.resolve()
    refs = skill_dir / "references"
    stages_dir = refs / "stages"

    skill_name = _check_skill_md(skill_dir, errors)
    _check_openai_yaml(skill_dir, skill_name, errors)
    required = [
        skill_dir / "agents" / "openai.yaml",
        refs / "overview.md",
        refs / "index.md",
        refs / "registers.md",
        refs / "stage-rules.json",
        refs / "coverage.json",
    ]
    for path in required:
        if not path.is_file():
            errors.append(f"missing required file: {path}")
    if not stages_dir.is_dir():
        errors.append(f"missing stages directory: {stages_dir}")

    coverage = _load_json(refs / "coverage.json", errors)
    rules = _load_json(refs / "stage-rules.json", errors)
    if coverage.get("schema_version") != 1:
        errors.append("coverage.json schema_version must be 1")
    if rules.get("schema_version") != 1:
        errors.append("stage-rules.json schema_version must be 1")

    try:
        current = scan(source_root, excludes)
    except ValueError as exc:
        errors.append(str(exc))
        return errors, warnings

    try:
        expected_coverage = compile_coverage(current, rules)
    except ValueError as exc:
        errors.append(f"cannot compile current stage rules: {exc}")
        expected_coverage = {}

    current_by_path = {item["path"]: item for item in current["files"]}
    covered_files = coverage.get("files", [])
    if not isinstance(covered_files, list):
        errors.append("coverage.json files must be an array")
        covered_files = []

    covered_by_path: dict[str, dict] = {}
    duplicates: set[str] = set()
    for item in covered_files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            errors.append("each coverage file must be an object with a string path")
            continue
        path = item["path"]
        if path in covered_by_path:
            duplicates.add(path)
        covered_by_path[path] = item
    if duplicates:
        errors.append("duplicate coverage paths: " + ", ".join(sorted(duplicates)))

    missing = sorted(set(current_by_path) - set(covered_by_path))
    deleted = sorted(set(covered_by_path) - set(current_by_path))
    stale = sorted(
        path
        for path in set(current_by_path) & set(covered_by_path)
        if current_by_path[path]["sha256"] != covered_by_path[path].get("sha256")
    )
    if missing:
        errors.append("eligible files missing from coverage: " + ", ".join(missing[:50]))
    if deleted:
        errors.append("coverage contains deleted or excluded files: " + ", ".join(deleted[:50]))
    if stale:
        errors.append("coverage hashes are stale: " + ", ".join(stale[:50]))

    rule_stages = rules.get("stages", [])
    stage_ids = {
        item.get("id")
        for item in rule_stages
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    covered_stage_ids = {
        item.get("stage")
        for item in covered_files
        if isinstance(item, dict) and isinstance(item.get("stage"), str)
    }
    if stage_ids != covered_stage_ids:
        errors.append(
            "stage IDs differ between rules and coverage: "
            f"rules={sorted(stage_ids)}, coverage={sorted(covered_stage_ids)}"
        )

    existing_stage_ids = (
        {path.stem for path in stages_dir.glob("*.md")} if stages_dir.is_dir() else set()
    )
    if existing_stage_ids != stage_ids:
        errors.append(
            "stage pages differ from stage rules: "
            f"pages={sorted(existing_stage_ids)}, rules={sorted(stage_ids)}"
        )

    try:
        index_text = (refs / "index.md").read_text(encoding="utf-8")
    except OSError:
        index_text = ""
    for stage_id in sorted(stage_ids):
        link = f"stages/{stage_id}.md"
        count = index_text.count(link)
        if count != 1:
            errors.append(f"index.md must link {link} exactly once; found {count}")

    summary = coverage.get("summary", {})
    if summary.get("eligible_files") != len(covered_by_path):
        errors.append("coverage summary eligible_files does not match files array")
    if coverage.get("inventory_sha256") != current.get("inventory_sha256"):
        errors.append("coverage inventory fingerprint is stale")
    if coverage.get("coverage_sha256") != expected_coverage.get("coverage_sha256"):
        errors.append("coverage assignments differ from current stage rules")

    if current["summary"]["skipped_paths"]:
        warnings.append(
            f"{current['summary']['skipped_paths']} paths were intentionally skipped; "
            "review coverage.json skipped reasons before sharing"
        )
    return errors, warnings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="same additional exclusion passed to inventory.py; repeatable",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    errors, warnings = validate(args.source_root, args.skill_dir, args.exclude)
    for warning in warnings:
        print(f"validate-handbook: warning: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"validate-handbook: error: {error}", file=sys.stderr)
        return 2
    print("validate-handbook: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
