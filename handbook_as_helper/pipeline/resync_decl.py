#!/usr/bin/env python3
"""resync_decl.py — parse the planner's declarations block (generator-free).

`parse_declarations` is a pure regex+json helper. It lives here (with no import
of any handbook generator) so BOTH resync engines can read a plan's will_* block
— including the FILE-level engine, which must not import the member engine
(`resync_handbook.py` hard-fails to import under HANDBOOK_GEN_SCALE=large).
"""
from __future__ import annotations

import json
import re

_DECL_KEYS = ("will_modify", "will_add", "will_remove")


def parse_declarations(plan_text: str) -> dict:
    """The LAST ```json block in the plan that carries any will_* key. Tolerant: a
    missing/broken block degrades to empty lists."""
    out: dict[str, list[str]] = {k: [] for k in _DECL_KEYS}
    for m in re.finditer(r"```json\s*(.*?)```", plan_text, re.S):
        try:
            d = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(d, dict) and any(k in d for k in _DECL_KEYS):
            for k in _DECL_KEYS:
                v = d.get(k)
                out[k] = [str(q) for q in v if isinstance(q, str)] \
                    if isinstance(v, list) else []
    return out
