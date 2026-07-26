---
name: build-codebase-handbook
description: Generate, refresh, validate, or use a planner-ready handbook for a software repository. Use when Codex is asked to map or document a codebase, create a navigation skill, replace an API-backed handbook generator, update or validate an existing handbook, or use a handbook to locate, plan, implement, debug, test, or review a repository task—especially unfamiliar or cross-cutting work. Do not use when no handbook exists and the user only wants an ordinary code change without requesting one, for non-code research or writing, for API/product documentation lookup, or for an isolated edit whose exact file is already known and has no plausible repository-wide impact. Performs reasoning in the active Codex session and never requires an external LLM API key.
---

# Build Codebase Handbook

Create or use a compact source-location index that helps Codex find every place a
change touches. Use the active Codex session for analysis; do not call an LLM
endpoint, API client, or API-backed generator.

## Choose the workflow

Resolve this skill's directory from the selected `SKILL.md` and call it
`HANDBOOK_BUILDER_ROOT`.

Choose one mode:

- **Task mode:** Use an existing generated handbook to route a concrete planning,
  implementation, debugging, testing, explanation, or review task.
- **Build mode:** Generate, refresh, or validate a handbook skill.
- **Combined mode:** Build or refresh the handbook first, then use it for the task.

Do not generate a handbook merely because a normal coding task mentions a repository.
Task mode requires an existing handbook or an explicit request to create/use one.

## Use an existing handbook for a task

1. Resolve the handbook named by the user or already selected by Codex. Otherwise
   inspect `<source>/.agents/skills/*-handbook/SKILL.md`, excluding this builder.
   If several match, choose by repository name and described scope; ask only when
   ambiguity would materially change the task.
2. Read the generated handbook's `SKILL.md` completely. Read its
   `references/overview.md`, route through `references/index.md`, then load only the
   relevant stage pages and `references/registers.md`.
3. Inspect `references/coverage.json` for freshness of the routed files. When useful,
   run this builder's validator. Treat validation failures as drift signals, not as
   permission to skip the user's task.
4. Read the real source at every cited path and symbol. Search for callers, tests,
   configuration, and state readers/writers that may have changed since generation.
5. Follow the user's requested action boundary:
   - For explanation, diagnosis, planning, or review, remain read-only.
   - For a requested change, implement it and verify it in proportion to risk.
   - For broad changes, use the handbook index and registers as a coverage checklist.
6. If the task changes covered source, refresh the handbook only when the user asks
   to keep it synchronized or repository instructions require it. Otherwise report
   that its coverage hashes are now stale.

Never treat handbook prose as authoritative code text.

## Build or refresh a handbook

### Set the scope

Use:

- Source root: the user's requested repository, otherwise the current repository.
- Output: the user's requested path, otherwise
  `<source>/.agents/skills/<repository-slug>-handbook`.
- Work directory: a task-specific temporary directory outside the output skill.
- Language: the user's requested language, otherwise English.

Run the bundled utilities with Python 3.10 or newer. They use only the Python
standard library, so this skill intentionally has no `requirements.txt`. If a future
version adds a third-party import, add and package a pinned `requirements.txt`.

Make the generated folder independently shareable. Do not import code or templates
from the source repository or this skill. Do not add a README, build log, scratch
notes, or source snapshot.

If the output already exists, update it in place. Preserve useful hand-written
content, inspect changes before overwriting, and never delete the whole folder as a
shortcut.

### 1. Inventory before interpreting

Read applicable `AGENTS.md` files and repository instructions first.

Run:

```bash
python3 "$HANDBOOK_BUILDER_ROOT/scripts/inventory.py" \
  --source-root "<source>" \
  --output "<work>/inventory.json"
```

Review the summary and skipped-file reasons. Add `--exclude PATTERN` for generated
trees or project-specific noise. Never weaken the default secret and private-key
exclusions.

Treat the inventory as the coverage contract. Do not silently omit an eligible file
because it looks unimportant.

### 2. Derive the behavioral map

Inspect the actual source with repository search and targeted reads. Use the
inventory to work in bounded batches.

Identify:

1. Entry points and external inputs.
2. Ordered runtime or build stages.
3. Core domain logic and orchestration.
4. State, configuration, persistence, queues, caches, and other registers.
5. Boundaries such as APIs, CLIs, databases, providers, and subprocesses.
6. Error, retry, cancellation, cleanup, and shutdown paths.
7. Tests that establish contracts or exercise cross-stage behavior.

Group files by behavior and lifecycle, not merely by directory. Prefer 4–12 stages
for a typical repository. A file must have one primary stage even when referenced
from several pages.

For large repositories, analyze independent inventory slices concurrently with
subagents when that capability is available. Give each agent only its file slice
and request factual notes with exact paths and symbols. Reconcile their notes
against the source yourself. Otherwise process the slices sequentially.

### 3. Write the generated skill

Read [references/handbook-format.md](references/handbook-format.md) completely before
creating or updating output files. Follow its required layout and schemas.

Create:

```text
<output>/
├── SKILL.md
├── agents/openai.yaml
└── references/
    ├── overview.md
    ├── index.md
    ├── registers.md
    ├── stage-rules.json
    ├── coverage.json
    └── stages/<stage-id>.md
```

Write exact repository-relative paths and symbol names. Summarize behavior rather
than copying source bodies. Never include credentials, environment values, private
keys, or secret file contents.

The generated `SKILL.md` must tell future agents to use the handbook for routing,
then inspect real source before planning or editing. The handbook is not ground
truth for verbatim code.

### 4. Compile complete coverage

Express the primary file-to-stage assignment in
`references/stage-rules.json`. Prefer narrow, non-overlapping glob rules; use exact
path overrides for exceptions.

Run:

```bash
python3 "$HANDBOOK_BUILDER_ROOT/scripts/compile_coverage.py" \
  --inventory "<work>/inventory.json" \
  --rules "<output>/references/stage-rules.json" \
  --output "<output>/references/coverage.json"
```

Fix unmatched files, overlapping rules, unknown overrides, and invalid stage IDs.
Do not use a catch-all rule to conceal an incomplete architecture analysis.

### 5. Validate and reconcile

Run:

```bash
python3 "$HANDBOOK_BUILDER_ROOT/scripts/validate_handbook.py" \
  --source-root "<source>" \
  --skill-dir "<output>"
```

Resolve every error. Review warnings and fix any that affect routing quality. Then:

- Check that every stage page links from `index.md`.
- Spot-check paths, symbols, lifecycle order, and register read/write claims against
  current source.
- Review the output diff for accidental source excerpts or sensitive information.
- Report the output path, eligible-file coverage, validation result, and any
  intentionally skipped categories.

### Refresh an existing handbook

Re-run the inventory and validator first. Use stale hashes and new/deleted paths to
identify affected stages. Re-read changed source, update those stage pages, then
roll changes upward into `registers.md`, `index.md`, and `overview.md`. Recompile
coverage and validate again.

Do not rewrite unaffected prose solely for stylistic consistency.

## Package this builder skill for sharing

The builder carries its own Apache-2.0 `LICENSE`, so a ZIP remains licensed when
detached from this repository. The repository has no `NOTICE` file to propagate.

Create a deterministic archive:

```bash
python3 "$HANDBOOK_BUILDER_ROOT/scripts/package_skill.py" \
  --output "<destination>/build-codebase-handbook.zip"
```

The archive contains one top-level `build-codebase-handbook/` directory and excludes
caches, temporary files, existing archives, and VCS metadata. It includes
`requirements.txt` automatically if one exists.

Do not assume the same Apache license applies to handbooks generated from someone
else's repository. Before distributing a generated handbook, use the license and
notices authorized by that repository's owner.
