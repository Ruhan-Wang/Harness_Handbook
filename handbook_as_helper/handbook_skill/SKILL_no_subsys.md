---
name: terminus2-handbook
description: >-
  Structural map of the Terminus-2 harness, derived from its code. Use it when planning a
  change to terminus_2 to find EVERY code site the change must touch — it maps the code
  stage-by-stage (with a per-function description) and lists every state variable together
  with all of its read/write locations. Consult it before finalizing a plan so that
  scattered or non-obvious sites are not missed.
---

# Terminus-2 Handbook — navigation guide

This is a structural map of the Terminus-2 harness. Use it to locate where a requested
change must take effect — especially sites that are not adjacent to the obvious one.

## Reference files
They live in this handbook folder, whose absolute path is given to you in your instructions.
Read them with `read_file` using that absolute path + the relative name below.

- `references/overview.md`    — whole-system orientation: the lifecycle, the main loop, and
                                how the stages fit together. Read this first.
- `references/index.md`       — the map: every stage (id, title, what it does, the functions
                                it covers) and every state register (name + purpose).
- `references/registers.md`   — for each state variable: its purpose and EVERY place it is
                                written and read, across the whole harness.
- `references/stages/<id>.md` — one stage's prose plus a description of each function in it.
                                Open the stage file(s) the change concerns — there may be
                                more than one.

## How to use it (during planning, before you write your plan)
1. Read `references/overview.md` first to understand the whole system — the lifecycle and
   the main loop — so you know roughly where any change belongs.
2. Read `references/index.md`. Each stage entry describes what that stage does plus its
   functions; each register has its purpose. Use it to identify the stages, functions, AND
   state variables your change involves — and any it might couple to. Do NOT prematurely
   narrow to the one obvious stage: a single change often touches sites in several stages,
   and the easy-to-miss ones usually live OUTSIDE the obvious place.
3. For EVERY state variable your change touches or relies on, read `references/registers.md`,
   find that register, and note EVERY write and read site it lists. This read/write registry
   is the handbook's main asset for surfacing the scattered, non-adjacent sites a top-down
   code read would miss — use it on every change, not only the obviously state-variable
   ones. The change must stay consistent across all of those sites.
4. If the change is about a stage's behavior, open that stage's file under
   `references/stages/` to confirm and get the detail. Note the functions and sub-steps it
   names — and any assumption the described logic relies on that your change might break.
5. For each site the handbook names (a stage, a function, a register read/write), open the
   real code with `read_file` / `search_file_content` and locate the precise lines.
6. Your plan must account for every site the handbook surfaced — not only the first place
   the change seems to go.

The handbook tells you WHERE things live and how they connect. You still decide what the
change should be, and you verify every location against the real code before planning it.
