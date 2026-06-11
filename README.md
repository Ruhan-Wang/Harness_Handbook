# Harness_Handbook

Automated pipeline for generating structured **code handbooks** for the Terminus-2
harness, plus an evaluation framework that tests whether these handbooks help AI
coding agents — and a desktop/web app for generating and exploring handbooks
interactively.

## Repository layout

- **`handbook_generate/`** — the three-phase generation pipeline:
  - `phase1/` — AST-based call-graph extraction.
  - `phase2/` — LLM-driven actor–critic loop that maps functions to lifecycle stages.
  - `phase3/` — LLM-driven document assembly and rendering.
- **`handbook_as_helper/`** — evaluation harness measuring handbook impact on agents.
- **`handbook-studio/`** — a full-stack app (React + Vite frontend, Node.js backend)
  that drives the pipeline through your coding CLI / an LLM gateway and provides
  rich handbook visualization and chat. See `handbook-studio/README.md`.
- `handbook_en.html` / `handbook_ch.html` — example rendered handbooks.

## Credentials

This project talks to an LLM endpoint. **No secrets are stored in the repo.**
Provide credentials via environment variables, e.g.:

```bash
export HANDBOOK_LLM_USER=...      # internal endpoint user / secret id
export HANDBOOK_LLM_KEY=...       # internal endpoint key
# optional overrides
export HANDBOOK_LLM_HOST=...      # e.g. local gateway host
export HANDBOOK_LLM_PORT=...
```

`handbook_as_helper/opus_proxy.py` reads `OPUS_APP_ID` / `OPUS_APP_KEY`, and
`handbook_as_helper/grade_ab.py` reads `GRADER_TRPC_USER` / `GRADER_TRPC_KEY`.

## handbook-studio quick start

```bash
cd handbook-studio
npm install
npm run dev
```
