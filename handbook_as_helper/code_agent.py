"""code_agent.py — two-phase code agent built on NexAU's official example agent.

Pipeline per query:
    1. PLAN     — a read-only planner explores the code and emits a natural-language
                  plan of the edits (file : location → what change, why). It does NOT
                  edit anything. The handbook Skill is attached HERE in arm B (planning
                  is where the handbook is meant to help).
    2. EXECUTE  — an executor agent implements that plan by editing the files. It has
                  the full official tool set, NO handbook, and is told to follow the
                  plan faithfully.
    3. DIFF     — `git diff` of the executor's edits.

run_query returns {"plan": <NL plan>, "diff": <git diff>}.

The agent is NexAU's official `examples/code_agent` config; we apply only minimal,
documented glue (drop tracers; per-phase tool set + prompt; temperature; A/B handbook
switch). A/B differs only in the PLANNER: arm B attaches the handbook skill AND uses a
prompt that requires using it (planner_handbook.md); arm A uses the neutral planner.md
with no skill. The executor (and its prompt) is identical in both arms.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from nexau import Agent, AgentConfig

# git invocation with a fixed identity (the sandbox is a throwaway repo)
_GIT = ["git", "-c", "user.email=eval@local", "-c", "user.name=eval"]

HERE = Path(__file__).resolve().parent
HANDBOOK_SKILL = HERE / "handbook_skill"
PROMPTS = HERE / "prompts"
PLANNER_PROMPT = PROMPTS / "planner.md"                  # baseline arm
PLANNER_PROMPT_HANDBOOK = PROMPTS / "planner_handbook.md"  # handbook arm (must use the skill)
EXECUTOR_PROMPT = PROMPTS / "executor.md"

# Official NexAU example agent (overridable for a moved checkout).
OFFICIAL_DIR = Path(
    os.environ.get(
        "NEXAU_CODE_AGENT_DIR",
        HERE.parent.parent / "NexAU" / "examples" / "code_agent",
    )
).resolve()
OFFICIAL_YAML = OFFICIAL_DIR / "code_agent.yaml"

_ENV_RE = re.compile(r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)\}")
_REQUIRED_ENV = ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY")

# read-only subset of the official tools — the planner may explore but not edit.
# (no write_todos: the planner's deliverable is the plan it passes to complete_task, so a
#  todo scratchpad is redundant — and the model sometimes calls it malformed.)
_READONLY_TOOLS = {"read_file", "search_file_content", "list_directory", "complete_task"}

# Tools the executor must NOT have:
#   - ask_user: would block the unattended run.
#   - run_shell_command / BackgroundTaskManage: a shell can `find`/`grep` the WHOLE
#     filesystem and discover the pristine source (mounted read-only at harbor/...), then
#     try to edit THAT instead of the sandbox copy → the edit lands nowhere (empty diff).
#   - list_directory: the executor already has a plan with real file names, so it never
#     needs to browse directories; if it does, it can list a too-broad path (`/`, `..`) and
#     flood on the restricted /proc tree. (The planner keeps list_directory, but constrained
#     to its working directory by the planner prompt.)
#     Editing Python needs only read_file / write_file / replace / search_file_content.
_EXECUTOR_BLOCK = {"ask_user", "run_shell_command", "BackgroundTaskManage", "list_directory"}


def _load_official_dict() -> dict:
    """Read the official yaml and interpolate ${env.X} ourselves.

    Required LLM_* vars must be set (clear error otherwise). Any other referenced var
    (e.g. LANGFUSE_*) defaults to "" so loading never hard-fails on tracing config we
    are about to drop anyway.
    """
    if not OFFICIAL_YAML.exists():
        raise FileNotFoundError(
            f"official code_agent.yaml not found at {OFFICIAL_YAML}. "
            "Set NEXAU_CODE_AGENT_DIR to the examples/code_agent directory."
        )
    missing = [v for v in _REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(f"missing required env vars: {', '.join(missing)}")

    text = OFFICIAL_YAML.read_text()
    text = _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), text)
    return yaml.safe_load(text)


def _build(system_prompt: Path, name: str, keep_tool, keep_sub_agents: bool,
           use_handbook: bool) -> Agent:
    """Build a NexAU agent from the official config + minimal glue."""
    cfg = _load_official_dict()

    cfg.pop("tracers", None)                                   # no tracing
    cfg["tools"] = [t for t in cfg.get("tools", []) if keep_tool(t.get("name"))]
    if not keep_sub_agents:
        cfg.pop("sub_agents", None)
    cfg["system_prompt"] = str(system_prompt)
    cfg["system_prompt_type"] = "jinja"
    cfg["name"] = name
    # temp=0 (greedy): fewer malformed tool calls (less drift into the wrong format) AND
    # reproducible runs. Same for both arms, so it stays A/B-fair. Override via LLM_TEMPERATURE.
    cfg.setdefault("llm_config", {})["temperature"] = float(
        os.environ.get("LLM_TEMPERATURE", "0.0")
    )
    # Token budgets — env-tunable so they can be raised without editing the yaml. These only
    # bound NexAU's own behaviour; the HARD ceiling is how vLLM was launched (--max-model-len)
    # for input and the model's own output cap. Raise these toward, not beyond, that.
    #   LLM_MAX_TOKENS  — max OUTPUT tokens per LLM call (yaml default 32000)
    #   LLM_MAX_CONTEXT — input/context token budget before compaction (yaml default 200000)
    # `or` (not a default arg) so an empty-string env (the apptainer wrapper passes "" when the
    # var is unset) falls back to the yaml value instead of crashing int("").
    cfg["llm_config"]["max_tokens"] = int(
        os.environ.get("LLM_MAX_TOKENS") or cfg["llm_config"].get("max_tokens", 32000)
    )
    cfg["max_context_tokens"] = int(
        os.environ.get("LLM_MAX_CONTEXT") or cfg.get("max_context_tokens", 200000)
    )
    # Model-provider switch for the Opus "ceiling" arm: set LLM_API_TYPE=
    # anthropic_chat_completion (+ LLM_BASE_URL=<opus_proxy>, LLM_MODEL=claude-opus-4-8). When
    # unset, stays the official openai_chat_completion (the local vLLM Qwen). base_url / model /
    # api_key already come from the LLM_* env via the yaml ${env.X} interpolation.
    if os.environ.get("LLM_API_TYPE"):
        cfg.setdefault("llm_config", {})["api_type"] = os.environ["LLM_API_TYPE"]
        # Force non-streaming for the gateway-backed Opus arm: the gateway's SSE support is
        # unverified, and batch grading needs no live stream. Non-streaming is the path the
        # gateway example uses, so it's the robust choice. (NexAU handles both.)
        cfg["llm_config"]["stream"] = False
        # Claude Opus 4.8 rejects `temperature` ("deprecated for this model"). Drop it from the
        # config AND request. (Opus is effectively deterministic at its default.)
        cfg["llm_config"].pop("temperature", None)
        cfg["llm_config"]["additional_drop_params"] = ["temperature"]
    # Raise the tool-output truncation so a single read_file returns a whole file without
    # truncation. The biggest reads are ~85k (handbook stage file subsys-tmux-internal.md)
    # and ~83k (terminus_2.py itself) — at the old 50k cap BOTH were cut in half, so the
    # planner silently missed later-line sites (and the tmux ceiling). 120k clears both, well
    # within the judge/agent context window. Applied to both agents → symmetric across A/B.
    for mw in cfg.get("middlewares") or []:
        if isinstance(mw, dict) and "LongToolOutput" in str(mw.get("import", "")):
            mw.setdefault("params", {})["max_output_chars"] = int(
                os.environ.get("TOOL_OUTPUT_LIMIT") or "120000"
            )
    if use_handbook:
        if not (HANDBOOK_SKILL / "SKILL.md").exists():
            raise FileNotFoundError(
                f"handbook skill not built yet: {HANDBOOK_SKILL}/SKILL.md missing. "
                "The baseline arm works without it."
            )
        # Give the planner the handbook BY PATH and let it read_file the references directly,
        # instead of the auto-injected LoadSkill tool. Qwen sometimes emits a LoadSkill call in
        # a format the vLLM qwen3_coder parser rejects (deterministic at temp=0), which silently
        # aborts the whole plan. read_file is reliable and keeps progressive disclosure intact
        # (the references are still read on demand, never dumped into context). We inline the
        # prompt as a "string" so the absolute path can be interpolated in.
        cfg["system_prompt"] = system_prompt.read_text() + (
            "\n\n## Where the handbook lives\n"
            f"The handbook is at `{HANDBOOK_SKILL}`. Read `{HANDBOOK_SKILL}/SKILL.md` first (its\n"
            "navigation guide), then the reference files it names — e.g.\n"
            f"`{HANDBOOK_SKILL}/references/index.md`, `{HANDBOOK_SKILL}/references/registers.md`,\n"
            f"and `{HANDBOOK_SKILL}/references/stages/<id>.md` — with `read_file` (absolute paths).\n"
            "There is NO LoadSkill tool; access the handbook only by reading these files.\n"
        )
        cfg["system_prompt_type"] = "string"

    return Agent(config=AgentConfig.from_dict(cfg, base_path=OFFICIAL_DIR))


def build_planner(use_handbook: bool) -> Agent:
    """Read-only planner. Arm B attaches the handbook skill AND uses a prompt that requires
    using it; arm A uses the neutral planner prompt (planning is where the handbook helps)."""
    return _build(
        PLANNER_PROMPT_HANDBOOK if use_handbook else PLANNER_PROMPT,
        "terminus2_planner_" + ("handbook" if use_handbook else "baseline"),
        keep_tool=lambda n: n in _READONLY_TOOLS,
        keep_sub_agents=False,
        use_handbook=use_handbook,
    )


def build_executor() -> Agent:
    """Executor: edits the sandbox to implement the plan. Never gets the handbook (the A/B
    difference lives in the planner). Tools: official set minus _EXECUTOR_BLOCK (no shell,
    so it can't find/edit the pristine source). sub_agents dropped (the official `explore`
    sub-agent uses api_type=openai_responses, which vLLM does not support -> 500)."""
    return _build(
        EXECUTOR_PROMPT,
        "terminus2_executor",
        keep_tool=lambda n: n not in _EXECUTOR_BLOCK,
        keep_sub_agents=False,
        use_handbook=False,
    )


def _snapshot_git(pristine_dir: Path, workdir: Path) -> None:
    """Fresh copy of the terminus_2 subsystem under git, committed as the baseline."""
    if workdir.exists():
        shutil.rmtree(workdir)
    shutil.copytree(pristine_dir, workdir)
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(_GIT + ["add", "-A"], cwd=workdir, check=True)
    subprocess.run(_GIT + ["commit", "-q", "-m", "pristine"], cwd=workdir, check=True)


def _git_diff(workdir: Path) -> str:
    """Diff the executor's edits against the pristine baseline (new files included)."""
    subprocess.run(_GIT + ["add", "-A"], cwd=workdir, check=True)
    out = subprocess.run(
        _GIT + ["diff", "--cached"], cwd=workdir, capture_output=True, text=True, check=True
    )
    return out.stdout


def _dump_trace(agent: Agent, workdir: Path, label: str) -> None:
    """Best-effort debug dump of the agent's message history (every tool call + result)
    to <case>/<label>_trace.json, so we can see what the agent actually did."""
    try:
        import json

        msgs = [m.model_dump() for m in agent.history]
        (workdir.parent / f"{label}_trace.json").write_text(
            json.dumps(msgs, indent=2, ensure_ascii=False, default=str)
        )
    except Exception as e:  # noqa: BLE001
        try:
            (workdir.parent / f"{label}_trace.json").write_text(f"[trace dump failed] {e!r}")
        except Exception:  # noqa: BLE001
            pass


def _run_agent(agent: Agent, task: str, workdir: Path, label: str) -> str:
    """Run an agent on a task; return its final text (str), tolerating errors."""
    try:
        result = agent.run(message=task, context={"working_directory": str(workdir)})
        return result[0] if isinstance(result, tuple) else str(result)
    except Exception as e:  # noqa: BLE001
        print(f"  !! {label} error: {e!r}")
        return f"[{label} error] {e!r}"
    finally:
        _dump_trace(agent, workdir, label)


def run_query(use_handbook: bool, query: str, pristine_dir: Path, workdir: Path) -> dict:
    """Run the two-phase pipeline for one query.

    Returns {"plan": <NL edit plan>, "diff": <git diff of the executed edits>}.
    """
    _snapshot_git(pristine_dir, workdir)

    # Point NexAU's builtin file tools at THIS case's working copy. They resolve relative
    # paths against `sandbox.work_dir`, which defaults to $SANDBOX_WORK_DIR or os.getcwd()
    # (= /root in the container) — NOT the `context={"working_directory": ...}` we pass to
    # agent.run (that key is ignored). Without this, read_file/list_directory/replace operate
    # on an empty /root, so the planner "can't find the code" and the executor edits nothing.
    os.environ["SANDBOX_WORK_DIR"] = str(workdir.resolve())

    # Phase 1 — PLAN (read-only; handbook attached in arm B)
    planner = build_planner(use_handbook)
    plan_task = (
        "A code reviewer has requested the following change to the Terminus-2 harness. "
        "Produce a precise plan of the edits needed (do NOT edit anything yet).\n\n"
        "=== REVIEWER REQUEST ===\n" + query.strip() + "\n========================\n"
    )
    plan = _run_agent(planner, plan_task, workdir, "planner")

    # Phase 2 — EXECUTE the plan (edits the files; no handbook)
    executor = build_executor()
    exec_task = (
        "A code reviewer requested the following change to the Terminus-2 harness, and a "
        "plan of edits was produced. Implement the plan by editing the files in your "
        "working directory.\n\n"
        "=== REVIEWER REQUEST ===\n" + query.strip() + "\n\n"
        "=== PLAN ===\n" + plan.strip() + "\n========================\n"
    )
    _run_agent(executor, exec_task, workdir, "executor")

    return {"plan": plan, "diff": _git_diff(workdir)}
