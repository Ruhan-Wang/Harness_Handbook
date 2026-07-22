# handbook_generate_small — skeleton-driven handbook pipeline

**English** | [中文](README.zh-CN.md) | [Русский](README.ru.md)

A **project-agnostic** three-phase pipeline (static graph → LLM classification → LLM
narration) with a uniform `LanguageAdapter` front end, so it can target **Python, Rust,
TypeScript, Go** (plus lightweight Starlark / Shell / PowerShell). Best for codebases
small enough to describe with a short hand-written **stage skeleton** and where you want
tightly tailored prose.

The project's identity is injected at run time via `--project-name` / `--project-brief`
/ `--project-kind` (read through `project_context.py`), so nothing is hardcoded — the
handbook is generated for *whatever* codebase you point it at.

## Pipeline

```
Phase 1   run_phase1.py   source → phase1/graph.json                  (no LLM)
Phase 2   phase2/          LLM classification (Critic-Actor iteration)  → stage assignment
Phase 3   phase3/          LLM narration (actor-critic-reflexion, stage-parallel) → handbook
```

Phase 2/3 require the LLM **and** a user-authored `skeleton.yaml` describing the stage
lifecycle.

## Layout

```
handbook_generate_small/
├── project_context.py        # project identity injected into every LLM prompt
├── ir.py                     # language-agnostic IR (FunctionNode/BoundaryNode/CallEdge)
├── adapters/                 # LanguageAdapter ABC + per-language front ends
├── phase1/build_graph.py     # language-agnostic graph assembly + emitters
├── run_phase1.py             # Phase 1 CLI
├── phase2/                   # LLM classification (Critic-Actor); api_client lives here
├── phase3/                   # LLM narration (actor-critic-reflexion), stage-parallel
└── run.py                    # end-to-end driver (phase1 → phase2 → phase3)
```

## Setup

```bash
pip install tree-sitter tree-sitter-language-pack pyyaml requests markdown pygments

# LLM: any OpenAI-compatible endpoint (Phase 2/3 need it; Phase 1 does not).
export OPENAI_API_KEY=sk-...                        # required (=EMPTY for a keyless local endpoint)
export OPENAI_MODEL=gpt-4o-mini                     # optional (default: gpt-4o-mini)
export OPENAI_BASE_URL=https://api.openai.com/v1    # optional; or a self-hosted vLLM / proxy
```

`markdown` + `pygments` are only needed for HTML rendering. The client lives in
`phase2/api_client.py`; the `HANDBOOK_LLM_MODEL` / `HANDBOOK_LLM_BASE_URL` /
`HANDBOOK_LLM_API_KEY` names are still honored as overrides.

## Usage

End-to-end. Describe the project once via `--project-*` so the prompts are tailored to it:

```bash
python3 run.py \
    --lang rust \
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

`--project-brief-file path.md` reads the brief from a file instead. If `--project-name`
is omitted it falls back to `--title`.

Just the call graph (no LLM, any language):

```bash
python3 run.py --lang rust --source-root /path/to/repo --work-dir work/repo --phase 1
# or directly:
python3 run_phase1.py --lang go --source-root /path/to/repo --out out/repo
```

`--phase` accepts `all | 1 | 2 | 3 | 1-2 | 2-3`. `--out-lang {zh,en}` sets the handbook
language (default `zh`). Restrict Phase 1 to specific files with `--files a.py,b.py`
(else it auto-discovers all files of the chosen language under `--source-root`).

**Output** → `work/repo/phase3/output/` (markdown handbook + JSON).

## Language support

| Language | Parser | Nodes (fn/method/sig/async/class) | Call edges | self-attr typing |
|---|---|---|---|---|
| Python | stdlib `ast` | exact | full (all `call_type`s) | from `__init__` assigns + annotations |
| Rust | tree-sitter | full | self / self-field / param / `Type::` / free / macro | from struct field types |
| TypeScript | tree-sitter | full (class methods, functions, arrows) | this / this-field / param / free / import | from class fields + ctor params |
| Go | tree-sitter | full (funcs, methods w/ receiver) | receiver / receiver-field / param / free / pkg | from struct field types |
| Starlark | tree-sitter | functions (no classes) | call name → internal/boundary | n/a |
| Shell (bash) | tree-sitter | functions (no classes) | command name → internal/boundary | n/a |
| PowerShell | tree-sitter | functions (no classes) | command name → internal/boundary | n/a |

All emit the **same `graph.json` schema**, so Phase 2/3 consume any of them unchanged.
Starlark / Shell / PowerShell use a lightweight free-function model (weak call-graph
semantics — most commands are external), so a mixed repo drops **no files**.

### Mixed-language repos: `--lang auto`

`--lang auto` discovers every supported language under the source root and merges them
into one `graph.json`. Per-language call graphs are complete; **cross-language call edges
break at the boundary** (e.g. Rust spawning a Python script) and land in
`dropped_calls.json`, same as any other unresolved call. No function is ever lost.

```bash
python3 run.py --lang auto --source-root /path/to/repo \
    --skeleton skeletons/repo.yaml --work-dir work/repo --title "Repo Handbook"
```

### Known simplifications (non-Python)

- Call resolution is best-effort static analysis (no full type inference); anything it
  can't pin to a name lands in `dropped_calls.json` as `unresolved`.
- `boundary` qualname splitting uses `.`-segmentation (tuned for Python dotted paths);
  Rust `::` boundary nodes still resolve but their module/class metadata split is
  approximate. Phase 2/3 are unaffected — they key on qualname + file + line range.

## Project context (making prompts generic)

At run time `run.py` injects three env vars (read by `project_context.py`) that every
Phase 2 / Phase 3 prompt consumes:

| env var (set by `run.py`) | CLI flag | meaning |
|---|---|---|
| `HANDBOOK_PROJECT_NAME` | `--project-name` (falls back to `--title`) | display name, e.g. "Redis" |
| `HANDBOOK_PROJECT_BRIEF` | `--project-brief` / `--project-brief-file` | 1–3 sentence description |
| `HANDBOOK_PROJECT_KIND` | `--project-kind` | noun, e.g. "web service", "compiler" |

Optional subsystem enrichment (empty by default, set directly in the env if you want it):
`HANDBOOK_SUBSYS_FILE_MAP` (JSON `{"file.py": "subsys-x"}`) and `HANDBOOK_SUBSYS_BOUNDARY_MAP`
(JSON `{"module.path": "subsys-x"}`).

## Concurrency

- **Phase 2 · Pass A** classifies functions across a thread pool already.
- **Phase 3** generates stages concurrently (`--max-stage-workers`, default 4).
  Per-function Tier 3 units stay sequential inside a stage so each can cross-reference
  already-written siblings. Set `--max-stage-workers 1` for a fully serial run.

## Adding a language

1. `pip`-install or rely on `tree-sitter-language-pack` for the grammar.
2. Add `adapters/<lang>_adapter.py` implementing `LanguageAdapter.analyze()` (return
   `ModuleAnalysis`) and optionally `statement_spans()`. Use the `TSNode` wrapper +
   `parse_tree()` from `base.py`.
3. `register("<lang>", <Adapter>, (".ext",))` at the bottom; `base._autoregister` picks
   it up.
