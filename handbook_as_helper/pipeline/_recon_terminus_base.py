#!/usr/bin/env python3
"""Deterministically reconstruct the terminus member-level phase-2 base
(skeleton.yaml + mapping.yaml) from the rendered handbook skill's index.md.

The index encodes the exact stage tree, per-stage member functions as
`qualname (file:start-end)`, and the state registers — i.e. everything the
member resync needs in PHASE2_FINAL. We rebuild the canonical YAML from it with
ZERO LLM, computing each member's sha1 the same way apply._sha1_of_range does,
and validating every qualname against the AST (lang_layer.spans) so the resync's
span-based verdict will find them.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lang_layer as _L  # noqa: E402

HELPER_ROOT = HERE.parent
REPO_ROOT = HELPER_ROOT.parent
INDEX = HELPER_ROOT / "handbook_skills/handbook_skill_terminus/references/index.md"
PRISTINE = REPO_ROOT / "harbor/src/harbor/agents/terminus_2"
OUT = REPO_ROOT / "handbook_generate_terminus/work/terminus/phase2/iterations/final"

STAGE_RE = re.compile(r"^- \*\*(?P<id>[^*]+)\*\*\s+—\s+(?P<title>.+?)\s*$")
MEMBER_RE = re.compile(r"`(?P<qn>[^`]+)`\s*\((?P<file>[^():]+):(?P<a>\d+)-(?P<b>\d+)")
REG_RE = re.compile(r"^- \*\*`(?P<id>[^`]+)`\*\*\s+—\s+(?P<sem>.+?)\s*$")


def _sha1_of_range(file_path: Path, start: int, end: int) -> str:
    lines = file_path.read_text(encoding="utf-8").splitlines()
    return hashlib.sha1("\n".join(lines[start - 1:end]).encode("utf-8")).hexdigest()


def parse_index(text: str):
    """-> (stages: list[{id,title,description,members:[(qn,file,a,b)]}], registers: list[(id,sem)])."""
    lines = text.splitlines()
    stages: list[dict] = []
    registers: list[tuple[str, str]] = []
    cur: dict | None = None
    in_regs = False
    for ln in lines:
        if ln.startswith("## State registers"):
            in_regs = True
            cur = None
            continue
        if in_regs:
            m = REG_RE.match(ln)
            if m:
                registers.append((m.group("id"), m.group("sem")))
            continue
        m = STAGE_RE.match(ln)
        if m:
            cur = {"id": m.group("id").strip(), "title": m.group("title").strip(),
                   "description": "", "members": []}
            stages.append(cur)
            continue
        if cur is None:
            continue
        body = ln.strip()
        if body.startswith("- functions:"):
            for mm in MEMBER_RE.finditer(body):
                cur["members"].append((mm.group("qn"), mm.group("file"),
                                       int(mm.group("a")), int(mm.group("b"))))
        elif body.startswith("- ") and not cur["description"]:
            cur["description"] = body[2:].strip()
    return stages, registers


def main() -> int:
    text = INDEX.read_text(encoding="utf-8")
    stages, registers = parse_index(text)

    # AST spans per file (for function-vs-region typing + qualname validation).
    span_cache: dict[str, dict] = {}

    def spans_for(fname: str) -> dict:
        if fname not in span_cache:
            span_cache[fname] = _L.spans(PRISTINE / fname, "python")
        return span_cache[fname]

    # ── skeleton.yaml ──
    ids = [s["id"] for s in stages]

    def parent_of(sid: str) -> str | None:
        m = re.match(r"^(.*?)(\.\d+)$", sid)
        return m.group(1) if (m and m.group(1) in ids) else None

    sk_stages = []
    for s in stages:
        pid = parent_of(s["id"])
        sk_stages.append({
            "id": s["id"], "title": s["title"],
            "description": s["description"] or "(no description)",
            "parent": pid,
            "children": [c for c in ids if parent_of(c) == s["id"]],
        })
    skeleton = {
        "metadata": {"version": 1, "generated_from": "index.md (deterministic recon)"},
        "stages": sk_stages,
        "state_registers": [{"id": rid, "semantics": sem} for rid, sem in registers],
        "subsystems": [],
    }

    # ── mapping.yaml ──
    mapping = {"metadata": {"reconstructed_from": "handbook_skill_terminus/index.md",
                            "method": "deterministic (zero-LLM)"},
               "stages": {}, "unmapped_functions": []}
    n_members = 0
    warns: list[str] = []
    for s in stages:
        if not s["members"]:
            continue
        st = mapping["stages"].setdefault(
            s["id"], {"members": [], "uses_crosscuts": [], "subsystem_refs": []})
        for qn, fname, a, b in s["members"]:
            sp = spans_for(fname)
            full = sp.get(qn)
            if full is None:
                warns.append(f"{s['id']}: qualname not found in AST: {qn} ({fname})")
                mtype = "function"
            else:
                mtype = "function" if (full[0], full[1]) == (a, b) else "region"
            st["members"].append({
                "qualname": qn, "type": mtype, "file": fname,
                "line_range": [a, b],
                "sha1": _sha1_of_range(PRISTINE / fname, a, b),
                "purpose": "",
            })
            n_members += 1

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "skeleton.yaml").write_text(
        yaml.safe_dump(skeleton, sort_keys=False, allow_unicode=True, width=10000),
        encoding="utf-8")
    (OUT / "mapping.yaml").write_text(
        yaml.safe_dump(mapping, sort_keys=False, allow_unicode=True, width=10000),
        encoding="utf-8")

    # unique qualnames for a sanity read
    quals = {qn for s in stages for (qn, *_r) in s["members"]}
    print(f"stages parsed: {len(stages)}  (with members: {len(mapping['stages'])})")
    print(f"members: {n_members}  unique qualnames: {len(quals)}  registers: {len(registers)}")
    print(f"wrote: {OUT/'skeleton.yaml'}")
    print(f"       {OUT/'mapping.yaml'}")
    if warns:
        print(f"WARNINGS ({len(warns)}):")
        for w in warns:
            print("  -", w)
    else:
        print("all qualnames matched AST spans ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
