#!/usr/bin/env python3
"""Create a deterministic, content-safe repository inventory.

The inventory records paths, sizes, line counts, language hints, and SHA-256 hashes.
It never emits file contents and excludes common secret, generated, dependency, and
binary paths by default.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1

DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "coverage",
    ".coverage",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".next",
    ".nuxt",
    ".turbo",
    ".cache",
    "target",
}

DEFAULT_EXCLUDED_GLOBS = (
    ".agents/skills/*-handbook/**",
    ".handbook-work/**",
    "*.pyc",
    "*.pyo",
    "*.class",
    "*.o",
    "*.obj",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
)

SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    "credentials",
    "credentials.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}

SENSITIVE_GLOBS = (
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*credentials*.json",
    "*secret*.json",
)

LANGUAGES = {
    ".py": "Python",
    ".pyi": "Python",
    ".rs": "Rust",
    ".go": "Go",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".c": "C",
    ".h": "C/C++",
    ".cc": "C/C++",
    ".cpp": "C/C++",
    ".cxx": "C/C++",
    ".hpp": "C/C++",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".scala": "Scala",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".fish": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".proto": "Protocol Buffers",
    ".graphql": "GraphQL",
    ".gql": "GraphQL",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".less": "Less",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".md": "Markdown",
    ".mdx": "MDX",
    ".rst": "reStructuredText",
    ".txt": "Text",
    ".json": "JSON",
    ".jsonc": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".xml": "XML",
    ".ini": "INI",
    ".cfg": "Config",
    ".conf": "Config",
    ".properties": "Config",
    ".gradle": "Gradle",
    ".cmake": "CMake",
    ".dockerfile": "Dockerfile",
}

SPECIAL_NAMES = {
    "Dockerfile": "Dockerfile",
    "Makefile": "Make",
    "Rakefile": "Ruby",
    "Gemfile": "Ruby",
    "CMakeLists.txt": "CMake",
    "Justfile": "Just",
    "Procfile": "Procfile",
}


def _posix(path: Path) -> str:
    return path.as_posix().removeprefix("./")


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _language(path: Path) -> str:
    if path.name in SPECIAL_NAMES:
        return SPECIAL_NAMES[path.name]
    return LANGUAGES.get(path.suffix.lower(), "Other text")


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data[:8192]:
        return True
    sample = data[:8192]
    suspicious = sum(
        byte < 9 or (13 < byte < 32) for byte in sample
    )
    return suspicious / len(sample) > 0.10


def _sensitive(rel: str, name: str) -> bool:
    lower_name = name.lower()
    if lower_name in SENSITIVE_NAMES:
        return True
    return _matches(rel.lower(), (pattern.lower() for pattern in SENSITIVE_GLOBS))


def scan(
    source_root: Path,
    extra_excludes: Iterable[str] = (),
    max_bytes: int = 2_000_000,
) -> dict:
    root = source_root.resolve()
    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")

    patterns = (*DEFAULT_EXCLUDED_GLOBS, *extra_excludes)
    files: list[dict] = []
    skipped: list[dict] = []

    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for dirname in sorted(dirs):
            child = current_path / dirname
            rel = _posix(child.relative_to(root))
            if dirname in DEFAULT_EXCLUDED_DIRS:
                skipped.append({"path": f"{rel}/", "reason": "excluded-directory"})
            elif _matches(f"{rel}/", patterns) or _matches(f"{rel}/_", patterns):
                skipped.append({"path": f"{rel}/", "reason": "excluded-pattern"})
            elif child.is_symlink():
                skipped.append({"path": f"{rel}/", "reason": "symlink-directory"})
            else:
                kept_dirs.append(dirname)
        dirs[:] = kept_dirs

        for name in sorted(names):
            path = current_path / name
            rel = _posix(path.relative_to(root))
            if path.is_symlink():
                skipped.append({"path": rel, "reason": "symlink"})
                continue
            if _sensitive(rel, name):
                skipped.append({"path": rel, "reason": "sensitive"})
                continue
            if _matches(rel, patterns):
                skipped.append({"path": rel, "reason": "excluded-pattern"})
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                skipped.append({"path": rel, "reason": f"stat-error:{exc.__class__.__name__}"})
                continue
            if size > max_bytes:
                skipped.append({"path": rel, "reason": "oversize", "bytes": size})
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                skipped.append({"path": rel, "reason": f"read-error:{exc.__class__.__name__}"})
                continue
            if _looks_binary(data):
                skipped.append({"path": rel, "reason": "binary", "bytes": size})
                continue

            files.append(
                {
                    "path": rel,
                    "language": _language(path),
                    "bytes": size,
                    "lines": len(data.splitlines()),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )

    files.sort(key=lambda item: item["path"])
    skipped.sort(key=lambda item: item["path"])
    language_counts = Counter(item["language"] for item in files)
    canonical_files = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()

    return {
        "schema_version": SCHEMA_VERSION,
        "source_root": str(root),
        "inventory_sha256": hashlib.sha256(canonical_files).hexdigest(),
        "summary": {
            "eligible_files": len(files),
            "eligible_lines": sum(item["lines"] for item in files),
            "eligible_bytes": sum(item["bytes"] for item in files),
            "skipped_paths": len(skipped),
            "languages": dict(sorted(language_counts.items())),
        },
        "files": files,
        "skipped": skipped,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, help="write JSON here; stdout if omitted")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="additional repository-relative glob to exclude; repeatable",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=2_000_000,
        help="skip individual files larger than this many bytes",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = scan(args.source_root, args.exclude, args.max_bytes)
    except ValueError as exc:
        print(f"inventory: error: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"inventory: wrote {args.output}")
    else:
        print(payload, end="")

    summary = result["summary"]
    print(
        "inventory: "
        f"{summary['eligible_files']} eligible files, "
        f"{summary['eligible_lines']} lines, "
        f"{summary['skipped_paths']} skipped",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
