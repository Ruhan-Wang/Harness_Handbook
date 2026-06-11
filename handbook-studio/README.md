# Handbook Studio

Generate and explore **code handbooks** for any repo, powered by **your coding CLI** — Claude Code,
Cursor, Codex, or Gemini (the same set dr-claw drives). No API keys: you use whichever CLI you're
already logged into.

It wraps the 3-phase handbook generator in `../Harness_Handbook/handbook_generate` with a UI, and
routes every LLM call through a local gateway that drives the active CLI in single-shot mode.
Generated handbooks are written into each repo's `.handbook/` folder and rendered as an interactive
viewer.

## What it does

1. **Connect & use a CLI** — detects installed coding CLIs (Claude Code, Cursor, Codex, Gemini) in
   the left bar, lets you pick the active provider + model and log in. All generation LLM traffic
   goes through it.
2. **Open repos, generate with buttons** — register a repo, then run the pipeline in stages:
   - Map call graph (static AST, no LLM)
   - Draft skeleton (the CLI proposes lifecycle stages; you review/edit in-app)
   - Classify stages (actor-critic mapping loop)
   - Write docs (Tier 1/2/3 prose + `references/` + `SKILL.md`)
3. **Outputs land in the repo** — everything is written under `<repo>/.handbook/`.
4. **Visualize & interact** — interactive call graph (colored by stage), stage navigator,
   state-register read/write explorer with source linking, function cards, a coverage/health
   panel, and an "ask the handbook" chat.

## Architecture

```
React + Tailwind UI  ──REST/WebSocket──▶  Express server
                                              │  spawns python  ─▶ handbook_generate (phase1/2/3)
                                              │                         │ HTTP localhost (data_eval shape)
                                              └─ LLM gateway  ◀─────────┘
                                                   │ spawn active CLI in single-shot mode
                                                   ▼
                                  Claude Code / Cursor / Codex / Gemini CLI
```

The Python pipeline's LLM client (`api_client.Api`) is repointed at the gateway via env vars
(`HANDBOOK_LLM_HOST` / `HANDBOOK_LLM_PORT`), so no internal endpoint is needed.

## Prerequisites

- Node 18+
- Python 3.10+ with the pipeline deps: `pip install -r python/requirements.txt`
- One of these AI providers:
  - A coding CLI installed + logged in. Any of:
    - Claude Code (`claude`) — override with `CLAUDE_CLI_PATH`
    - Cursor (`cursor-agent` / `agent`) — override with `CURSOR_CLI_PATH`
    - Codex (`codex`) — override with `CODEX_CLI_PATH`
    - Gemini (`gemini`) — override with `GEMINI_CLI_PATH`
  - **Internal endpoint** — an HMAC-signed `data_eval` HTTP endpoint (no CLI, no
    subscription rate limits). Configure it in the left bar (host / secretId /
    secretKey / model marker) or via `HS_INTERNAL_*` env vars. This is the same
    protocol as the original pipeline's `trpc-gpt-eval` endpoint.
  AI is only needed for the skeleton/classify/docs/chat steps; Phase 1 needs none.

## Run (dev)

```bash
npm install
pip install -r python/requirements.txt
npm run dev        # starts the API/gateway server (4319) and the Vite UI (5319)
```

Open http://localhost:5319, "Open" a repo, and click **Generate full handbook**.

## Run (single process)

```bash
npm run build      # build the UI
npm start          # serves UI + API + gateway on http://localhost:4319
```

## Configuration (env)

| Var | Purpose | Default |
|-----|---------|---------|
| `HS_SERVER_PORT` | API + gateway port | `4319` |
| `HS_LLM_CONCURRENCY` | max concurrent CLI/HTTP calls | `4` |
| `CLAUDE_CLI_PATH` / `CURSOR_CLI_PATH` / `CODEX_CLI_PATH` / `GEMINI_CLI_PATH` | override a CLI binary | auto-detected |
| `HS_INTERNAL_HOST` / `HS_INTERNAL_PORT` | internal data_eval endpoint | — / `8080` |
| `HS_INTERNAL_USER` / `HS_INTERNAL_KEY` | HMAC secretId / secretKey | — |
| `HS_INTERNAL_MODEL` | model marker | `api_azure_openai_gpt-5.4-2026-03-05` |
| `HANDBOOK_GENERATE_DIR` | path to `handbook_generate` | `../Harness_Handbook/handbook_generate` |
| `HANDBOOK_PYTHON` | python interpreter | `python3` |

## Notes

- Phase 1 runs with no LLM, so the call graph + state-register explorer work even before login.
- The active provider + model are chosen in the left bar and persisted to
  `~/.handbook-studio/settings.json`; pipeline runs and chat both use them.
- The skeleton step falls back to a per-file heuristic if no CLI is available, so the pipeline
  never hard-blocks.
- Phase 3 is generated in English (`--lang en`).
