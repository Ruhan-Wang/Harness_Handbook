"""code_agent.py — the handbook planner, built on NexAU's official example agent.

This IS the `handbook` arm (the "recall" flat planner): a SINGLE read-only agent that
routes with the navigation handbook (SKILL/index/registers/stage pages) and reads the
REAL source itself before emitting a precise, verbatim EDIT plan. There is no locator
sub-agent and no map-reduce — the planner accumulates its own reads. PLAN-ONLY: only the
plan is produced (there is no executor phase). Entry point:

    from code_agent import run_query
    out = run_query(query, pristine_dir, workdir)   # arm="handbook" by default

Building blocks (also reused by the resync step, `update_handbook.py`):
  - `_load_official_dict`     load + env-interpolate the official `code_agent.yaml`
  - `_ensure_nosrc_handbook`  build the navigation-only ("locator + Relations") handbook copy
  - `_build` / `build_planner`  assemble the read-only handbook planner
  - `_snapshot_git` / `_git_diff`  throwaway git sandbox + diff
  - `_run_agent` / `_dump_trace`   run an agent tolerantly + dump its message trace
  - `_READONLY_TOOLS`, `TARGET`, path constants

The agent is NexAU's official `examples/code_agent` config with minimal, documented glue
(drop tracers; read-only tool set; temperature/budgets; attach the handbook skill by path).
Everything project-specific comes from the active `Target` (targets.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


def _configure_openai_env() -> None:
    """Public OpenAI-style configuration.

    The underlying NexAU config interpolates ${env.LLM_MODEL/LLM_BASE_URL/LLM_API_KEY},
    so we accept the STANDARD OpenAI env vars and map them onto those, defaulting to the
    public OpenAI endpoint:

        OPENAI_API_KEY   (required)         -> LLM_API_KEY
        OPENAI_MODEL     (default gpt-4o-mini) -> LLM_MODEL
        OPENAI_BASE_URL  (default https://api.openai.com/v1) -> LLM_BASE_URL

    An explicit LLM_* always wins (setdefault), so any OpenAI-compatible endpoint — a
    self-hosted vLLM, a proxy, Azure OpenAI, etc. — still works by setting LLM_BASE_URL
    (or OPENAI_BASE_URL) directly.
    """
    os.environ.setdefault(
        "LLM_BASE_URL", os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    os.environ.setdefault("LLM_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        os.environ.setdefault("LLM_API_KEY", key)


_configure_openai_env()

HERE = Path(__file__).resolve().parent
HELPER_ROOT = HERE.parent
REPO_ROOT = HELPER_ROOT.parent
for _pkg_root in (REPO_ROOT / "NexAU", HELPER_ROOT / "NexAU"):
    if _pkg_root.exists() and str(_pkg_root) not in sys.path:
        sys.path.insert(0, str(_pkg_root))

# Agent/AgentConfig build the read-only planner (via `AgentConfig.from_dict`); Agent is
# also used in this module's type hints.
from nexau import Agent, AgentConfig  # noqa: E402,F401  (after the env bridge)

from targets import get_target  # noqa: E402  (the target-project config layer)

# git invocation with a fixed identity (the sandbox is a throwaway repo)
_GIT = ["git", "-c", "user.email=eval@local", "-c", "user.name=eval"]

# The active target project (terminus2 by default; codex / others via EVAL_TARGET).
# Everything project-specific — pristine path, source language, prompt wording — comes
# from here, so this module stays generic.
TARGET = get_target()
HANDBOOK_SKILL = TARGET.handbook_skill
# Navigation-only handbook, built on demand from HANDBOOK_SKILL by _ensure_nosrc_handbook():
# each function card is collapsed to its locator (`<summary>`) line plus its **Relations**
# block (callers/callees/register read-write sites — the cross-reference layer). The planner
# routes with this address book, then reads the REAL source by the anchor. (For handbooks
# that are plain markdown rather than `<details>` cards, the collapse is a no-op and the
# skill is used as-is.)
HANDBOOKS_ROOT = HELPER_ROOT / "handbook_skills"
HANDBOOK_SKILL_NOSRC_REL = HANDBOOKS_ROOT / f"handbook_skill_nosrc_rel_{TARGET.name}"  # summary + Relations
PROMPTS = HELPER_ROOT / "prompts"
# The handbook ("recall" flat) arm's planner prompt: route with the handbook, read the real
# source, emit self-contained verbatim EDIT blocks.
PLANNER_PROMPT_HANDBOOK = PROMPTS / "planner_handbook.md"

# Official NexAU example agent (overridable for a moved checkout).
# Resolve to the first candidate that exists so this survives folder moves: the NexAU
# checkout has lived at both <repo>/NexAU and one level up. NEXAU_CODE_AGENT_DIR overrides.
def _resolve_official_dir() -> Path:
    env = os.environ.get("NEXAU_CODE_AGENT_DIR")
    if env:
        return Path(env).resolve()
    for base in (HELPER_ROOT, REPO_ROOT):
        cand = base / "NexAU" / "examples" / "code_agent"
        if cand.exists():
            return cand.resolve()
    return (REPO_ROOT / "NexAU" / "examples" / "code_agent").resolve()


OFFICIAL_DIR = _resolve_official_dir()
OFFICIAL_YAML = OFFICIAL_DIR / "code_agent.yaml"

_ENV_RE = re.compile(r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)\}")
_REQUIRED_ENV = ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY")

# read-only subset of the official tools — the planner may explore but not edit.
# (no write_todos: the planner's deliverable is the plan it passes to complete_task, so a
#  todo scratchpad is redundant — and the model sometimes calls it malformed.)
_READONLY_TOOLS = {"read_file", "search_file_content", "list_directory", "complete_task"}


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
        raise EnvironmentError(
            f"missing required env vars: {', '.join(missing)}. "
            "Set OPENAI_API_KEY (and optionally OPENAI_MODEL / OPENAI_BASE_URL), or the "
            "LLM_* equivalents directly.")

    text = OFFICIAL_YAML.read_text()
    text = _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), text)
    return yaml.safe_load(text)


# Collapse each function card to its LOCATOR LINE: keep `<details id>` + the `<summary>`
# (qualname + file.py:start-end anchor + one-line role) and drop the body (What/Interface/
# Execution-flow/Design-decisions/Source). keep_rel additionally preserves each card's
# **Relations** block (callers/callees/register read-write sites — the cross-reference
# layer that helps find scattered cold-file sites). The handbook becomes an address book;
# the planner uses it (plus index.md / registers.md) to locate, then reads the REAL source
# by the anchor for every detail.
_CARD_TO_SUMMARY = re.compile(
    r'(<details id="[^"]*">\s*<summary>.*?</summary>).*?(</details>)', re.S)
_RELATIONS_BLOCK = re.compile(r"\*\*Relations\*\*.*?(?=\n</details>)", re.S)


def _ensure_nosrc_handbook(dest: Path, keep_rel: bool) -> None:
    """Build/refresh `dest` = a copy of HANDBOOK_SKILL whose function cards are collapsed to
    their locator (`<summary>`) line (plus **Relations** when keep_rel). index.md and
    registers.md (the routing backbone) are left intact. Rebuilt when missing, stale, or
    built in a different mode.

    PROCESS-SAFE: the eval fans out many cases per arm in parallel (`xargs -P 8`), so the
    first jobs of an arm all call this at once on the SAME dest. A directory lock serializes
    them — one process builds, the others wait and reuse — so concurrent rmtree/copytree
    can't corrupt the copy. Build is atomic (build a sibling .tmp dir, then os.replace)."""
    import shutil
    import time

    src_skill = HANDBOOK_SKILL
    if not (src_skill / "SKILL.md").exists():
        raise FileNotFoundError(
            f"handbook skill not built yet: {src_skill}/SKILL.md missing.")
    stamp = dest / ".built_from"
    want = f"locator keep_relations={keep_rel}"
    src_mtime = max(p.stat().st_mtime for p in src_skill.rglob("*") if p.is_file())

    def fresh() -> bool:
        return (stamp.exists() and stamp.stat().st_mtime >= src_mtime
                and stamp.read_text().strip() == want)

    if fresh():
        return

    lock = dest.parent / (dest.name + ".lock")
    dest.parent.mkdir(parents=True, exist_ok=True)
    STALE_SEC = 600            # a real build takes seconds; older lock ⇒ builder died
    have_lock = False
    deadline = time.time() + 1800                          # generous cap (slow cephfs)
    while time.time() < deadline:
        try:
            lock.mkdir()                                    # atomic acquire
            have_lock = True
            break
        except FileExistsError:
            if fresh():                                     # another process finished it
                return
            try:                                            # reclaim a dead builder's lock
                if time.time() - lock.stat().st_mtime > STALE_SEC:
                    shutil.rmtree(lock, ignore_errors=True)
                    continue
            except OSError:
                continue                                    # lock vanished — retry mkdir
            time.sleep(0.1)
    if not have_lock:
        if fresh():
            return
        raise TimeoutError(f"could not acquire {lock} to build {dest}")
    try:
        if fresh():                                         # re-check under the lock
            return
        # clear any orphan tmp dirs from a crashed prior build of this dest
        for orphan in dest.parent.glob(dest.name + ".tmp.*"):
            shutil.rmtree(orphan, ignore_errors=True)
        tmp = dest.parent / (dest.name + f".tmp.{os.getpid()}")
        shutil.copytree(src_skill, tmp)

        def collapse(m: re.Match) -> str:
            summary, close = m.group(1), m.group(2)
            if keep_rel:
                rel = _RELATIONS_BLOCK.search(m.group(0))
                if rel:
                    return f"{summary}\n\n{rel.group(0).rstrip()}\n{close}"
            return f"{summary}\n{close}"

        for f in (tmp / "references" / "stages").glob("*.md"):
            text = f.read_text()
            new = _CARD_TO_SUMMARY.sub(collapse, text)
            if new != text:
                f.write_text(new)
        (tmp / ".built_from").write_text(want + "\n")
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(tmp, dest)                               # atomic swap-in
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def _snapshot_git(pristine_dir: Path, workdir: Path) -> None:
    """Fresh copy of the target subsystem under git, committed as the baseline.

    Skips the target's `snapshot_ignore` entries (e.g. `.git`, and for a big Cargo
    workspace `target/` — copying build output would be gigabytes per case)."""
    if workdir.exists():
        shutil.rmtree(workdir)
    ignore = shutil.ignore_patterns(*TARGET.snapshot_ignore) if TARGET.snapshot_ignore else None
    shutil.copytree(pristine_dir, workdir, ignore=ignore)
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(_GIT + ["add", "-A"], cwd=workdir, check=True)
    subprocess.run(_GIT + ["commit", "-q", "-m", "pristine"], cwd=workdir, check=True)


def _git_diff(workdir: Path) -> str:
    """Diff the working tree against the pristine baseline (new files included).
    Reused by the resync step (update_handbook.py)."""
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


def _build(system_prompt: Path, name: str, use_handbook: bool,
           handbook_dir: Path | None = None) -> Agent:
    """Build the read-only NexAU planner from the official config + minimal glue.

    The planner keeps only the read-only tools (_READONLY_TOOLS) and has no sub-agents —
    it reads every file it needs itself. `handbook_dir` picks which handbook skill to
    attach when `use_handbook` (default: the full HANDBOOK_SKILL). Every knob below is
    env-tunable so the config can be steered without editing the yaml."""
    cfg = _load_official_dict()

    cfg.pop("tracers", None)                                    # no tracing
    cfg["tools"] = [t for t in cfg.get("tools", []) if t.get("name") in _READONLY_TOOLS]
    cfg.pop("sub_agents", None)                                 # flat planner: no sub-agents
    # Always inline the prompt as a raw STRING (none of our prompts use jinja vars).
    cfg["system_prompt"] = TARGET.render_prompt(system_prompt.read_text())
    cfg["system_prompt_type"] = "string"
    cfg["name"] = name

    # Tool-call mode override (env), default = the yaml's `structured`.
    tcm = os.environ.get("NEXAU_TOOL_CALL_MODE")
    if tcm:
        cfg["tool_call_mode"] = tcm
    # temp=0 (greedy) by default: fewer malformed tool calls + reproducible runs.
    # LLM_NO_TEMPERATURE=1 drops `temperature` entirely (models that reject it, e.g. some
    # gateway-served Claude variants). Otherwise LLM_TEMPERATURE overrides the 0.0 default.
    if os.environ.get("LLM_NO_TEMPERATURE"):
        cfg.setdefault("llm_config", {}).pop("temperature", None)
        cfg.setdefault("llm_config", {}).setdefault("additional_drop_params", [])
        if "temperature" not in cfg["llm_config"]["additional_drop_params"]:
            cfg["llm_config"]["additional_drop_params"].append("temperature")
    else:
        cfg.setdefault("llm_config", {})["temperature"] = float(
            os.environ.get("LLM_TEMPERATURE", "0.0")
        )
    # Token budgets (env-tunable; `or` so an empty-string env falls back to the yaml value):
    #   LLM_MAX_TOKENS  — max OUTPUT tokens per LLM call (yaml default 32000)
    #   LLM_MAX_CONTEXT — input/context token budget before compaction (yaml default 200000)
    #   LLM_MAX_ITERATIONS — max tool-call iterations before force-stop (yaml default 300);
    #     a weaker planner reading a LARGE handbook can exhaust the default before finishing.
    cfg["llm_config"]["max_tokens"] = int(
        os.environ.get("LLM_MAX_TOKENS") or cfg["llm_config"].get("max_tokens", 32000)
    )
    cfg["max_context_tokens"] = int(
        os.environ.get("LLM_MAX_CONTEXT") or cfg.get("max_context_tokens", 200000)
    )
    cfg["max_iterations"] = int(
        os.environ.get("LLM_MAX_ITERATIONS") or cfg.get("max_iterations", 300)
    )
    # Provider-specific request-body fields, passed verbatim via the OpenAI SDK's extra_body
    # (e.g. DeepSeek V4 Pro thinking toggle: LLM_EXTRA_BODY='{"thinking": {"type": "disabled"}}').
    if os.environ.get("LLM_EXTRA_BODY"):
        cfg.setdefault("llm_config", {}).setdefault("extra_body", {}).update(
            json.loads(os.environ["LLM_EXTRA_BODY"])
        )
    # Prompt caching (COST lever — never changes model outputs). For OpenAI-compatible paths
    # prefix caching is automatic provider-side and this field is an inert no-op; for the
    # Anthropic path it extends the cache window. Override/disable via LLM_CACHE_TTL ("" off).
    _cache_ttl = os.environ.get("LLM_CACHE_TTL", "1h")
    if _cache_ttl:
        cfg.setdefault("llm_config", {})["cache_control_ttl"] = _cache_ttl
    # Model-provider switch: LLM_API_TYPE=anthropic_chat_completion (+ LLM_BASE_URL / LLM_MODEL
    # for a gateway-served Claude). When unset, stays the official openai_chat_completion.
    if os.environ.get("LLM_API_TYPE"):
        cfg.setdefault("llm_config", {})["api_type"] = os.environ["LLM_API_TYPE"]
        cfg["llm_config"]["stream"] = False
        cfg["llm_config"].pop("temperature", None)
        cfg["llm_config"]["additional_drop_params"] = ["temperature"]
    # Raise the tool-output truncation so a single read_file returns a whole file without
    # truncation (override via TOOL_OUTPUT_LIMIT).
    for mw in cfg.get("middlewares") or []:
        if isinstance(mw, dict) and "LongToolOutput" in str(mw.get("import", "")):
            mw.setdefault("params", {})["max_output_chars"] = int(
                os.environ.get("TOOL_OUTPUT_LIMIT") or "300000"
            )

    if use_handbook:
        hb = handbook_dir or HANDBOOK_SKILL
        if not (hb / "SKILL.md").exists():
            raise FileNotFoundError(f"handbook skill not built yet: {hb}/SKILL.md missing.")
        # Give the planner the handbook BY PATH and let it read_file the references directly
        # (progressive disclosure), instead of an auto-injected LoadSkill tool. The
        # disambiguation reference is named only when present, so its absence changes nothing.
        disambig_line = (
            f"`{hb}/references/disambiguation.md` (search-word disambiguation), "
            if (hb / "references" / "disambiguation.md").exists() else ""
        )
        cfg["system_prompt"] = TARGET.render_prompt(system_prompt.read_text()) + (
            "\n\n## Where the handbook lives\n"
            f"The handbook is at `{hb}`. Read `{hb}/SKILL.md` first (its\n"
            "navigation guide), then the reference files it names — e.g.\n"
            f"`{hb}/references/index.md`, {disambig_line}`{hb}/references/registers.md`,\n"
            f"and `{hb}/references/stages/<id>.md` — with `read_file` (absolute paths).\n"
            "There is NO LoadSkill tool; access the handbook only by reading these files.\n"
        )
        cfg["system_prompt_type"] = "string"

    return Agent(config=AgentConfig.from_dict(cfg, base_path=OFFICIAL_DIR))


def build_planner(arm: str = "handbook") -> Agent:
    """Read-only handbook ("recall" flat) planner: attaches the navigation handbook
    (summary + Relations) and a prompt that routes via the handbook but reads the REAL
    source before planning. Only the `handbook` arm exists in this repo."""
    if arm != "handbook":
        raise ValueError(f"unknown arm {arm!r}; expected 'handbook'")
    _ensure_nosrc_handbook(HANDBOOK_SKILL_NOSRC_REL, keep_rel=True)  # summary + Relations
    return _build(
        PLANNER_PROMPT_HANDBOOK,
        f"{TARGET.name}_planner_{arm}",
        use_handbook=True,
        handbook_dir=HANDBOOK_SKILL_NOSRC_REL,
    )


def run_query(query: str, pristine_dir: Path, workdir: Path, arm: str = "handbook") -> dict:
    """Run the handbook ("recall" flat) planner for one query. PLAN-ONLY (no executor).

    `pristine_dir` is the codebase to plan against; `workdir` is a scratch sandbox (a git
    copy of pristine, created then deleted). Returns {"plan": <NL plan>, "diff": ""}."""
    _snapshot_git(pristine_dir, workdir)
    # Point NexAU's builtin file tools at THIS case's working copy. They resolve relative
    # paths against `sandbox.work_dir`, which defaults to $SANDBOX_WORK_DIR — NOT the
    # context={"working_directory": ...} we pass to agent.run (that key is ignored).
    os.environ["SANDBOX_WORK_DIR"] = str(workdir.resolve())

    planner = build_planner(arm)
    plan_task = (
        "A code reviewer has requested the following change to the target harness. "
        "Produce a precise plan of the edits needed (do NOT edit anything yet).\n\n"
        "=== REVIEWER REQUEST ===\n" + query.strip() + "\n========================\n"
    )
    plan = _run_agent(planner, plan_task, workdir, "planner")

    shutil.rmtree(workdir, ignore_errors=True)  # plan-only: drop the sandbox
    return {"plan": plan, "diff": ""}
