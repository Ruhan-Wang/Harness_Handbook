# handbook_generate_large — file-as-leaf handbook pipeline

**English** | [中文](README.zh-CN.md)

Turns a **large** codebase into a navigable **handbook** (markdown + optional HTML),
bottom-up, with the **FILE as the leaf node**. Every file is read and described, files
are grouped into an ordered stage skeleton, and the whole thing is narrated from the
leaves up to a system overview. Coverage is complete by construction — no file is
silently dropped, and you don't hand-write a skeleton.

## The idea: bottom-up, file as the leaf

1. **Read every file** → a per-file card (purpose; in deep mode a detailed
   description + the graph-derived function inventory with call relations).
2. **Synthesize the stage skeleton** from those cards (an ordered lifecycle spine),
   and assign every file to a stage.
3. **Organize each stage internally** (order + sub-group its files).
4. **Narrate bottom-up**: render file/function detail at the leaves, then LLM-summarize
   sub-stage → stage → system; extract cross-stage state registers.

Stage *order* comes from the call graph (entry points → callers-before-callees), so
the skeleton is a narrative spine, not a blind clustering.

## Pipeline

```
Phase 1   run_phase1.py            source → phase1/graph.json              (no LLM)
Phase 2a  phase2/read_files        read EVERY file → phase2/cards/         (one card/file)
Phase 2b  phase2/synth_stages      cards → phase2/skeleton.yaml + file_stage.json
Phase 2c  phase2/organize_stages   order + group each stage → stage_organization.yaml
Phase 3   phase3/build_handbook    bottom-up narration → handbook/ (md + optional html)
```

## Setup

```bash
pip install tree-sitter tree-sitter-language-pack pyyaml requests markdown pygments

# LLM: any OpenAI-compatible endpoint (Phase 2/3 need it; Phase 1 does not).
export OPENAI_API_KEY=sk-...                        # required (=EMPTY for a keyless local endpoint)
export OPENAI_MODEL=gpt-4o-mini                     # optional (default: gpt-4o-mini)
export OPENAI_BASE_URL=https://api.openai.com/v1    # optional; or a self-hosted vLLM / proxy
```

`markdown` + `pygments` are only needed for the HTML site. The `HANDBOOK_LLM_MODEL` /
`HANDBOOK_LLM_BASE_URL` / `HANDBOOK_LLM_API_KEY` names are still honored as overrides.

## Layout

```
run.py            end-to-end driver (--phase all|1|2a|2b|2c|2|3|comma-list)
run_phase1.py     Phase 1 standalone (static call graph)
run_phase3.py     Phase 3 standalone (narration; reuses Phase 2 artifacts)
ir.py  adapters/  language adapters → language-agnostic IR (rust/python/go/ts/…)
shared/           api_client (OpenAI-compatible LLM), skeleton_yaml, critic, progress
phase1/           build_graph.py
phase2/           read_files, synth_stages, synth_agent, skeleton_doctor_files,
                  file_assign, nav_pack, organize_stages, agent_tools/
phase3/           load_inputs, render_file, rollup, registers, render_html, build_handbook
```

## Phases in detail

### 2a — read every file (`phase2/read_files.py`)
An O(files) batched + parallel pass. `--read-detail deep` reads each file in full and
writes the handbook leaf content: a detailed `description` + a per-function inventory
(qualname / line range / signature / call relations from the graph; the LLM writes
`purpose` / `data_flow` / `relations`). Cards are written incrementally (crash-safe) and
`--resume` skips good ones.

### 2b — synthesize stages (`phase2/synth_stages.py`)
Rolls per-file purposes up to the directory level, hands that + the call-graph entry
points to the LLM, and gets an **ordered** stage skeleton; then assigns every file to a
stage. `--synth-mode`:
- **`oneshot`** (default): one LLM call drafts the skeleton, then assign once.
- **`doctor`**: one-shot draft + an **actor-critic convergence loop**
  (`skeleton_doctor_files`, reuses `shared/critic.py`) that splits / merges / adds stages
  and re-assigns until every file is placed. **No NexAU / `LLM_*` needed.**
- **`agent`**: a NexAU agent drafts the skeleton (needs `LLM_BASE_URL` / `LLM_MODEL` /
  `LLM_API_KEY`), then the same convergence loop. Falls back to oneshot if that endpoint
  is unavailable.

### 2c — organize each stage (`phase2/organize_stages.py`)
For each stage: order its files by call-graph dependency (callers before callees, Kahn)
and split into 2–8 ordered sub-groups. ~O(stages) LLM calls.

### 3 — narration (`phase3/build_handbook.py`)
Post-order walk of the stage tree: render file/function detail at the leaves (no LLM),
LLM-summarize each non-leaf node from its children's summaries, then a system overview.
Also extracts **state registers** (cross-stage global state) and an index. Outputs
`handbook/`: `overview.md`, `index.md` (per-stage with overviews), `register.md`,
`stages/<id>.md`, and optionally a multi-page (`--phase3-html` on `run.py`; `--html` on
`run_phase3.py`) or single-page (`--html-single`) HTML site.

## Usage

```bash
# everything (English): deep read → synth → organize → narrate + HTML
python3 run.py --source-root /path/to/repo --work-dir work/repo \
    --read-detail deep --read-batch-size 1 --read-workers 100 \
    --synth-mode doctor --doctor-workers 32 --doctor-llm-workers 100 \
    --organize-workers 100 --phase3-html

# Chinese handbook (use a FRESH work-dir; 2a must be re-run for zh cards)
python3 run.py --source-root /path/to/repo --work-dir work/repo_zh \
    --read-detail deep --read-batch-size 1 --narrate-lang zh --phase3-html

# phase by phase
python3 run.py --source-root … --work-dir work/repo --phase 1
python3 run.py --source-root … --work-dir work/repo --phase 2a --read-detail deep
python3 run.py --source-root … --work-dir work/repo --phase 2b --synth-mode doctor
python3 run.py --source-root … --work-dir work/repo --phase 2c --organize-workers 100
python3 run_phase3.py --phase2-dir work/repo/phase2 --out work/repo/handbook \
    --lang zh --workers 100 --html
```

`--narrate-lang {en,zh}` controls the language of all handbook-bound prose (file/function
detail, stage/system overviews, register semantics) across 2a/2b/2c/3. `--lang` is
unrelated — it's the source-language hint for Phase 1 (`auto` detects and merges every
supported language under the source root).

## Notes

- **No function-level classification.** This pipeline is file-as-leaf only; the
  per-function path (iterate / pass_a..d) lives in `handbook_generate_small`.
- LLM access is the OpenAI-compatible `Api` in `shared/api_client.py`, configured via
  `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_BASE_URL` (only the `agent` skeleton draft
  uses NexAU instead, via the `LLM_*` env).
- `work/` holds per-project artifacts (graph, cards, skeleton, handbook) and is created
  on demand; it is not committed.
