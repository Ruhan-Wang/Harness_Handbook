# handbook_as_helper

**English** | [中文](README.zh-CN.md) | [Русский](README.ru.md)

Use a generated **handbook** to help a code agent. This module does exactly two
things:

1. **Handbook as a planner** — turn a handbook into an agent SKILL and give it to a
   read-only planner, which localizes *every* edit site for a natural-language change
   request (the scattered, non-obvious ones included). **Plan-only**: it emits a plan;
   it never edits code.
2. **Handbook resync** — after a real code change lands, roll the handbook's derived
   layer (cards, line anchors, code-sites, index) forward to match the change, without
   regenerating the whole handbook.

> The planner is a SINGLE read-only agent that routes with the
> handbook (`SKILL.md` / `index.md` / `registers.md` / `stages/<id>.md`) and reads the
> REAL source itself before emitting a precise, verbatim EDIT plan — no `locator`
> sub-agent, no map-reduce. This is the **only** planner, and it lives in `code_agent.py`.
> (All eval/benchmark/grading code — golden suites, A/B judge, `run_eval.py`, the executor
> phase — was removed.)

---

## Setup

```bash
# from the repo root
python3 -m venv .venv && source .venv/bin/activate
pip install pyyaml requests tree-sitter tree-sitter-language-pack

# the planner is built on NexAU's official example agent — point at that checkout
export NEXAU_CODE_AGENT_DIR=/path/to/NexAU/examples/code_agent   # or a sibling NexAU/ dir

# LLM: any OpenAI-compatible endpoint (planner AND resync share it)
export OPENAI_API_KEY=sk-...                        # required
export OPENAI_MODEL=gpt-4o-mini                     # optional (default: gpt-4o-mini)
export OPENAI_BASE_URL=https://api.openai.com/v1    # optional; or a self-hosted vLLM / proxy
```

For a keyless local endpoint set `OPENAI_API_KEY=EMPTY` (the planner and resync both
require *some* key and fail loudly if none is set). The lower-level `LLM_MODEL` /
`LLM_BASE_URL` / `LLM_API_KEY` names are still honored and win over the `OPENAI_*` ones.

---

## Part 1 — handbook as a planner

### 1. Build a planner-ready SKILL from a handbook

```bash
# generic (any target): assemble handbook_skills/handbook_skill_<target>/ from a
# rendered handbook dir (e.g. work/repo/handbook or .../phase3/output)
python handbook_skills/build_skill_from_handbook.py --target codex \
    --src /path/to/rendered/handbook

# Terminus-2 only: carve the skill from the fully-rendered handbook markdown
python handbook_skills/build_handbook_skill.py
```

Either builds `handbook_skills/handbook_skill_<target>/` = `SKILL.md` + `references/`
(`overview.md`, `index.md`, `registers.md`, `stages/<id>.md`).

### 2. Run the planner for a change request

```python
import sys; sys.path.insert(0, "pipeline")
from pathlib import Path
from code_agent import run_query             # needs NexAU + the OPENAI_*/LLM_* env

out = run_query(
    "<the reviewer's natural-language change request>",
    Path("/path/to/source"),        # pristine codebase to plan against (required)
    Path("runs/case1"),             # scratch sandbox: a git copy of source, then deleted (required)
    # arm="handbook" by default (the only arm)
)
print(out["plan"])                  # the localization plan;  out["diff"] == "" (plan-only)
```

**How it works.** `run_query` git-snapshots `pristine_dir` into `workdir`, builds a
single read-only planner from NexAU's official `code_agent.yaml` (with the navigation
handbook attached by path), runs it over the sandbox, then deletes the sandbox and
returns the plan. The planner routes with the handbook (`SKILL.md` / `index.md` /
`registers.md` / `stages/<id>.md`) and reads the REAL source itself before emitting the
verbatim EDIT plan.

---

## Part 2 — resync a handbook after a code change

Resync is **independent of the planner** (the planner never edits code, so it produces
no diff to resync). You supply a completed *case directory* describing a real change:

```
<case_dir>/
├── edited/       the changed source tree (e.g. a checkout with the PR applied)   [required]
├── plan.md       a description of the change; its declarations drive the reconcile [required]
└── agent.diff    (optional) diff of edited/ vs pristine — an empty diff is skipped
```

```bash
# member-level (default): roll the handbook's derived layer forward to the change
python pipeline/update_handbook.py <case_dir>
python pipeline/update_handbook.py <case_dir> --no-translate   # skip the card-translation LLM step
python pipeline/update_handbook.py <case_dir> --target codex   # pick the target project

# file-level engine, for a large-pipeline skill
HANDBOOK_GEN_SCALE=large python pipeline/update_handbook.py <case_dir>
```

Resync is **multi-language**: it reads spans / syntax gate / rename fingerprint / call
graph from `lang_layer` — Python via `ast`, Rust / TypeScript / Go / … via the
`handbook_generate_small` tree-sitter adapters. It refuses only a language with no
registered adapter. A function-level phase-2 mapping for the target must exist (see
`PHASE2_FINAL` below).

---

## Files

| File | Purpose |
|------|---------|
| `pipeline/targets.py` | **Target-project config layer.** Each project (`terminus2`, `codex`, …) is a `Target`: pristine source path, language, snapshot ignores, prompt wording. Add a project here — no other file changes. |
| `pipeline/code_agent.py` | **The handbook planner** (the `handbook` arm) plus its glue: loads + env-interpolates NexAU's official `code_agent.yaml`, builds the navigation-only handbook copy (`_ensure_nosrc_handbook`), the read-only planner (`_build` / `build_planner`), the throwaway git sandbox (`_snapshot_git` / `_git_diff`, also used by resync) and the tolerant agent runner (`_run_agent`). Bridges `OPENAI_*` → `LLM_*` on import. Entry point: `run_query(query, pristine_dir, workdir, arm="handbook")`. |
| `pipeline/update_handbook.py` | **Resync entry point.** Takes a case dir (`edited/` + `plan.md`) and rolls the handbook forward to it (no agent re-run). Picks member- vs file-level by `HANDBOOK_GEN_SCALE`. |
| `pipeline/resync_handbook.py` | **Member-level resync engine** (A→D: semantic roll → sha verdict → reclassify → handbook writeback). |
| `pipeline/resync_large.py` | **File-level resync engine** for a large-pipeline skill (whole-file leaves; `HANDBOOK_GEN_SCALE=large`). |
| `pipeline/resync_llm.py` | Shared LLM backend for the resync engines (`EnvLLM`: one bare `/chat/completions` POST per call on the same OpenAI-compatible endpoint the agents use). |
| `pipeline/resync_decl.py` | Member-free declaration parser (used by the file-level path so it never imports the member engine). |
| `pipeline/lang_layer.py` | Multi-language substrate: spans, syntax gate, rename fingerprint, call graph (Python via `ast`, others via tree-sitter adapters). |
| `pipeline/_recon_terminus_base.py` | Terminus-2 helper: rebuild the canonical `PHASE2_FINAL` mapping/skeleton YAML from the rendered handbook. |
| `handbook_skills/build_skill_from_handbook.py` | Generic skill builder: assembles `handbook_skill_<target>/` from any rendered handbook dir, so one planner prompt works across targets. |
| `handbook_skills/build_handbook_skill.py` | Terminus-2-specific skill builder: carves the function-card skill from the rendered handbook markdown, with register enrichment. |
| `prompts/planner_handbook.md` | Planner prompt (routes with the handbook, reads the REAL source, emits self-contained verbatim EDIT blocks). |
| `rerun_resync.py` | Niche helper: re-run resync on a completed case by replaying its ledger (Translate-vs-No-Translate ablation). Not part of the normal flow. |

---

## Key environment variables

| Var | Used by | Meaning |
|-----|---------|---------|
| `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_BASE_URL` | planner, resync | the OpenAI (or OpenAI-compatible) endpoint. Defaults: `gpt-4o-mini` / `https://api.openai.com/v1`. Use `OPENAI_API_KEY=EMPTY` for a keyless local endpoint. |
| `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` | planner, resync | lower-level overrides for the same endpoint (win over the `OPENAI_*` vars). |
| `NEXAU_CODE_AGENT_DIR` | planner | path to NexAU's `examples/code_agent` (default: a sibling `NexAU/` checkout). |
| `EVAL_TARGET` | all | active target project (`terminus2` default, `codex`, …). `--target` overrides. |
| `PRISTINE_ROOT` | all | override the target's pristine source path. |
| `HANDBOOK_SKILL_DIR` / `HANDBOOK_RENDERED_DIR` | skill build / planner | override the target's built-skill dir / rendered-handbook source. |
| `NEXAU_TOOL_CALL_MODE` | planner | tool-call format (`xml` / `structured`; default = the yaml's `structured`). |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` / `LLM_MAX_CONTEXT` / `LLM_MAX_ITERATIONS` / `TOOL_OUTPUT_LIMIT` | planner | NexAU tuning knobs (defaults: `0.0` / yaml / `200000` / `300` / `300000`). |
| `LLM_EXTRA_BODY` | planner, resync | raw JSON merged into the request body (via the SDK's `extra_body`). |
| `HANDBOOK_GEN_SCALE` | resync | `large` → the file-level engine; `small` → the member-level engine (default). |
| `HANDBOOK_GEN_ROOT` | resync | explicit path to the phase-2/3 generator whose modules resync drives. |
| `PHASE2_FINAL` | resync | the function-level phase-2 mapping/skeleton dir the member resync reads. |
| `HANDBOOK_REFS` | resync | override the handbook `references/` dir the member resync edits. |
| `HANDBOOK_LARGE_SKILL` | resync | override the large-pipeline skill dir the file-level resync edits. |
| `RESYNC_TRANSLATE` | resync | `0` / `off` skips the card-translation LLM step (same as `--no-translate`). |
| `RESYNC_NARRATE_LANG` | resync | prose language for the file-level (large) resync (`en` / `zh`; default `zh`). |

> **Layout note.** The member-level resync reads its pristine phase-2/3 artifacts from a
> sibling `Harness_Translation/` checkout by default (`PHASE2_FINAL`, `PRISTINE_HANDBOOK_JSON`,
> `UPSTREAM_CACHE`). Set `PHASE2_FINAL` (and, if needed, `HANDBOOK_REFS`) to point at your own
> layout; the phase-3 JSON / cache paths are optional enrichment and are skipped when absent.
