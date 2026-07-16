# -*- coding: utf-8 -*-
"""Phase 3 paths + UI strings.

Multilang change vs. the legacy phase3: paths are no longer hardcoded to one
repo. They resolve from environment variables (injected by run.py), falling
back to sensible defaults so the module still imports standalone. Every var is
optional; run.py sets them per project/language.

  HANDBOOK_SOURCE_ROOT   source tree root (where snippets are sliced from)
  HANDBOOK_PHASE2_FINAL  dir holding the converged mapping.yaml + skeleton.yaml
  HANDBOOK_PHASE3_ROOT   base for cache/ and output/
  HANDBOOK_CACHE_ROOT    override cache dir (default: PHASE3_ROOT/cache)
  HANDBOOK_OUTPUT_ROOT   override output dir (default: PHASE3_ROOT/output)
  HANDBOOK_TITLE         H1 title of the handbook (default: "Handbook")
"""
from __future__ import annotations

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _env_path(name: str, default: Path) -> Path:
    val = os.environ.get(name)
    return Path(val) if val else default


# Default base: a sibling work dir under the new pipeline, so a bare import
# still yields valid (if empty) paths instead of pointing at a foreign repo.
_DEFAULT_ROOT = _HERE.parent / "work"

SOURCE_ROOT = _env_path("HANDBOOK_SOURCE_ROOT", _DEFAULT_ROOT / "source")
PHASE2_FINAL = _env_path("HANDBOOK_PHASE2_FINAL", _DEFAULT_ROOT / "phase2" / "iterations" / "final")
PHASE3_ROOT = _env_path("HANDBOOK_PHASE3_ROOT", _DEFAULT_ROOT / "phase3")
CACHE_ROOT = _env_path("HANDBOOK_CACHE_ROOT", PHASE3_ROOT / "cache")
OUTPUT_ROOT = _env_path("HANDBOOK_OUTPUT_ROOT", PHASE3_ROOT / "output")

_TITLE = os.environ.get("HANDBOOK_TITLE", "Handbook")

# Section labels that are NOT LLM-generated, keyed by lang. The title is
# project-specific (set via HANDBOOK_TITLE); the rest are fixed UI text.
UI_STRINGS = {
    "zh": {
        "title": _TITLE,
        "overview": "🗺️ 系统总览",
        "registers": "🔄 状态流动总览",
        "fns": "函数细节",
    },
    "en": {
        "title": _TITLE,
        "overview": "🗺️ System Overview",
        "registers": "🔄 State Flow Reference",
        "fns": "Function details",
    },
}
