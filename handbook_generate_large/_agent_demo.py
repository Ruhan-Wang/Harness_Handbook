# -*- coding: utf-8 -*-
"""_agent_demo.py — prove render_agent layer 1 on REAL data, zero LLM.

Loads the existing Phase 2 tree, recovers the register→stages map from the
already-written register.md (so no LLM call), uses each stage's existing rollup
summary from index.md when available (else its skeleton description), and renders
the agent arm for a chosen subtree. Lets us eyeball the fixed-schema output —
including a worst-case cross-cutting util stage — before spending LLM on layer 2.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for p in ("phase3", "phase2", "shared"):
    sys.path.insert(0, str(_HERE / p))

import load_inputs as load_mod          # noqa: E402
import render_agent as ra               # noqa: E402


def recover_registers(register_md: Path, valid: set[str]) -> list[dict]:
    """Parse register.md's table back into [{id, semantics, stages}] — recovers
    the LLM-extracted registers without re-calling the model."""
    regs: list[dict] = []
    if not register_md.exists():
        return regs
    for line in register_md.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*`(reg-[a-z0-9-]+)`\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|", line)
        if not m:
            continue
        rid, sem, stages_cell = m.groups()
        sids = re.findall(r"\(([a-z0-9.-]+)\.md\)", stages_cell)
        regs.append({"id": rid, "semantics": sem,
                     "stages": [s for s in sids if s in valid]})
    return regs


def recover_summaries(index_md: Path, valid: set[str]) -> dict[str, str]:
    """Pull each stage's first overview paragraph out of the human index.md so
    the 职责 line is realistic (else render falls back to skeleton description)."""
    out: dict[str, str] = {}
    if not index_md.exists():
        return out
    cur = None
    buf: list[str] = []
    for line in index_md.read_text(encoding="utf-8").splitlines():
        m = re.match(r"#+\s+\[.+?\]\((stage-[0-9.]+)\.md\)", line)
        if m:
            if cur and buf:
                out[cur] = " ".join(buf).strip()
            cur = m.group(1)
            buf = []
        elif cur and line.strip() and not line.startswith("#"):
            if not buf:                              # first paragraph only
                buf.append(line.strip())
    if cur and buf:
        out[cur] = " ".join(buf).strip()
    return {k: v for k, v in out.items() if k in valid}


def main() -> int:
    work = _HERE / "work" / "codex"
    if not (work / "phase2").exists():
        # fall back to the large-handbook checkout layout
        work = Path("/Users/tencentintern/Desktop/Harness_Handbook/"
                    "handbook_generate_large/work/codex")
    tree = load_mod.load_all(work / "phase2")
    valid = set(tree.order)
    registers = recover_registers(work / "handbook" / "register.md", valid)
    summaries = recover_summaries(work / "handbook" / "index.md", valid)
    print(f"recovered {len(registers)} registers, {len(summaries)} summaries")

    out = Path("/tmp/agent_demo")
    roots = sys.argv[1:] or ["stage-14.2", "stage-22"]
    stats = ra.render_agent_site(tree, summaries, registers, out,
                                 lang="zh", roots=roots)
    print("wrote:", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
