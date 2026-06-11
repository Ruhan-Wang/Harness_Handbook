#!/usr/bin/env python3
"""grade_ab.py — LLM grader comparing the baseline vs handbook arms of roundtrip_eval.

For each golden query it grades the two arms' outputs (`runs/<arm>/<Qid>/plan.md` +
`agent.diff`) against the answer key (`terminus2_roundtrip_golden.yaml` +
`terminus2_golden_solutions.md`) in two ways:

  1. ABSOLUTE  — each arm is scored on its own: per-anchor / per-discriminator /
                 per-trap hit, recall, discriminator-rate, precision, correctness.
                 (`grade_absolute`, prompt: prompts/grader_absolute.md)
  2. HEAD-TO-HEAD — the two arms' (plan+diff) are shown side by side in a RANDOMIZED
                 A/B order and the judge picks the better one per dimension + overall.
                 (`grade_h2h`, prompt: prompts/grader_h2h.md)

The judge model is a pluggable backend (GRADER_BACKEND): `openai` (default — an
OpenAI-compatible /chat/completions endpoint, reusing the same local vLLM the agent
uses), `anthropic` (Claude API — optional, wired for later), or `mock` (offline,
returns deterministic stub JSON to exercise the pipeline without a model).

The aggregate metrics (macro recall, discriminator-rate, precision, H2H win counts)
are computed in Python from the judge's per-anchor booleans — the model decides each
hit, we do the arithmetic.

Usage:
    python grade_ab.py                      # grade every Q both arms have outputs for
    python grade_ab.py --cases Q1,Q12
    python grade_ab.py --dry-run --cases Q1 # print the prompts, don't call the model
    GRADER_BACKEND=mock python grade_ab.py  # offline pipeline check
    GRADER_BACKEND=anthropic GRADER_MODEL=claude-opus-4-8 python grade_ab.py   # later
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from itertools import combinations
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent                 # roundtrip_eval/
ROOT = HERE.parent                                     # Harness_Translation/
GOLDEN = ROOT / "terminus2_roundtrip_golden.yaml"
SOLUTIONS = ROOT / "terminus2_golden_solutions.md"
PROMPTS = HERE / "prompts"
WORK_ROOT = Path(os.environ.get("EVAL_WORK_ROOT", HERE / "runs"))
GRADES_DIR = WORK_ROOT / "grades"

# baseline/handbook = Qwen (no handbook / + handbook); opus = the SAME baseline pipeline with
# Claude Opus, the "ceiling" arm. Thesis: does Qwen+handbook close the Qwen→Opus gap?
ARMS = ("baseline", "handbook", "opus")
# Keep prompts bounded; the diffs/plans are small in practice but guard anyway.
# Generous caps so NOTHING realistic is ever truncated (largest seen: ~31k diff, ~16k plan).
# Truncation biases against whichever arm produced MORE (its later hunks get cut), so we keep
# huge headroom — the judge's window (--max-model-len ~200k tokens ≈ ~600k chars) swallows
# these easily; the cap only guards against a pathological runaway file.
MAX_DIFF_CHARS = int(os.environ.get("GRADER_MAX_DIFF_CHARS", "160000"))
MAX_PLAN_CHARS = int(os.environ.get("GRADER_MAX_PLAN_CHARS", "80000"))


# --------------------------------------------------------------------------- I/O

def load_golden() -> dict:
    return yaml.safe_load(GOLDEN.read_text())


def load_solutions() -> dict[str, str]:
    """Split the answer-key markdown into {Qid: section text}."""
    text = SOLUTIONS.read_text()
    out: dict[str, str] = {}
    parts = re.split(r"^## (Q\d+)\b", text, flags=re.MULTILINE)
    # parts = [preamble, 'Q1', body1, 'Q2', body2, ...]
    for i in range(1, len(parts) - 1, 2):
        out[parts[i]] = parts[i + 1].strip()
    return out


def _clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n...[truncated {len(s) - n} chars]"


def load_candidate(arm: str, qid: str) -> dict | None:
    """Return {'plan':…, 'diff':…} for an arm/case, or None if it wasn't run."""
    case_dir = WORK_ROOT / arm / qid
    plan_f, diff_f = case_dir / "plan.md", case_dir / "agent.diff"
    if not plan_f.exists() and not diff_f.exists():
        return None
    return {
        "plan": _clip(plan_f.read_text() if plan_f.exists() else "", MAX_PLAN_CHARS),
        "diff": _clip(diff_f.read_text() if diff_f.exists() else "", MAX_DIFF_CHARS),
    }


# ---------------------------------------------------------------- prompt assembly

def _answer_key_block(case: dict, solution: str) -> str:
    """The shared answer-key context handed to both graders."""
    def lst(xs):
        return "\n".join(f"  - {x}" for x in (xs or [])) or "  (none)"

    return (
        f"REVIEWER REQUEST:\n{case['query'].strip()}\n\n"
        f"INTENDED BEHAVIOUR CHANGE:\n{case.get('expected_behavior_delta', '').strip()}\n\n"
        f"EXPECTED ANCHORS (must be changed):\n{lst(case.get('expected_anchors'))}\n\n"
        f"DISCRIMINATORS (hard, scattered — the real signal):\n"
        f"{lst(case.get('discriminators'))}\n\n"
        f"PRECISION TRAPS (must NOT be changed):\n"
        f"{lst(case.get('out_of_scope_precision_traps'))}\n\n"
        f"REFERENCE SOLUTION (intended edits; equivalent routes also count):\n{solution}\n"
    )


def build_absolute_messages(case: dict, solution: str, cand: dict) -> list[dict]:
    system = (PROMPTS / "grader_absolute.md").read_text()
    user = (
        _answer_key_block(case, solution)
        + "\n=== CANDIDATE PLAN ===\n" + (cand["plan"] or "(empty)")
        + "\n\n=== CANDIDATE GIT DIFF ===\n" + (cand["diff"] or "(empty diff)")
        + "\n\nGrade this candidate. Return only the JSON object."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_h2h_messages(case: dict, solution: str, a: dict, b: dict) -> list[dict]:
    """a/b are the already-A/B-assigned bundles (order randomized by caller)."""
    system = (PROMPTS / "grader_h2h.md").read_text()
    user = (
        _answer_key_block(case, solution)
        + "\n=== CANDIDATE A — PLAN ===\n" + (a["plan"] or "(empty)")
        + "\n\n=== CANDIDATE A — GIT DIFF ===\n" + (a["diff"] or "(empty diff)")
        + "\n\n=== CANDIDATE B — PLAN ===\n" + (b["plan"] or "(empty)")
        + "\n\n=== CANDIDATE B — GIT DIFF ===\n" + (b["diff"] or "(empty diff)")
        + "\n\nCompare A and B. Return only the JSON object."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --------------------------------------------------------------------- backends

def _extract_json(text: str) -> dict:
    """Pull the first balanced {...} object out of a model response and parse it."""
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in model output: {text[:200]!r}")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON braces in model output")


def _judge(chat, msgs: list[dict], retries: int = 3) -> dict:
    """Call the judge and parse its JSON, retrying transient failures (HTTP errors, gateway
    error codes, truncated / no-JSON replies). Raises the last error only after `retries`
    attempts — callers (main) skip the case rather than letting one blip kill a 120-call run."""
    import time
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return _extract_json(chat(msgs))
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                print(f"    judge attempt {attempt}/{retries} failed ({e}); retrying…")
                time.sleep(2)
    assert last is not None
    raise last


def make_backend():
    """Return chat(messages) -> str, selected by GRADER_BACKEND."""
    backend = os.environ.get("GRADER_BACKEND", "openai").lower()

    if backend == "mock":
        return _mock_backend

    if backend == "openai":
        import requests

        base = (os.environ.get("GRADER_BASE_URL")
                or os.environ.get("LLM_BASE_URL")
                or "http://localhost:8000/v1").rstrip("/")
        model = (os.environ.get("GRADER_MODEL")
                 or os.environ.get("LLM_MODEL")
                 or "Qwen3-Coder-30B")
        key = (os.environ.get("GRADER_API_KEY")
               or os.environ.get("LLM_API_KEY") or "EMPTY")
        temp = float(os.environ.get("GRADER_TEMPERATURE", "0.0"))

        def chat(messages: list[dict]) -> str:
            r = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "messages": messages,
                      "temperature": temp, "max_tokens": 16384},
                # Bypass any host http(s)_proxy (e.g. the cluster Squid) — the judge talks to
                # a localhost vLLM; routing localhost through the proxy returns an HTML error.
                proxies={"http": None, "https": None},
                timeout=600,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

        chat.label = f"openai:{model}@{base}"  # type: ignore[attr-defined]
        return chat

    if backend == "anthropic":
        # Optional / for later. Uses the official SDK + adaptive thinking.
        import anthropic  # noqa: F401  (only imported when selected)

        client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env
        model = os.environ.get("GRADER_MODEL", "claude-opus-4-8")

        def chat(messages: list[dict]) -> str:
            system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
            convo = [m for m in messages if m["role"] != "system"]
            resp = client.messages.create(
                model=model,
                max_tokens=8000,
                system=system,
                thinking={"type": "adaptive"},
                messages=convo,
            )
            return "".join(b.text for b in resp.content if b.type == "text")

        chat.label = f"anthropic:{model}"  # type: ignore[attr-defined]
        return chat

    if backend == "trpc":
        # Internal gpt-eval gateway (see ../test_api.py) → Azure OpenAI GPT-5.4, HMAC-signed.
        # Creds/host default to the test_api.py values; override via GRADER_TRPC_* env.
        import base64
        import datetime
        import hashlib
        import hmac
        import uuid

        import requests

        host = os.environ.get("GRADER_TRPC_HOST",
                              "http://trpc-gpt-eval.production.polaris:8080").rstrip("/")
        sid = os.environ.get("GRADER_TRPC_USER", "")
        skey = os.environ.get("GRADER_TRPC_KEY", "")
        model = os.environ.get("GRADER_MODEL", "api_azure_openai_gpt-5.4-2026-03-05")
        url = host + "/api/v1/data_eval"

        def _auth() -> dict:
            source = "grader"
            dt = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
            sign = base64.b64encode(
                hmac.new(skey.encode(), f"date: {dt}\nsource: {source}".encode(),
                         hashlib.sha1).digest()).decode()
            auth = (f'hmac id="{sid}", algorithm="hmac-sha1", '
                    f'headers="date source", signature="{sign}"')
            return {"Apiversion": "v2.03", "Authorization": auth, "Date": dt, "Source": source}

        def _extract(data: object) -> str:
            # Confirmed gateway shape: {"code":0,"msg":"ok","answer":[{"type":"text","value":...}]}
            if isinstance(data, str):
                return data
            if isinstance(data, dict):
                if data.get("code") not in (0, None):
                    raise ValueError(f"[trpc] gateway error code={data.get('code')}: "
                                     f"{data.get('msg')}")
                ans = data.get("answer")
                if isinstance(ans, list):
                    txt = "".join(
                        b.get("value", "") for b in ans
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("value"))
                    if txt.strip():
                        return txt
            raise ValueError(f"[trpc] no reply text found in response: {json.dumps(data)[:1000]}")

        def chat(messages: list[dict]) -> str:
            # The example shows a single 'user' turn with content=[{type,value}]; fold the
            # system prompt into the user turn to be safe across model markers.
            system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
            body = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
            full = f"{system}\n\n{body}" if system else body
            r = requests.post(
                url, headers=_auth(),
                json={"request_id": str(uuid.uuid4()), "model_marker": model,
                      "messages": [{"role": "user",
                                    "content": [{"type": "text", "value": full}]}],
                      "params": {}, "timeout": 6000},
                timeout=3600,
            )
            r.raise_for_status()
            return _extract(r.json())

        chat.label = f"trpc:{model}"  # type: ignore[attr-defined]
        return chat

    raise ValueError(f"unknown GRADER_BACKEND={backend!r} (openai|anthropic|trpc|mock)")


def _mock_backend(messages: list[dict]) -> str:
    """Deterministic stub so the pipeline runs with no model. Marks nothing hit."""
    user = messages[-1]["content"]
    is_h2h = "CANDIDATE A" in user
    if is_h2h:
        return json.dumps({
            "dimensions": {k: "tie" for k in
                           ("anchor_coverage", "discriminators", "precision", "correctness")},
            "winner": "tie", "margin": "tie",
            "reasoning": "[mock backend] no model called.",
        })
    # absolute: echo the anchors with hit=false
    anchors = re.findall(r"^  - (.+)$", user, flags=re.MULTILINE)
    return json.dumps({
        "expected_anchors": [{"anchor": a, "plan_hit": False, "diff_hit": False,
                              "evidence": "[mock]"} for a in anchors[:1]],
        "discriminators": [], "precision_traps": [],
        "correctness": 0, "honest_no_anchor": None, "notes": "[mock backend] no model called.",
    })


# ----------------------------------------------------------------- aggregation

def _aggregate_absolute(case: dict, judged: dict) -> dict:
    """Compute recall / discriminator-rate / precision from the judge's booleans, for BOTH
    the PLAN (planner localization = the handbook signal) and the DIFF (end-to-end result,
    incl. executor noise). Diff-level keeps the plain names (`recall`, ...); plan-level is
    prefixed `plan_`."""
    exp = judged.get("expected_anchors", [])
    disc = judged.get("discriminators", [])
    traps = judged.get("precision_traps", [])
    # Denominators are the GOLDEN counts (authoritative), so a judge that drops an
    # item from its echoed list can't silently shrink the denominator.
    n_exp = len(case.get("expected_anchors") or []) or len(exp)
    n_disc = len(case.get("discriminators") or []) or len(disc)

    def _hit(item: dict, kind: str) -> bool:
        v = item.get(f"{kind}_hit")
        return bool(v) if v is not None else bool(item.get("hit"))  # legacy fallback

    def _touched(item: dict, kind: str) -> bool:
        v = item.get(f"{kind}_touched")
        return bool(v) if v is not None else bool(item.get("touched"))

    out: dict = {"correctness": judged.get("correctness"),
                 "honest_no_anchor": judged.get("honest_no_anchor")}
    for kind in ("diff", "plan"):
        exp_hits = sum(1 for a in exp if _hit(a, kind))
        disc_hits = sum(1 for a in disc if _hit(a, kind))
        trap_hits = sum(1 for t in traps if _touched(t, kind))
        recall = exp_hits / n_exp if n_exp else 0.0
        disc_all = (disc_hits == n_disc) if n_disc else True
        produced = exp_hits + trap_hits  # rough denom for precision
        precision = (1 - trap_hits / produced) if produced else 1.0
        p = "" if kind == "diff" else "plan_"  # diff = end-to-end (default names)
        out.update({
            f"{p}recall": round(recall, 3),
            f"{p}expected_hit": f"{exp_hits}/{n_exp}",
            f"{p}discriminators_hit": f"{disc_hits}/{n_disc}",
            f"{p}discriminator_all_hit": disc_all,
            f"{p}traps_touched": trap_hits,
            f"{p}precision": round(precision, 3),
        })
    return out


def _resolve_h2h(judged: dict, label_for: dict) -> dict:
    """Map the judge's A/B verdict back to baseline/handbook arm names."""
    def back(v):
        return label_for.get(v, v)  # "A"/"B" -> arm name; "tie" stays
    dims = {k: back(v) for k, v in (judged.get("dimensions") or {}).items()}
    return {
        "winner": back(judged.get("winner")),
        "margin": judged.get("margin"),
        "dimensions": dims,
        "reasoning": judged.get("reasoning", ""),
    }


# ----------------------------------------------------------------------- driver

def grade_case(case: dict, solution: str, cands: dict, chat, dry: bool) -> dict | None:
    qid = case["id"]
    result: dict = {"id": qid, "title": case.get("title"),
                    "failure_mode": case.get("failure_mode"), "absolute": {}, "h2h": None}

    for arm in ARMS:
        cand = cands.get(arm)
        if cand is None:
            continue
        msgs = build_absolute_messages(case, solution, cand)
        if dry:
            print(f"\n########## ABSOLUTE :: {qid} :: {arm} ##########")
            print(msgs[1]["content"])
            continue
        judged = _judge(chat, msgs)
        result["absolute"][arm] = {"scores": _aggregate_absolute(case, judged), "detail": judged}

    # head-to-head for EVERY pair of arms that both produced output. With 3 arms this is
    # baseline-vs-handbook (the A/B), handbook-vs-opus ("does Qwen+handbook reach Opus?"), and
    # baseline-vs-opus (the raw small-vs-big gap). Result keyed by "<arm1>_vs_<arm2>".
    qnum = int(re.sub(r"\D", "", qid) or 0)
    present = [a for a in ARMS if cands.get(a)]
    h2h_pairs: dict = {}
    for arm1, arm2 in combinations(present, 2):
        # deterministic but case-dependent A/B order (no RNG; avoids position bias being
        # correlated with arm across cases): even Q-number -> arm1=A.
        a_arm, b_arm = (arm1, arm2) if qnum % 2 == 0 else (arm2, arm1)
        label_for = {"A": a_arm, "B": b_arm}
        msgs = build_h2h_messages(case, solution, cands[a_arm], cands[b_arm])
        key = f"{arm1}_vs_{arm2}"
        if dry:
            print(f"\n########## H2H :: {qid} :: {key} :: A={a_arm} B={b_arm} ##########")
            print(msgs[1]["content"])
        else:
            judged = _judge(chat, msgs)
            h2h_pairs[key] = {"order": label_for, **_resolve_h2h(judged, label_for)}
    result["h2h"] = h2h_pairs or None

    return None if dry else result


def summarize(results: list[dict]) -> dict:
    summary: dict = {"n_cases": len(results), "arms": {}, "h2h": {}}
    for arm in ARMS:
        rows = [r["absolute"][arm]["scores"] for r in results if arm in r["absolute"]]
        if not rows:
            continue
        n = len(rows)
        summary["arms"][arm] = {
            "n": n,
            # PLANNING (planner localization = the handbook signal)
            "planning_recall": round(sum(x["plan_recall"] for x in rows) / n, 3),
            "planning_discriminator_rate": round(
                sum(1 for x in rows if x["plan_discriminator_all_hit"]) / n, 3),
            "planning_precision": round(sum(x["plan_precision"] for x in rows) / n, 3),
            # END-TO-END (plan as applied by the executor; incl. executor noise)
            "macro_recall": round(sum(x["recall"] for x in rows) / n, 3),
            "discriminator_rate": round(sum(1 for x in rows if x["discriminator_all_hit"]) / n, 3),
            "mean_precision": round(sum(x["precision"] for x in rows) / n, 3),
            "mean_correctness": round(
                sum((x["correctness"] or 0) for x in rows) / n, 2),
        }
    # h2h is per-pair: aggregate win counts + margin spread for each matchup present.
    pair_keys: set = set()
    for r in results:
        if r.get("h2h"):
            pair_keys.update(r["h2h"].keys())
    for key in pair_keys:
        verdicts = [r["h2h"][key] for r in results if r.get("h2h") and key in r["h2h"]]
        if not verdicts:
            continue
        wins: dict = {}
        margins: dict = {"clear": 0, "slight": 0, "tie": 0}
        for v in verdicts:
            wins[v["winner"]] = wins.get(v["winner"], 0) + 1
            margins[v.get("margin", "tie")] = margins.get(v.get("margin", "tie"), 0) + 1
        summary["h2h"][key] = {"n": len(verdicts), "wins": wins, "margins": margins}
    return summary


def write_report(results: list[dict], summary: dict) -> None:
    GRADES_DIR.mkdir(parents=True, exist_ok=True)
    for r in results:
        (GRADES_DIR / f"{r['id']}.json").write_text(json.dumps(r, indent=2, ensure_ascii=False))
    (GRADES_DIR / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    lines = ["# roundtrip_eval — baseline vs handbook grades", ""]
    lines.append(f"Graded {summary['n_cases']} case(s).  Judge: "
                 f"{os.environ.get('GRADER_BACKEND', 'openai')}")
    lines.append("")

    # ---- HEADLINE ----
    def _pct(x: float) -> str:
        return f"{round(x * 100)}%"

    # 3-arm gap-closed must compare ONLY cases all three arms graded — else a partial opus run
    # would pit opus's k-case average against Qwen's full-set average. For a full run this is
    # identical to the per-arm summary averages.
    TRI = ("baseline", "handbook", "opus")
    tri_rows = [r for r in results if all(a in r["absolute"] for a in TRI)]
    tri: dict = {}
    for arm in TRI:
        scs = [r["absolute"][arm]["scores"] for r in tri_rows]
        if scs:
            tri[arm] = {
                "planning_recall": sum(s["plan_recall"] for s in scs) / len(scs),
                "planning_discriminator_rate":
                    sum(1 for s in scs if s["plan_discriminator_all_hit"]) / len(scs),
            }

    base = summary["arms"].get("baseline")
    hb = summary["arms"].get("handbook")

    if len(tri) == 3:
        # 3-arm "ceiling" view: how much of the Qwen->Opus gap does the handbook close?
        b3, h3, o3 = tri["baseline"], tri["handbook"], tri["opus"]

        def _row3(label: str, key: str) -> str:
            b, h, o = b3[key], h3[key], o3[key]
            gap = o - b
            if abs(gap) < 1e-9:
                closed = "—" if abs(h - b) < 1e-9 else "n/a (base≈Opus)"
            else:
                closed = f"{round((h - b) / gap * 100)}%"
            reaches = f"{round(h / o * 100)}% of Opus" if o > 1e-9 else "—"
            return f"| {label} | {_pct(b)} | {_pct(h)} | {_pct(o)} | {closed} | {reaches} |"
        lines += [
            f"## ⭐ Headline — does the handbook let Qwen reach Opus?  (PLANNING, "
            f"n={len(tri_rows)} cases all 3 arms ran)",
            "| signal | Qwen baseline | **Qwen + handbook** | Opus (ceiling) | gap closed | hb reaches |",
            "|---|---|---|---|---|---|",
            _row3("**discriminator hit-rate**  (core signal)", "planning_discriminator_rate"),
            _row3("recall  (anchors localized)", "planning_recall"),
            "",
            "_**gap closed** = (handbook − baseline) / (Opus − baseline): the fraction of the "
            "small-model→big-model gap the handbook closes. 100% = Qwen+handbook matched Opus._",
            "",
        ]
    elif base and hb:
        # 2-arm fallback (the opus ceiling arm has not been run yet)
        def _row(label: str, key: str) -> str:
            b, h = base[key], hb[key]
            d = h - b
            arrow = "▲" if d > 0 else ("▼" if d < 0 else "·")
            return f"| {label} | {_pct(b)} | {_pct(h)} | {arrow} {'+' if d >= 0 else ''}{round(d * 100)}pp |"
        lines += [
            "## ⭐ Headline — does the handbook help?  (PLANNING)  _[opus ceiling arm not run yet]_",
            "| signal | baseline | handbook | Δ (hb − base) |",
            "|---|---|---|---|",
            _row("**discriminator hit-rate**  (core signal)", "planning_discriminator_rate"),
            _row("recall  (anchors localized)", "planning_recall"),
            "",
        ]

    lines.append("## Aggregate — PLANNING (planner localization = the handbook signal)")
    lines.append("| arm | n | planning_recall | planning_discriminator_rate | planning_precision |")
    lines.append("|---|---|---|---|---|")
    for arm in ARMS:
        a = summary["arms"].get(arm)
        if a:
            lines.append(f"| {arm} | {a['n']} | {a['planning_recall']} | "
                         f"{a['planning_discriminator_rate']} | {a['planning_precision']} |")
    lines += ["", "## Aggregate — END-TO-END (plan as applied by the executor; incl. executor noise)"]
    lines.append("| arm | n | macro_recall | discriminator_rate | mean_precision | mean_correctness |")
    lines.append("|---|---|---|---|---|---|")
    for arm in ARMS:
        a = summary["arms"].get(arm)
        if a:
            lines.append(f"| {arm} | {a['n']} | {a['macro_recall']} | "
                         f"{a['discriminator_rate']} | {a['mean_precision']} | {a['mean_correctness']} |")
    if summary.get("h2h"):
        lines += ["", "## Head-to-head — direct comparison",
                  "_GPT-5.4 judges the two candidates' plans+diffs against each other (golden shown "
                  "only as a reference for what to look for). Golden-light: the verdict is "
                  '"which work is better", not "who matched the Claude golden"._', "",
                  "| matchup | n | wins | ties | margin (clear/slight) |", "|---|---|---|---|---|"]
        # thesis matchup first
        order = ["handbook_vs_opus", "baseline_vs_opus", "baseline_vs_handbook"]
        keys = [k for k in order if k in summary["h2h"]]
        keys += [k for k in summary["h2h"] if k not in order]
        for key in keys:
            h = summary["h2h"][key]
            a1, a2 = key.split("_vs_")
            w, mg = h["wins"], h["margins"]
            star = " ⭐" if key == "handbook_vs_opus" else ""
            lines.append(f"| **{a1} vs {a2}**{star} | {h['n']} | {a1} {w.get(a1,0)} · "
                         f"{a2} {w.get(a2,0)} | {w.get('tie',0)} | {mg.get('clear',0)}/{mg.get('slight',0)} |")
        if "handbook_vs_opus" in summary["h2h"]:
            hv = summary["h2h"]["handbook_vs_opus"]
            w = hv["wins"]
            lines += ["", f"_⭐ **Does Qwen+handbook reach Opus?**  handbook won "
                      f"{w.get('handbook',0)}, Opus won {w.get('opus',0)}, tied {w.get('tie',0)} "
                      f"of {hv['n']}. (tie/win for handbook ⇒ reached Opus on that case.)_"]
    lines += ["", "## Per-case  (plan_recall → / diff_recall, base vs hb)",
              "| Q | plan recall (base→hb) | diff recall (base→hb) | disc-all plan (base→hb) | hb-vs-opus |",
              "|---|---|---|---|---|"]
    for r in results:
        b = r["absolute"].get("baseline", {}).get("scores", {})
        h = r["absolute"].get("handbook", {}).get("scores", {})
        win = ((r.get("h2h") or {}).get("handbook_vs_opus") or {}).get("winner", "—")
        lines.append(
            f"| {r['id']} | {b.get('plan_recall','—')}→{h.get('plan_recall','—')} | "
            f"{b.get('recall','—')}→{h.get('recall','—')} | "
            f"{b.get('plan_discriminator_all_hit','—')}→{h.get('plan_discriminator_all_hit','—')} | {win} |")

    # ---- CONCRETE EVIDENCE: discriminators the handbook PLAN caught but baseline missed ----
    lines += ["", "## 🔑 Hidden anchors the handbook caught but baseline missed",
              "_Discriminators where the handbook plan localized/flagged the site and the "
              "baseline plan did not — the concrete, case-by-case handbook advantage._", ""]
    any_catch = False
    for r in results:
        bdet = r["absolute"].get("baseline", {}).get("detail", {})
        hdet = r["absolute"].get("handbook", {}).get("detail", {})
        bmap = {d.get("anchor"): d for d in (bdet.get("discriminators") or [])}
        for hd in (hdet.get("discriminators") or []):
            if hd.get("plan_hit") and not bmap.get(hd.get("anchor"), {}).get("plan_hit"):
                any_catch = True
                ev = str(hd.get("evidence", "")).replace("\n", " ")[:140]
                lines.append(f"- **{r['id']}** — {str(hd.get('anchor',''))[:110]}  · _{ev}_")
    if not any_catch:
        lines.append("- (none yet — run a real judge)")

    (GRADES_DIR / "SUMMARY.md").write_text("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", help="comma-separated Qids (default: every Q both arms have)")
    ap.add_argument("--dry-run", action="store_true", help="print prompts, do not call the model")
    args = ap.parse_args()

    golden = load_golden()
    solutions = load_solutions()
    cases = {c["id"]: c for c in golden["test_cases"]}

    if args.cases:
        want = [q.strip() for q in args.cases.split(",")]
    else:
        # default: cases where at least one arm has output (h2h restricted to both)
        want = [c["id"] for c in golden["test_cases"]
                if any(load_candidate(a, c["id"]) for a in ARMS)]

    chat = None if args.dry_run else make_backend()
    if chat is not None:
        print(f"judge backend: {getattr(chat, 'label', os.environ.get('GRADER_BACKEND','openai'))}")

    results = []
    for qid in want:
        if qid not in cases:
            print(f"  !! unknown case {qid}, skipping")
            continue
        cands = {a: load_candidate(a, qid) for a in ARMS}
        if not any(cands.values()):
            print(f"  !! no outputs for {qid}, skipping")
            continue
        have = [a for a in ARMS if cands[a]]
        print(f"== {qid} ({', '.join(have)}) ==")
        try:
            r = grade_case(cases[qid], solutions.get(qid, "(no reference solution)"),
                           cands, chat, args.dry_run)
        except Exception as e:  # noqa: BLE001  — one bad case must not kill the whole run
            print(f"  !! grading {qid} failed after retries ({e}); skipping this case")
            continue
        if r is not None:
            results.append(r)

    if args.dry_run or not results:
        return
    summary = summarize(results)
    write_report(results, summary)
    print(f"\nDone. {summary['n_cases']} cases → {GRADES_DIR}/ (SUMMARY.md, summary.json, <Q>.json)")
    for arm in ARMS:
        a = summary["arms"].get(arm)
        if a:
            print(f"  {arm:9s} recall={a['macro_recall']} disc_rate={a['discriminator_rate']} "
                  f"prec={a['mean_precision']} correctness={a['mean_correctness']}")
    for key, h in summary.get("h2h", {}).items():
        a1, a2 = key.split("_vs_")
        w = h["wins"]
        print(f"  H2H {key}: {a1} {w.get(a1,0)} | {a2} {w.get(a2,0)} | tie {w.get('tie',0)}")


if __name__ == "__main__":
    sys.exit(main())
