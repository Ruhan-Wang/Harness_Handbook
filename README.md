# Harness Handbook

**English** | [中文](README.zh-CN.md)

[![Blog](https://img.shields.io/badge/Blog-105864?style=for-the-badge)](https://ruhan-wang.github.io/Harness-Handbook/)
[![arXiv](https://img.shields.io/badge/arXiv-2607.13285-b31b1b?style=for-the-badge&logo=arxiv&logoColor=white)](https://arxiv.org/abs/2607.13285)
[![Hugging Face Daily Paper](https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Daily%20Paper-ffd21e?style=for-the-badge)](https://huggingface.co/papers/2607.13285)

Turn any codebase into a navigable **handbook**, then use that handbook to help a
code agent find *every* place a change needs to touch.

There are two parts:

1. **Generate a handbook** from your source — a structured, stage-by-stage map of
   the codebase (markdown + optional HTML).
2. **Use the handbook as a helper** — attach it to a code agent's planner and
   measure how much better the agent localizes edits with it than without.

```
handbook_generate_large/    generate a handbook for a LARGE codebase
handbook_generate_small/    generate a handbook for a SMALL codebase
handbook_as_helper/         use a handbook as a code-agent planner + resync
```

Each folder has its own detailed `README.md`; this page is the end-to-end guide.

### Demo & examples

- **[Handbook Studio](https://ruhan-wang.github.io/Harness-Handbook/studio/index.html)** — interactive demo for browsing a handbook
- **[Terminus 2 Handbook](https://ruhan-wang.github.io/Harness-Handbook/terminus-handbook/index.html)** — example handbook generated for Terminus 2

<p align="center">
  <a href="https://ruhan-wang.github.io/Harness-Handbook/studio/index.html">
    <img src="assets/handbook-studio.png" alt="Handbook Studio demo" width="720"/>
  </a>
  <br/>
  <em>Handbook Studio</em>
</p>

<p align="center">
  <a href="https://ruhan-wang.github.io/Harness-Handbook/terminus-handbook/index.html">
    <img src="assets/terminus-handbook.png" alt="Terminus 2 example handbook" width="720"/>
  </a>
  <br/>
  <em>Example: Terminus 2 Handbook</em>
</p>

---

## 0. Setup

```bash
git clone <your-repo-url>
cd Harness_Handbook          # or whatever you named the clone

python3 -m venv .venv && source .venv/bin/activate
pip install tree-sitter tree-sitter-language-pack pyyaml requests markdown pygments
```

**LLM access.** Every phase that isn't pure static analysis calls an LLM. Both the
**generators** (`handbook_generate_*/**/api_client.py`) and the **helper** (its code
agent + resync) talk to the same **OpenAI-compatible** endpoint, configured with the
standard OpenAI env vars — nothing is hardcoded:

```bash
export OPENAI_API_KEY=sk-...                        # your OpenAI API key (required)
# optional overrides:
export OPENAI_MODEL=gpt-4o-mini                     # default: gpt-4o-mini
export OPENAI_BASE_URL=https://api.openai.com/v1    # default; or any OpenAI-compatible
                                                    # endpoint (self-hosted vLLM, a proxy, …)
```

Any OpenAI-compatible endpoint works — point `OPENAI_BASE_URL` at it (a local vLLM,
LiteLLM, etc.) and use `OPENAI_API_KEY=EMPTY` if it needs no key. *Phase 1 of the
generators needs no LLM* — you can always run it first to sanity-check the parser.
The `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` names are still honored as overrides.

---

## 1. Generate a handbook

Pick the pipeline that matches your codebase. Both support Python, Rust,
TypeScript, Go (plus Starlark / Shell / PowerShell), and `--lang auto` to detect
and merge everything under the source root.

### 1A. Large codebase → `handbook_generate_large/`

Bottom-up, **file-as-leaf**: read *every* file, synthesize an ordered stage
skeleton, then narrate leaves-up to a system overview. Coverage is complete by
construction — no file is silently dropped, and no hand-written skeleton is
required.

```bash
cd handbook_generate_large

# (optional) Phase 1 only — static call graph, no LLM. Good smoke test.
python3 run.py --lang auto --source-root /path/to/repo --work-dir work/repo --phase 1

# Full run: deep per-file read → doctor synthesis → organize → narrate + HTML
python3 run.py \
    --source-root /path/to/repo \
    --work-dir work/repo \
    --read-detail deep --read-batch-size 1 --read-workers 100 \
    --synth-mode doctor --doctor-workers 32 --doctor-llm-workers 100 \
    --organize-workers 100 \
    --phase3-html

# Chinese handbook (use a FRESH work-dir; the read pass must be re-run for zh)
python3 run.py --source-root /path/to/repo --work-dir work/repo_zh \
    --read-detail deep --read-batch-size 1 --narrate-lang zh --phase3-html
```

Key flags: `--read-detail deep` (full-file read; pair with `--read-batch-size 1`),
`--synth-mode doctor` (actor-critic skeleton, no extra creds), `--narrate-lang
{en,zh}`, `--phase3-html` (multi-page site), `--phase <all|1|2a|2b|2c|2|3>` to run
a subset.

**Output** → `work/repo/handbook/`:

```
overview.md            system overview
index.md               per-stage index (the routing backbone)
register.md            cross-stage state registers
stages/<id>.md         one page per stage
html/overview.html     the HTML site entry (if --phase3-html)
```

### 1B. Small codebase → `handbook_generate_small/`

Three phases: static graph → LLM classification → LLM narration. This pipeline is
**skeleton-driven**: you supply a short `skeleton.yaml` describing the stage
lifecycle, and describe the project once via `--project-*` so the prose is
tailored to it.

```bash
cd handbook_generate_small

# Phase 1 only (no LLM)
python3 run.py --lang auto --source-root /path/to/repo --work-dir work/repo --phase 1

# Full run (Phases 2 & 3 need the LLM + your skeleton.yaml)
python3 run.py \
    --lang auto \
    --source-root /path/to/repo \
    --skeleton skeletons/repo.yaml \
    --work-dir work/repo \
    --title "Repo Handbook" \
    --project-name "Repo" \
    --project-kind "coding agent" \
    --project-brief "A terminal coding agent that edits code and runs commands." \
    --out-lang en \
    --max-stage-workers 4
```

Key flags: `--skeleton` (required for phase 2+), `--project-name/-kind/-brief[-file]`
(injected into every prompt), `--out-lang {zh,en}`, `--max-stage-workers`,
`--phase <all|1|2|3|1-2|2-3>`.

**Output** → `work/repo/phase3/output/` (markdown handbook + JSON).

> **Which one?** Use **large** when you don't want to author a skeleton and want
> guaranteed full-file coverage. Use **small** when the codebase is small enough
> to describe with a hand-written stage skeleton and you want tighter prose.

---

## 2. Use the handbook as a planner → `handbook_as_helper/`

This folder keeps only two things (all eval/scoring/benchmark code was removed):

1. **Handbook as a planner** — turn a handbook into an agent SKILL and give it to
   a code agent's planner, which then localizes the edits for a change request.
2. **Handbook resync** — after code changes, roll the handbook's derived layer
   forward to match a diff.

### 2A. Use a handbook as a planner

```bash
cd handbook_as_helper

# 0. point the agent at your model (see Setup)
export OPENAI_API_KEY=sk-...                  # + optional OPENAI_MODEL / OPENAI_BASE_URL
export EVAL_TARGET=codex                      # which target project (see pipeline/targets.py)

# 1. Build a planner-ready SKILL from a handbook you generated in step 1.
#    --src is the rendered handbook dir (e.g. work/repo/handbook or .../phase3/output).
python handbook_skills/build_skill_from_handbook.py --target codex \
    --src /path/to/rendered/handbook
#    → writes handbook_skills/handbook_skill_codex/ (SKILL.md + references/)
```

Then drive the **sub-agent (map-reduce) handbook planner** from
`pipeline/code_agent_subagent.py`. A parent planner routes with the small handbook
files (SKILL / index / registers) and delegates every deep read (big stage pages,
source files) to a `locator` sub-agent that reads each file in its own context and
returns only a short report — so the parent's context stays small:

```python
import sys; sys.path.insert(0, "pipeline")
from pathlib import Path
from code_agent_subagent import run_query_subagent   # needs NexAU + the LLM_* env above

out = run_query_subagent(
    "<the reviewer's natural-language change request>",
    Path("/path/to/source"),                # the codebase to plan against
    Path("runs/case1/edited"),              # scratch sandbox (git copy of source, then deleted)
    # arm="handbook" by default (the only arm)
)
print(out["plan"])                          # the planner's localization plan
```

The `handbook` arm **is** this sub-agent planner — the only planner in this repo. It
runs **plan-only** (it emits the plan; there is no executor/diff phase). `code_agent.py`
is now just the shared glue it builds on (config loading, the navigation-only handbook
copy, the git sandbox, the agent runner).

### 2B. Resync a handbook after code changes

This is **independent of the planner above.** The planner is plan-only — it never edits
code, so it produces no `edited/` tree or diff. Resync instead rolls the handbook's derived
layer forward to a **real code change** you supply. Prepare a `<case_dir>` containing:

- `edited/` — the changed source tree (e.g. a checkout with a merged PR applied)
- `plan.md` — a description of the change (its declarations drive the reconcile)
- `agent.diff` *(optional)* — the diff of `edited/` vs pristine; an empty diff is skipped

```bash
# roll a handbook's derived layer forward to that change (any language with an adapter)
python pipeline/update_handbook.py <case_dir>
python pipeline/update_handbook.py <case_dir> --no-translate   # skip card translation
```

### What you need to supply

- **A target project.** Each target's *pristine source tree*, language and prompt
  wording live in `pipeline/targets.py` (one entry per project); override the
  source path with `PRISTINE_ROOT` if needed. Set the active target with
  `EVAL_TARGET`.
- See `handbook_as_helper/README.md` for full details (planner usage, resync,
  file table, and every environment variable).

---

## Repository layout

```
Harness_Handbook/
├── README.md                     ← you are here (English)
├── README.zh-CN.md               ← 中文版
├── handbook_generate_large/      large-codebase generator (run.py, phase1/2/3, adapters, build_site.py)
├── handbook_generate_small/      small-codebase generator (run.py, phase1/2/3, adapters, project_context.py)
└── handbook_as_helper/           use a handbook as a planner + resync
    ├── pipeline/                 code_agent_subagent.py (sub-agent planner), code_agent.py, targets.py, update_handbook.py, resync_*, lang_layer.py
    ├── handbook_skills/          build_skill_from_handbook.py (+ other skill builders)
    ├── prompts/                  planner_handbook.md (handbook arm parent), locator_subagent.md (locator sub-agent)
    └── rerun_resync.py           resync helper
```

**Generated artifacts are not committed** (`.gitignore`d) and are recreated by
running the pipelines: `work/`, `site/`, `site_technical_backup/` (generators),
`runs/` and built `handbook_skills/handbook_skill_*/` (helper). No credentials are
committed.
