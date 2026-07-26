# Generated handbook format

Use this contract for every handbook produced by the builder.

## Design rules

- Make the output a standalone Codex skill with no dependency on the builder.
- Use repository-relative POSIX paths even on Windows.
- Use exact symbol names and concise factual summaries.
- Route with the handbook, but verify real source before planning or editing.
- Do not copy complete functions, files, prompts, credentials, or environment values.
- Keep each fact in the narrowest useful page and link upward instead of duplicating it.

## `SKILL.md`

Use only `name` and `description` in YAML frontmatter. Name the skill
`<repository-slug>-handbook`. Put both positive and negative trigger boundaries in
the description because Codex uses it to decide whether to load the skill:

```yaml
---
name: <repository-slug>-handbook
description: Navigate the <Project> repository by behavior and source location. Use when planning, implementing, debugging, testing, explaining, or reviewing <Project> work that is unfamiliar, spans multiple files, or may affect cross-cutting state. Do not use for tasks unrelated to <Project>, requests without access to its source, or isolated edits where the exact file is already known and no cross-cutting impact is plausible.
---
```

The body must tell the agent to:

1. Read `references/overview.md` for the system shape.
2. Route through `references/index.md`.
3. Read relevant `references/stages/<stage-id>.md` pages.
4. Check `references/registers.md` for cross-cutting state.
5. Read actual source at the cited paths before proposing or making changes.
6. Treat `references/coverage.json` hashes as freshness signals, not source truth.
7. Respect the requested task boundary: read-only for explanation, diagnosis, plans,
   and reviews; edit and test only when a change is requested.

## `agents/openai.yaml`

Quote all values:

```yaml
interface:
  display_name: "<Project> Handbook"
  short_description: "Navigate the <Project> codebase by behavior"
  default_prompt: "Use $<repository-slug>-handbook to locate the source involved in this change."
```

Keep `short_description` between 25 and 64 characters.

## `references/overview.md`

Include:

- Purpose and system boundary.
- Component map.
- End-to-end execution or build lifecycle.
- Entry points and external interfaces.
- Major extension points.
- Build, test, and verification commands when established by repository evidence.
- A freshness note that points to `coverage.json`.

Use a compact flow diagram only when it clarifies a sequence with at least three
stages.

## `references/index.md`

Start with a routing table:

| Stage | Responsibility | Primary inputs | Primary outputs | Page |
|---|---|---|---|---|

Follow it with:

- Change-routing hints that map common task types to stages.
- Cross-stage relationships.
- A link to `registers.md`.

Every page in `references/stages/` must be linked exactly once from the routing table.

## `references/registers.md`

Document cross-cutting state, configuration, or durable data:

| Register | Owner | Initialized | Written | Read | Invariants |
|---|---|---|---|---|---|

Use exact `path:symbol` locations. Include error/cancellation state when it affects
multiple stages. If the repository has no meaningful registers, say so and list the
configuration sources that shape execution.

## Stage pages

Name pages with stable lowercase IDs such as `stage-1-entry.md` or
`stage-storage.md`. Each page must contain:

1. Purpose and boundaries.
2. Files and key symbols:

   | File | Key symbols | Role |
   |---|---|---|

3. Control and data flow.
4. Inputs, outputs, and external interfaces.
5. State mutations and invariants.
6. Failure, retry, cancellation, and cleanup behavior.
7. Tests and verification.
8. Change-routing notes.

Mention shared files on other stage pages when useful, but give each eligible file
only one primary stage in `coverage.json`.

## `references/stage-rules.json`

Use this source-controlled assignment definition:

```json
{
  "schema_version": 1,
  "stages": [
    {
      "id": "stage-entry",
      "include": ["src/cli/**", "src/main.py"]
    },
    {
      "id": "stage-runtime",
      "include": ["src/runtime/**"],
      "exclude": ["src/runtime/adapters/**"]
    },
    {
      "id": "stage-adapters",
      "include": ["src/runtime/adapters/**"]
    }
  ],
  "overrides": {
    "tests/test_cli.py": "stage-entry"
  }
}
```

Rules must be non-overlapping after exclusions. Exact-path overrides take precedence.
Every stage ID must have a matching `references/stages/<id>.md` page.

## `references/coverage.json`

Do not hand-edit this file. Generate it with `scripts/compile_coverage.py`. It records
the inventory fingerprint plus each eligible file's primary stage, language, line
count, size, and content hash. The validator uses it to find omissions and stale pages.
