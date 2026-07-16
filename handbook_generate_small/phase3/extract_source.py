# -*- coding: utf-8 -*-
"""Extract source code snippets by (file, line_range) and verify against sha1.

sha1 算法与 phase2/tools/build_mapping.py:_function_sha1_range 一致：
  snippet = "\n".join(lines[start-1:end]); sha1(snippet.utf-8)

sha1 不匹配立即抛错——这是 NL↔code 可逆性的物理基础。源码改了就必须先重跑 phase2。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Snippet:
    qualname: str
    file: str            # mapping 里的文件名（如 "terminus_2.py"）
    line_range: tuple[int, int]
    text: str            # 抽出的源代码片段
    sha1: str            # 实际 sha1


class Sha1Mismatch(RuntimeError):
    """Raised when extracted source sha1 differs from mapping's recorded sha1.

    Recovery: rerun phase2 against the current source, then phase3 will use fresh
    mapping/skeleton — translations regenerate automatically because cache key
    includes sha1.
    """


def _read_lines(file_path: Path) -> list[str]:
    text = file_path.read_text(encoding="utf-8")
    # splitlines() drops the trailing newline of every line, which matches the
    # join-with-"\n" approach used in phase2. Don't use .split("\n") — that
    # appends a phantom empty element when the file ends with \n and would shift
    # sha1 by a single trailing newline.
    return text.splitlines()


def extract(
    source_root: Path,
    qualname: str,
    file: str,
    line_range: list[int] | tuple[int, int],
    expected_sha1: str | None = None,
) -> Snippet:
    """Slice [start, end] (1-based inclusive) from `source_root / file` and verify."""
    start, end = int(line_range[0]), int(line_range[1])
    if start < 1 or end < start:
        raise ValueError(f"bad line_range {line_range} for {qualname}")

    src_path = source_root / file
    if not src_path.exists():
        raise FileNotFoundError(f"source file missing: {src_path}")

    lines = _read_lines(src_path)
    if end > len(lines):
        raise ValueError(
            f"line_range end={end} exceeds file length {len(lines)} for {qualname}"
        )

    snippet = "\n".join(lines[start - 1 : end])
    actual = hashlib.sha1(snippet.encode("utf-8")).hexdigest()

    if expected_sha1 is not None and actual != expected_sha1:
        raise Sha1Mismatch(
            f"sha1 mismatch for {qualname} at {file}:{start}-{end}\n"
            f"  expected (mapping): {expected_sha1}\n"
            f"  actual   (source):  {actual}\n"
            f"  → rerun phase2 to refresh mapping/skeleton, then rerun phase3"
        )

    return Snippet(
        qualname=qualname,
        file=file,
        line_range=(start, end),
        text=snippet,
        sha1=actual,
    )


def extract_from_member(source_root: Path, member: dict) -> Snippet:
    """Convenience wrapper: extract directly from a mapping.yaml member dict."""
    return extract(
        source_root=source_root,
        qualname=member["qualname"],
        file=member["file"],
        line_range=member["line_range"],
        expected_sha1=member.get("sha1"),
    )
