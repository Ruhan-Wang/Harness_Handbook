#!/usr/bin/env python3
"""run_phase3.py — standalone Phase 3 (bottom-up handbook narration).

Thin wrapper over phase3/build_handbook.py, mirroring run_phase1.py. Use
this to build the handbook from existing Phase 2 artifacts without the full
run.py driver:

    python3 run_phase3.py --phase2-dir work/codex/phase2 --out work/codex/handbook \
        --workers 100

Or build just one subtree for a cheap inspection:

    python3 run_phase3.py --phase2-dir work/codex/phase2 --out /tmp/hb22 \
        --subtree stage-22 --workers 32
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "phase3"))
sys.path.insert(0, str(_HERE / "phase2"))
sys.path.insert(0, str(_HERE / "shared"))

import build_handbook  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(build_handbook.main())
