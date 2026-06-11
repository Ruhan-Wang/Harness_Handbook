# Phase 2 — Critic-Actor Iteration Pipeline

Iteratively populates `mapping.yaml` and (optionally) revises `skeleton.yaml`
through four LLM passes plus a stage-member ordering step, until the state
converges (or `--max-iters` is hit). All LLM output goes through Actor → Critic
review before it touches the mapping.

## Pipeline at a glance

```
                ┌─────────────────────────────────────────────┐
                │  iterate_phase2.run()  — one iteration:     │
                │                                             │
  invalidated ──┤  Pass A   per-function classify             │── populates mapping
                │  Pass B   global stage-reassignment audit   │── moves entries
                │  Pass C   skeleton doctor (add/rm/mg/sp)    │── mutates skeleton
                │  Pass D   region boundary revision          │── refines regions
                │                                             │
                │  mechanical post-pass:                      │
                │    dedup_members                            │
                │    rederive uses_crosscuts/subsystem_refs   │
                │    populate_unmapped                        │
                │                                             │
                │  Step 3.5  stage member ordering (cached)   │── per-iter
                │                                             │
                │  snapshot → iterations/iter_N/              │
                │  convergence check → loop or _finalize      │
                └─────────────────────────────────────────────┘
```

The driver is `iterate_phase2.py`. The four `pass_*.py` modules each implement
one pass; `critic.py` provides the Actor/Critic framework; `apply.py` is the
mechanical layer that mutates `mapping_doc`/`skeleton_doc`.

## Files

| File | Purpose |
|------|---------|
| `iterate_phase2.py` | Main driver: loops the four passes, runs Step 3.5 per iter, writes snapshots, checks convergence |
| `pass_a_classify.py` | Pass A — per-function classification with caller/callee context; Engineer Critic; ≤1 revise round; parallel workers, serial apply |
| `pass_b_reassign.py` | Pass B — per-stage audit that proposes ≤3 reassignments; Architect + Engineer must both APPROVE; cached by `(stage_id, member purposes)` fingerprint |
| `pass_c_skeleton_doctor.py` | Pass C — proposes ≤3 skeleton changes (`add_stage` / `remove_stage` / `merge_stages` / `split_stage`); Engineer + Architect + Reader must all APPROVE |
| `pass_d_region_revision.py` | Pass D — for functions with ≥2 regions, proposes ≤3 `merge` / `split` / `reassign_stage` actions; Engineer Critic; cached by `(qualname, source_sha, regions)` |
| `critic.py` | Actor-Critic orchestration: `actor_critic_loop` (single critic) and `actor_multi_critic_loop` (all-approve); role prompts; verdict parsing |
| `apply.py` | Mechanical mutations: `apply_classification`, `apply_reassignment`, `apply_skeleton_*`, `apply_region_revision`; derived-field recompute; `dedup_members`; `state_hash` for convergence |
| `order_stage_members.py` | Step 3.5 — per-stage member ordering (linear / branched / unordered) with Editor Critic; cached |
| `skeleton_yaml.py` | Load/save skeleton.yaml, convert skeleton.md ↔ skeleton.yaml, helpers |
| `parse_skeleton.py` | Standalone: extract stage IDs + descriptions from `skeleton.md` |
| `ast_snap.py` | Snap LLM-given `line_range` to legal AST statement boundaries (used by `apply_classification`) |
| `api_client.py` | Wraps the trpc-gpt-eval API (HMAC auth, retries, JSON extraction) |

> Legacy modules from the old four-step mechanical pipeline (`llm_analyze.py`,
> `validate.py`, `build_mapping.py`, `run_phase2.py`) are kept on disk because
> the new code reuses a handful of helpers from them (e.g. `function_sha1`,
> `render_source_with_line_numbers`). They are NOT part of the current
> orchestration.

## Usage

```bash
# From the repo root (Harness_Translation/):

# Full run with defaults (10 iterations max, all four passes + ordering):
python3 handbook/phase2/tools/iterate_phase2.py

# Smoke test on 3 functions:
python3 handbook/phase2/tools/iterate_phase2.py --limit 3 --max-iters 2

# Skip individual passes for debugging:
python3 handbook/phase2/tools/iterate_phase2.py --no-pass-b
python3 handbook/phase2/tools/iterate_phase2.py --no-pass-c
python3 handbook/phase2/tools/iterate_phase2.py --no-pass-d
python3 handbook/phase2/tools/iterate_phase2.py --no-ordering

# Custom skeleton / source root / output:
python3 handbook/phase2/tools/iterate_phase2.py \
    --skeleton-yaml handbook/phase2/skeleton.yaml \
    --graph handbook/phase1/graph.json \
    --source-root harbor/src/harbor/agents/terminus_2 \
    --mapping handbook/phase2/mapping.yaml \
    --iterations-dir handbook/phase2/iterations
```

Standalone re-runs of individual passes (operate on an existing
`mapping.yaml`):

```bash
# Pass B alone (re-audit reassignments, --force to bypass cache):
python3 handbook/phase2/tools/pass_b_reassign.py [--force]

# Pass D alone (region revision):
python3 handbook/phase2/tools/pass_d_region_revision.py [--force]

# Step 3.5 alone (member ordering):
python3 handbook/phase2/tools/order_stage_members.py [--force]
```

## How each pass decides what to do

Per pass, the Actor sees a tailored prompt; one or more Critics review the
proposal; if approved (all critics for B/C/D-multi, the single critic
otherwise), `apply.py` writes the change. Critic strictness scales with blast
radius:

| Pass | Scope | Critics required | Revise rounds | Cap per call |
|------|-------|------------------|---------------|--------------|
| A | one function | 1 (engineer) | 1 | — |
| B | one stage | 2 (architect + engineer), all APPROVE | 1 | ≤3 moves |
| C | whole skeleton | 3 (engineer + architect + reader), all APPROVE | 1 | ≤3 changes |
| D | one multi-region function | 1 (engineer) | 1 | ≤3 actions |
| 3.5 | one stage's order | 1 (editor) | 1 | — |

A vacuous `REVISE` (decision = REVISE with empty `concerns`) is normalized to
APPROVE — the Actor has nothing to revise against, so a second round just
burns tokens producing the same proposal.

## Invalidation queue (drives convergence)

`invalidated: list[str]` is both the per-iter Pass A worklist and one of the
two convergence criteria:

- Iter 0 seeds it with every internal non-synthetic qualname from `graph.json`.
- Pass A consumes the list; any qualname Pass A didn't process (worker crash,
  rate-limit-cut batch, etc.) is carried over rather than silently dropped.
- Pass B pushes every moved qualname back onto it (next iter re-classifies
  with new context).
- Pass C pushes every affected qualname (members of merged/removed/split
  stages, members named in `add_stage.move_members`).
- Pass D does NOT push — its region refinements are authoritative for the
  iter; if a later iter's Pass C disturbs the surrounding stage, Pass A
  re-classification will clobber the regions and Pass D simply re-revises.
- Phantom qualnames (in `invalidated` but not in graph) are dropped at the
  top of each iter — otherwise they'd sit there forever, silently skipped by
  Pass A, blocking convergence.

## Convergence

```python
current_hash = apply.state_hash(skeleton_doc, mapping_doc)
if prev_state_hash == current_hash and not invalidated:
    converged
```

`state_hash` is order-insensitive within each stage's member list (it sorts by
`(qualname, type, line_range)`) — so Step 3.5's reordering does not affect
termination. The hash captures both LLM-driven changes AND the mechanical
post-pass mutations, so a non-idempotent mechanical step is still detected.

## Caching

Per-pass disk cache, fingerprint-keyed so that "input unchanged" → "skip
LLM":

```
handbook/phase2/cache/
├── pass_b/<stage>.json         # key: (stage_id, sorted (qualname, purpose) tuples)
├── pass_d/<qualname>.json      # key: (qualname, source_sha1, sorted regions)
├── stage_orders/<stage>.json   # key: (stage_id, sorted member identity)
└── llm_outputs/                # legacy from old llm_analyze.py (unused)
```

Pass A has no per-call cache: by construction, its inputs (caller/callee
context, mapping overview) change every time something else moves, so a fixed
key wouldn't be reusable. The invalidation queue is the equivalent of a cache
miss signal for Pass A.

## Outputs

```
handbook/phase2/
├── skeleton.md                          (input; user-authored — never written by the loop)
├── skeleton.yaml                        (input; user-authored — never written by the loop)
├── mapping.yaml                         (top-level: latest iter's ordered version, then overwritten by final/)
├── cache/                               (see above)
└── iterations/
    ├── iter_0/
    │   ├── skeleton.yaml                (skeleton state at end of iter 0; may differ from input if Pass C mutated)
    │   ├── mapping.yaml                 (ordered, sha1-stamped)
    │   ├── changes.md                   (per-pass summary lines: accepted / moved / skeleton actions / region actions)
    │   └── invalidated.txt              (qualnames carried into iter 1)
    ├── iter_1/
    ├── …
    └── final/                           (snapshot at convergence or MAX iter)
```

Note: `iterations/` is wiped clean at the start of every run (only entries
named `iter_*` or `final` are removed — other files in the directory are
left alone). The user-authored `skeleton.yaml` / `skeleton.md` at the
phase2 root are NEVER overwritten by the loop — skeleton mutations from
Pass C live only in the iter snapshots, for the user to review and merge
back manually.

## Schema of `mapping.yaml`

```yaml
metadata:
  phase2_iteration_run: true
  # (other fields added by tooling as needed)

stages:
  stage-4.2:
    members:
      - qualname: terminus_2.Terminus2._query_llm
        type: function | region
        file: terminus_2.py
        line_range: [994, 1176]
        sha1: <hash of the line_range content>
        purpose: "5-aspect description (60–150 words for function, 30–80 for region)"
        # For regions only:
        # original_llm_range: [994, 1176]
        # snap_status: ok | snapped | needs_review | no_range
        # snap_distance: <int>
        # snap_note: "..."
        # first_line / last_line: original LLM-claimed boundary text
        # narrative_section: "<branch / group label>"  # set by Step 3.5 when structure ≠ linear
    uses_crosscuts: [crosscut-X1, crosscut-X3]   # derived from call edges
    subsystem_refs: [subsys-llm]                 # derived from call edges
    narrative_structure: linear | branched | unordered  # set by Step 3.5
    # ... all stages from skeleton.yaml

unmapped_functions:
  - qualname: terminus_2.Terminus2.name
    file: terminus_2.py
    reason: api_surface | dead | synthetic_dataclass | missing_llm_output
    purpose: "..."   # preserved when Pass A returned a purpose for an api_surface function
```

## Notes on the LLM API

- Endpoint: `trpc-gpt-eval.production.polaris:8080/api/v1/data_eval` (from
  `test_api.py`).
- Auth: HMAC-SHA1.
- Every prompt asks the model to emit one fenced ```json block; the client
  extracts and parses it.
- Pass A uses a thread pool (default 6 workers): workers run only the LLM
  exchange (Actor → Critic → optional revise), then return; the main thread
  applies accepted proposals serially via `apply.apply_classification` to
  avoid write races on `mapping_doc`.
- The LLM occasionally echoes a wrong `qualname` for Pass A (copy-error
  across functions in the same class). `pass_a_classify.py` overrides the
  echoed value with the qualname we sent in — without this, the wrong
  function's mapping would silently get mutated.
