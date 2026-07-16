# -*- coding: utf-8 -*-
"""Register-appendix narrative generation (one LLM call → all register cards).

The Tier 1 / Tier 2 narratives now go through the actor-critic-reflexion loop
(tier_actors + tier_loop). The register appendix is still a single plain LLM
call with its own content-hash cache — kept here so the orchestrator has one
import for it.
"""
from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
_PHASE2_TOOLS = _HERE.parent / "phase2"   # new layout: phase2 lives beside phase3
for _p in (_PHASE2_TOOLS, _HERE.parent, _HERE.parent / "adapters"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from api_client import Api  # noqa: E402
from config import CACHE_ROOT  # noqa: E402
from project_context import get_project_context  # noqa: E402
from prompts import _NARRATIVE_PROMPT_VERSION, _PROMPTS_BY_LANG  # noqa: E402
from skeleton_view import _registers_brief  # noqa: E402

logger = logging.getLogger(__name__)


def _narrative_cache_path(stage_id: str, key: str) -> Path:
    return CACHE_ROOT / "narrative" / f"{stage_id}_{key}.md"


def gen_register_appendix(api: Api, skeleton: dict, refresh: bool, lang: str) -> str:
    """One LLM call produces all register cards. Content-hash cached."""
    all_stages = []
    for s in skeleton.get("stages", []):
        sid = s.get("id", "")
        title = s.get("title", "") or s.get("role", "")
        desc1 = (s.get("description") or s.get("role", "")).split(".")[0][:160]
        all_stages.append(f"- {sid} · {title}: {desc1}")
    ctx = get_project_context()
    payload = (
        _NARRATIVE_PROMPT_VERSION
        + "|lang=" + lang
        + "|project=" + ctx.name + "|" + ctx.brief
        + "|" + _registers_brief(skeleton)
        + "|" + "\n".join(all_stages)
    )
    key = hashlib.sha1(payload.encode()).hexdigest()[:12]
    cached = _narrative_cache_path(f"register-appendix_{lang}", key)
    if not refresh and cached.exists():
        return cached.read_text(encoding="utf-8")

    prompt = _PROMPTS_BY_LANG[lang]["register_appendix"].format(
        project_name=ctx.name,
        project_block=ctx.block(lang),
        registers_full=_registers_brief(skeleton),
        all_stages_brief="\n".join(all_stages),
    )
    logger.info("Register appendix LLM call (%s)", lang)
    result = api.call(prompt)
    text = result.raw_text.strip()
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
    return text
