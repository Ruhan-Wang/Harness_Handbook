# -*- coding: utf-8 -*-
"""Phase 3 paths + UI strings. Single source of truth for where things live."""
from __future__ import annotations

import os
from pathlib import Path

# All roots are env-overridable so Phase 3 can target a repo's .handbook/ folder
# when driven by Handbook Studio. Falls back to the original cluster layout.
_DEFAULT_REPO_ROOT = Path(
    "/apdcephfs_cq11/share_1603164/user/ruhwang/Project/Harness_Translation"
)
REPO_ROOT = Path(os.environ.get("HANDBOOK_REPO_ROOT", str(_DEFAULT_REPO_ROOT)))

SOURCE_ROOT = Path(
    os.environ.get("HANDBOOK_SOURCE_ROOT", str(REPO_ROOT / "harbor/src/harbor/agents/terminus_2"))
)
PHASE2_FINAL = Path(
    os.environ.get("HANDBOOK_PHASE2_FINAL", str(REPO_ROOT / "handbook/phase2/iterations/final"))
)
PHASE3_ROOT = Path(os.environ.get("HANDBOOK_PHASE3_ROOT", str(REPO_ROOT / "handbook/phase3")))
CACHE_ROOT = PHASE3_ROOT / "cache"        # narrative / translate caches
OUTPUT_ROOT = PHASE3_ROOT / "output"      # handbook.json / .md / .html

# Section labels that are NOT LLM-generated, keyed by lang. Values are plain
# text — the renderer adds the heading markup (## / <h2>) around them. Single
# source of truth; render_doc imports this.
UI_STRINGS = {
    "zh": {
        "title": "Terminus 2 Handbook",   # H1 of the whole handbook
        "overview": "🗺️ 系统总览",          # Tier 1 section heading
        "registers": "🔄 状态流动总览",      # register appendix section heading
        "fns": "函数细节",                  # Tier 3 cards sub-heading inside a stage
    },
    "en": {
        "title": "Terminus 2 Handbook",
        "overview": "🗺️ System Overview",
        "registers": "🔄 State Flow Reference",
        "fns": "Function details",
    },
}
