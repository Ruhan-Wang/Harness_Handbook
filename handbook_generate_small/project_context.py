# -*- coding: utf-8 -*-
"""Project-level context shared by Phase 2 / Phase 3 prompts.

The multilang pipeline is codebase-agnostic: nothing in the prompts should
hardcode a particular project (the legacy `handbook_generate_terminus` baked in
"Terminus 2", tmux, trajectories, etc.). Instead, the project's identity is
injected at run time and read here from environment variables (set by `run.py`
per project), so every prompt can address the *current* codebase generically.

  HANDBOOK_PROJECT_NAME   short display name, e.g. "Terminus 2" or "Redis"
  HANDBOOK_PROJECT_BRIEF  1-3 sentence description of what the codebase is/does
  HANDBOOK_PROJECT_KIND   noun for the codebase, e.g. "agent harness",
                          "web service", "compiler" (default: "codebase")

All are optional; sensible neutral defaults keep the prompts grammatical even
when nothing is provided.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectContext:
    name: str    # display name used in prose, e.g. "Terminus 2"
    brief: str   # short description (may be empty)
    kind: str    # short noun, e.g. "agent harness", "codebase"

    def block(self, lang: str = "en") -> str:
        """A short, prompt-ready context block describing the project.

        Prepended to actor/critic/tier prompts so the model knows which
        codebase it is documenting without any hardcoded project name.
        """
        if lang == "zh":
            lines = [
                "# 项目背景",
                f"- 项目名称: {self.name}",
                f"- 项目类型: {self.kind}",
            ]
            if self.brief:
                lines.append(f"- 一句话简介: {self.brief}")
            lines.append(
                "以下所有说明都是针对**这个项目**的代码。凡是提到「这个系统 / "
                "本代码库」，都指上面这个项目。"
            )
            return "\n".join(lines)
        lines = [
            "# Project context",
            f"- Name: {self.name}",
            f"- Kind: {self.kind}",
        ]
        if self.brief:
            lines.append(f"- One-line brief: {self.brief}")
        lines.append(
            "Everything below documents THIS project's code. Whenever the text "
            "says \"the system\" / \"this codebase\", it means the project above."
        )
        return "\n".join(lines)


def get_project_context() -> ProjectContext:
    name = (os.environ.get("HANDBOOK_PROJECT_NAME") or "").strip() or "this codebase"
    brief = (os.environ.get("HANDBOOK_PROJECT_BRIEF") or "").strip()
    kind = (os.environ.get("HANDBOOK_PROJECT_KIND") or "").strip() or "codebase"
    return ProjectContext(name=name, brief=brief, kind=kind)
