#!/usr/bin/env python3
"""Build a deterministic, standalone ZIP of this Codex skill."""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "SKILL.md",
    "LICENSE",
    "agents/openai.yaml",
    "references/handbook-format.md",
    "scripts/compile_coverage.py",
    "scripts/inventory.py",
    "scripts/package_skill.py",
    "scripts/validate_handbook.py",
)
EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".zip", ".tmp"}
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _collect_files(root: Path) -> list[Path]:
    missing = [relative for relative in REQUIRED_FILES if not (root / relative).is_file()]
    if missing:
        raise ValueError("missing required skill files: " + ", ".join(missing))

    files: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"refusing to package symlink: {relative.as_posix()}")
        if not path.is_file() or path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def build_zip(root: Path, output: Path, force: bool = False) -> tuple[int, str]:
    root = root.resolve()
    output = output.resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("output path must end in .zip")
    if output.exists() and not force:
        raise ValueError(f"output already exists: {output}; pass --force to replace it")

    files = _collect_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()

    digest = hashlib.sha256()
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in files:
                relative = path.relative_to(root).as_posix()
                archive_path = f"{root.name}/{relative}"
                data = path.read_bytes()
                info = zipfile.ZipInfo(archive_path, date_time=FIXED_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
                digest.update(archive_path.encode("utf-8"))
                digest.update(b"\0")
                digest.update(data)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return len(files), digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output archive",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        count, content_hash = build_zip(SKILL_ROOT, args.output, args.force)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"package-skill: error: {exc}", file=sys.stderr)
        return 2
    print(f"package-skill: wrote {args.output.resolve()} ({count} files)")
    print(f"package-skill: content-sha256 {content_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
