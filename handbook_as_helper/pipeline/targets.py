"""targets.py — the target-project configuration layer.

This eval helper is GENERIC: it can run the two-phase code agent against any code
harness, not just Terminus-2. Everything project-specific (language, where the
pristine source lives, which golden suite to use, how to snapshot/syntax-check the
tree, and the wording the prompts use) is captured by a `Target` here.

Select the target with the `EVAL_TARGET` env var or `--target` on run_eval.py.
Default is `terminus2`, so prior behavior is unchanged. Add a new project by
registering one more `Target` in `_TARGETS` below — no code changes elsewhere.

Path defaults follow the existing convention: resolve to the first candidate that
exists (so the helper survives folder moves), with an env var override on top.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent                 # .../final_handbook_as_helper/pipeline
HELPER_ROOT = HERE.parent                              # .../final_handbook_as_helper
REPO_ROOT = HELPER_ROOT.parent                         # .../Harness_Handbook
_REPO = REPO_ROOT.parent / "Harness_Translation"       # sibling translation repo (if present)


def _first_existing(*candidates: Path) -> Path:
    """First candidate that exists, else the first (for a clear not-found error)."""
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


@dataclass(frozen=True)
class Target:
    """Everything the eval needs to know about one target project."""

    name: str                         # short id, e.g. "terminus2" / "codex"
    language: str                     # "python" | "rust" | ... (drives the syntax gate)
    source_globs: tuple[str, ...]     # globs for "source files" under the tree
    snapshot_ignore: tuple[str, ...]  # dir/file names to skip when snapshotting the tree
    syntax_mode: str                  # "python" | "command" | "none" (post-apply gate)
    # resolvers (deferred so env overrides are read at call time, not import time)
    _pristine: "object"               # () -> Path : the pristine source root
    _golden: "object"                 # () -> Path : the query suite yaml
    _handbook_skill: "object"         # () -> Path : the skill dir the planner reads
    # optional: a pre-rendered handbook dir to assemble the skill from (else None)
    _handbook_rendered: "object" = field(default=lambda: None)
    # optional shell command (string) to run as the syntax gate when syntax_mode=="command".
    # Run with cwd=<sandbox>; non-zero exit => "broken". $EVAL_SYNTAX_CMD overrides.
    syntax_command: str | None = None
    # prompt wording substituted into the templated prompts (see prompts/*.md placeholders)
    prompt_vars: dict = field(default_factory=dict)

    # -- resolved paths (env override wins) --
    @property
    def pristine_root(self) -> Path:
        env = os.environ.get("PRISTINE_ROOT")
        return Path(env) if env else self._pristine()

    @property
    def golden(self) -> Path:
        env = os.environ.get("GOLDEN")
        return Path(env) if env else self._golden()

    @property
    def handbook_skill(self) -> Path:
        env = os.environ.get("HANDBOOK_SKILL_DIR")
        return Path(env) if env else self._handbook_skill()

    @property
    def handbook_rendered(self) -> Path | None:
        env = os.environ.get("HANDBOOK_RENDERED_DIR")
        if env:
            return Path(env)
        return self._handbook_rendered()

    def render_prompt(self, template: str) -> str:
        """Substitute {{KEY}} placeholders in a prompt template with this target's wording.
        Unknown placeholders are left untouched (so a plain prompt with no vars is a no-op)."""
        out = template
        for k, v in self.prompt_vars.items():
            out = out.replace("{{" + k + "}}", v)
        return out


# ============================================================================
# Registered targets
# ============================================================================

# ---- Terminus-2 (the original target; defaults reproduce prior behavior) ----
_TERMINUS2 = Target(
    name="terminus2",
    language="python",
    source_globs=("*.py",),
    snapshot_ignore=(".git", "__pycache__", ".mypy_cache", ".pytest_cache"),
    syntax_mode="python",
    _pristine=lambda: _first_existing(
        _REPO / "harbor/src/harbor/agents/terminus_2",
        REPO_ROOT / "harbor/src/harbor/agents/terminus_2",
    ),
    _golden=lambda: _first_existing(
        HELPER_ROOT / "golden_task_request" / "terminus2_val_r30.yaml",
        HELPER_ROOT / "golden_task_request" / "terminus2_val.yaml",
        HELPER_ROOT / "terminus2_val_r30.yaml",
        HELPER_ROOT / "terminus2_val.yaml",
        _REPO / "terminus2_roundtrip_golden.yaml",
    ),
    _handbook_skill=lambda: HELPER_ROOT / "handbook_skills" / "handbook_skill_terminus",
    prompt_vars={
        "PROJECT": "Terminus-2",
        "LANGUAGE": "Python",
        "PROJECT_INTRO": "a Python agent harness called Terminus-2",
        "CODEBASE_DESC": (
            "the full Terminus-2 agent harness (several Python modules plus a\n"
            "`templates/` directory). There is no handbook or index — the working directory "
            "IS the source."
        ),
        "BASELINE_READ_STEP": (
            "`read_file` EVERY Python source module IN FULL — do not skip any, do not read "
            "only\n     fragments. Read each `.py` file end to end (`terminus_2.py`, the "
            "parser modules,\n     `tmux_session.py`, and any others present). Only after you "
            "have read all of them do you\n     start reasoning about the change."
        ),
        "PATH_EXAMPLE": "terminus_2.py",
        "PATH_EXAMPLE2": "templates/terminus-json-plain.txt",
        "WHERE_EXAMPLE": "Class.method (~line)",
        "QUALNAME_NOTE": (
            "fully qualified names (`Class.method`, nested as `Class.method.inner`) exactly "
            "as they appear"
        ),
        "DECL_JSON": (
            '{"will_modify": ["Terminus2._run_agent_loop", "Terminus2._check_timeout"],\n'
            ' "will_add":    ["Terminus2._upload_report"],\n'
            ' "will_remove": []}'
        ),
        "VALID_LANG": "valid Python / a valid template (balanced brackets, no broken lines)",
        "REPLACE_INSTRUCTION": (
            "In Terminus2.__init__, allow unbounded episodes by dropping the 1000000 cap."
        ),
        "REPLACE_INSTRUCTION2": (
            "In Terminus2.__init__, make episodes unbounded by dropping the 1000000 default."
        ),
        "REPLACE_OLD": (
            "        self._max_episodes = max_episodes or 1000000\\n"
            "        self._pending_completion = False"
        ),
        "REPLACE_NEW": (
            "        self._max_episodes = max_episodes  # None = unbounded\\n"
            "        self._pending_completion = False"
        ),
    },
)

def _find_rendered_handbook(project: str) -> "object":
    """Return a resolver that finds a rendered handbook dir for `project` under any
    handbook_generate* work tree (the generators move output around between runs).
    Prefers a non-_zh dir with both index.md and stages/."""
    def resolve() -> Path | None:
        cands: list[Path] = []
        for gen in sorted(REPO_ROOT.glob("handbook_generate*")):
            for hb in gen.glob(f"work/{project}*/handbook"):
                if (hb / "index.md").exists() and (hb / "stages").is_dir():
                    cands.append(hb)
        # prefer exact "<project>/handbook" over "<project>_zh/handbook" etc.
        cands.sort(key=lambda p: (p.parent.name != project, str(p)))
        return cands[0] if cands else None
    return resolve


# ---- Codex (codex-rs Rust workspace) ----
_CODEX = Target(
    name="codex",
    language="rust",
    source_globs=("*.rs",),
    # codex-rs is a large Cargo workspace: never copy build output or VCS metadata.
    snapshot_ignore=(".git", "target", "node_modules", ".cargo"),
    # default: no gate (cargo check on the whole workspace is slow + needs the toolchain).
    # Set EVAL_SYNTAX_CMD="cargo check -q" (and syntax_mode is read from env too) to enable.
    syntax_mode="none",
    syntax_command="cargo check -q",
    _pristine=lambda: _first_existing(
        REPO_ROOT / "codex" / "codex-rs",
        _REPO / "codex" / "codex-rs",
    ),
    _golden=lambda: _first_existing(
        HELPER_ROOT / "golden_task_request" / "codex_val.yaml",
        HELPER_ROOT / "golden_task_request" / "codex_val_r30.yaml",
    ),
    _handbook_skill=lambda: HELPER_ROOT / "handbook_skills" / "handbook_skill_codex",
    _handbook_rendered=_find_rendered_handbook("codex"),
    prompt_vars={
        "PROJECT": "Codex",
        "LANGUAGE": "Rust",
        "PROJECT_INTRO": "a Rust coding-agent harness called Codex (the codex-rs workspace)",
        "CODEBASE_DESC": (
            "the Codex Rust workspace (codex-rs): many crates such as `core/`, `exec/`,\n"
            "`tui/`, `protocol/`, `apply-patch/`, each with sources under `src/*.rs`. There "
            "is no handbook or index — the working directory IS the source."
        ),
        "BASELINE_READ_STEP": (
            "the workspace is large, so reading every file is impractical. Instead, use\n"
            "     `search_file_content` to locate the subsystems the request names "
            "(the turn loop in\n     `core/src/session/turn.rs`, the tools under "
            "`core/src/tools/handlers/`, compaction in\n     `core/src/compact*.rs`, etc.), "
            "then `read_file` each candidate file IN FULL before deciding.\n"
            "     Prefer reading whole files over fragments so nothing relevant is missed."
        ),
        "PATH_EXAMPLE": "core/src/session/turn.rs",
        "PATH_EXAMPLE2": "core/src/exec.rs",
        "WHERE_EXAMPLE": "module::function or Type::method (~line)",
        "QUALNAME_NOTE": (
            "fully qualified Rust paths (`module::function`, an impl method as `Type::method`) "
            "as they appear"
        ),
        "DECL_JSON": (
            '{"will_modify": ["session::turn::run_turn", "exec::ExecCapturePolicy::retained_bytes_cap"],\n'
            ' "will_add":    ["session::turn::run_verification_turn"],\n'
            ' "will_remove": []}'
        ),
        "VALID_LANG": "valid Rust (balanced braces, no broken lines)",
        "REPLACE_INSTRUCTION": (
            "In ExecCapturePolicy::retained_bytes_cap, raise the ShellTool byte cap."
        ),
        "REPLACE_INSTRUCTION2": (
            "In ExecCapturePolicy::retained_bytes_cap, raise the ShellTool byte cap."
        ),
        "REPLACE_OLD": (
            "            ExecCapturePolicy::ShellTool => Some(EXEC_OUTPUT_MAX_BYTES),\\n"
            "            ExecCapturePolicy::FullBuffer => None,"
        ),
        "REPLACE_NEW": (
            "            ExecCapturePolicy::ShellTool => Some(EXEC_OUTPUT_MAX_BYTES * 2),\\n"
            "            ExecCapturePolicy::FullBuffer => None,"
        ),
    },
)


_TARGETS = {t.name: t for t in (_TERMINUS2, _CODEX)}


def get_target(name: str | None = None) -> Target:
    """Resolve the active target: explicit `name`, else $EVAL_TARGET, else 'terminus2'."""
    key = (name or os.environ.get("EVAL_TARGET") or "terminus2").strip().lower()
    if key not in _TARGETS:
        raise ValueError(
            f"unknown target {key!r}; registered: {', '.join(sorted(_TARGETS))}. "
            "Add one in targets.py or pass --target."
        )
    return _TARGETS[key]


def target_names() -> list[str]:
    return sorted(_TARGETS)
