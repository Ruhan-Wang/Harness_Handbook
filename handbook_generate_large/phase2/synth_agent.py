# -*- coding: utf-8 -*-
"""synth_agent.py — agentic, multi-round Phase 2b skeleton synthesis.

The default Phase 2b Step A (`synth_stages.synthesize_skeleton`) fires a SINGLE
LLM call: directory rollup + entry points -> one ordered stage list. It is blind
to how well that skeleton actually partitions the files — you only learn the
coverage gaps after Step B (`file_assign`) runs, and there is no loop to fix them.

This module replaces Step A with a draft -> assign -> enrich loop so the skeleton
grows from a shallow first draft to a rich one, until every file lands cleanly in
a stage (0 unassigned), or a round cap forces an honest stop.

Division of labour (see plan / README):
  - Step A (DRAFT)  — a NexAU agent drafts the initial skeleton. This is the
    exploratory, "draw the narrative spine" work. It runs against a standard
    OpenAI-compatible endpoint (LLM_* env), exactly like
    handbook_as_helper/code_agent.py — NOT the local api_client `Api`.
  - Step B (CONVERGE) — an actor-critic loop (phase2/critic.py, reused verbatim)
    assigns files, then splits / merges / adds / removes stages and reassigns the
    affected files, round after round. This runs on the local api_client `Api`, like the
    rest of Phase 2 (file_assign, the doctor, etc.).

`synth_agent_loop(...)` returns `(skeleton_doc, assign_result)` with the SAME
shapes `synth_stages.synth` returns, so Phase 2c / Phase 3 are unaffected.

This file holds Step A (the drafting agent) and the loop driver. The file-level
skeleton doctor (Step B's structural surgeon) lives in `skeleton_doctor_files.py`.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "shared"))

from api_client import Api  # noqa: E402

import file_assign  # noqa: E402
import nav_pack as navmod  # noqa: E402
import synth_stages  # noqa: E402

logger = logging.getLogger(__name__)

_AGENT_TOOLS_DIR = _HERE / "agent_tools"


# ─── Step A: the drafting agent ──────────────────────────────────────────────


_DRAFT_SYSTEM_PROMPT = """You are dividing a large codebase into the STAGES of a system handbook.

Produce the high-altitude NARRATIVE SPINE: the ordered phases the system goes
through, from process startup through its main work loop to teardown, followed by
cross-cutting concerns (shared infrastructure that spans phases).

WORKFLOW
1. Call `get_orientation` FIRST to see the directory map, the entry-point
   candidates (where execution starts), the highest fan-out files, and the
   external subsystems. This is your only source of truth about the codebase —
   you cannot read individual files.
2. Reason about the lifecycle ORDER from the entry points and directory roles.
3. Call `propose_skeleton` ONCE with your ordered stage list.
4. After it succeeds, emit a one-line summary and stop.

WHAT MAKES A GOOD DRAFT
- This is a SHALLOW FIRST DRAFT. A later automated step assigns every file to
  your stages and then enriches/splits/merges the skeleton until every file is
  covered. So do NOT try to be exhaustive or perfectly granular — get the
  lifecycle ORDER and the top-level shape right.
- Order the main stages by EXECUTION/LIFECYCLE, not alphabetically: start from
  the entry points, follow setup -> dispatch -> main loop / request handling ->
  per-unit work -> teardown.
- Aim for 12-25 top-level stages. Use substages (parent set to a stage id, ids
  like "stage-3.1") for depth instead of widening the top level.
- Put genuinely cross-cutting infrastructure (logging/telemetry, config,
  protocol/types, generic utils, persistence) into stages with crosscut=true,
  placed after the main flow.
- Make every description concrete enough that a later step can decide which
  files belong to the stage."""


_DRAFT_SYSTEM_PROMPT_ZH = """你在把一个大型代码库划分成系统手册的**阶段**（STAGE）。

产出高层的**叙事主线**：系统所经历的有序阶段——从进程启动，经主工作循环，到收尾，
最后是横切关注点（跨阶段的共享基础设施）。

工作流
1. **先**调用 `get_orientation` 查看目录图、入口点候选（执行从哪开始）、最高扇出文件、
   外部子系统。这是你了解代码库的唯一信息源——你无法读单个文件。
2. 从入口点和目录角色推断生命周期**顺序**。
3. 调用 `propose_skeleton` **一次**，给出你的有序阶段列表。
4. 成功后，输出一行总结并停止。

好草稿的标准
- 这是**浅层初稿**。后续自动步骤会把每个文件分配到你的阶段，再 enrich/split/merge 直到全覆盖。
  所以不必追求穷尽或完美粒度——把生命周期**顺序**和顶层形态弄对即可。
- 主阶段按**执行/生命周期**排序，不要按字母：从入口点开始，setup -> 分发 -> 主循环/请求处理
  -> 单元工作 -> teardown。
- 顶层阶段目标 12-25 个。用子阶段（parent 设为某 stage id，id 形如 "stage-3.1"）增加深度，
  而不是把顶层拉宽。
- 把真正横切的基础设施（日志/遥测、配置、协议/类型、通用工具、持久化）放进 crosscut=true 的阶段，
  置于主流程之后。
- **每个 stage 的 title 和 description 用中文写**，且足够具体，让后续步骤能判断哪些文件属于它。"""


def _build_orientation_text(graph: dict, nav: dict) -> str:
    """The single orientation block the drafting agent reads (bounded, dir-level)."""
    return navmod.render_orientation(nav)


def _make_draft_tools(graph: dict, nav: dict, state: dict[str, Any]) -> list[Any]:
    """Build the two closure-bound tools for the drafting agent.

    Closures capture `graph` / `nav` (read-only orientation) and `state` (the
    mutable slot the agent's proposed skeleton is written into). Using closures
    keeps these non-serializable objects out of the LLM-visible tool args — the
    agent only ever passes its stage list.
    """
    from nexau import Tool  # local import: NexAU is only needed on the agent path

    orientation_text = _build_orientation_text(graph, nav)

    def get_orientation() -> str:
        # Idempotent, no args — return the prebuilt bounded orientation block.
        return orientation_text

    def propose_skeleton(stages: Any = None, metadata: Any = None) -> dict[str, Any]:
        if not isinstance(stages, list) or not stages:
            return {
                "content": "propose_skeleton needs a non-empty 'stages' array.",
                "error": {"message": "stages must be a non-empty array",
                          "type": "INVALID_PARAMETER"},
            }
        raw = {"stages": stages,
               "metadata": metadata if isinstance(metadata, dict) else {}}
        try:
            skeleton_doc = synth_stages._normalize(raw)
        except Exception as e:  # noqa: BLE001
            return {
                "content": f"Failed to normalize the proposed skeleton: {e!r}",
                "error": {"message": str(e), "type": "NORMALIZE_FAILED"},
            }
        state["skeleton_doc"] = skeleton_doc
        ids = [s["id"] for s in skeleton_doc["stages"]]
        n_cross = sum(1 for s in skeleton_doc["stages"] if s.get("crosscut"))
        return {
            "content": (
                f"Recorded skeleton draft: {len(ids)} stage(s) "
                f"({n_cross} crosscut). ids: {', '.join(ids)}. "
                f"You are done — emit a one-line summary."
            ),
            "n_stages": len(ids),
            "stage_ids": ids,
        }

    return [
        Tool.from_yaml(str(_AGENT_TOOLS_DIR / "get_orientation.tool.yaml"),
                       binding=get_orientation),
        Tool.from_yaml(str(_AGENT_TOOLS_DIR / "propose_skeleton.tool.yaml"),
                       binding=propose_skeleton),
    ]


def _build_draft_agent(graph: dict, nav: dict, state: dict[str, Any],
                       lang: str = "en") -> Any:
    """Construct the NexAU drafting agent (standard OpenAI-compatible endpoint).

    Mirrors handbook_as_helper/code_agent.py: api_type=openai_chat_completion,
    tool_call_mode=structured, endpoint from LLM_* env. No tracers, no sub-agents.
    """
    from nexau import Agent, AgentConfig, LLMConfig

    missing = [v for v in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY")
               if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(
            "synth-mode=agent needs a standard OpenAI-compatible endpoint; "
            f"missing env var(s): {', '.join(missing)}"
        )

    llm_kwargs: dict[str, Any] = {
        "model": os.environ["LLM_MODEL"],
        "base_url": os.environ["LLM_BASE_URL"],
        "api_key": os.environ["LLM_API_KEY"],
        "api_type": os.environ.get("LLM_API_TYPE", "openai_chat_completion"),
        "temperature": float(os.environ.get("LLM_TEMPERATURE", "0.0")),
        "max_tokens": int(os.environ.get("LLM_MAX_TOKENS") or "8000"),
    }
    if os.environ.get("LLM_EXTRA_BODY"):
        llm_kwargs["extra_body"] = json.loads(os.environ["LLM_EXTRA_BODY"])

    config = AgentConfig(
        name="skeleton_drafter",
        system_prompt=_DRAFT_SYSTEM_PROMPT_ZH if lang == "zh" else _DRAFT_SYSTEM_PROMPT,
        system_prompt_type="string",
        tool_call_mode=os.environ.get("NEXAU_TOOL_CALL_MODE", "structured"),
        max_iterations=int(os.environ.get("SYNTH_AGENT_MAX_ITERS") or "12"),
        max_context_tokens=int(os.environ.get("LLM_MAX_CONTEXT") or "200000"),
        llm_config=LLMConfig(**llm_kwargs),
        tools=_make_draft_tools(graph, nav, state),
    )
    return Agent(config=config)


def draft_skeleton(graph: dict, file_purposes: dict[str, dict], *,
                   api: Api | None = None, lang: str = "en") -> dict:
    """Step A — draft the initial skeleton with a NexAU agent.

    Returns a normalized skeleton_doc. If the agent endpoint is unavailable or
    the agent never produced a usable draft, falls back to the one-shot
    `synth_stages.synthesize_skeleton` (which runs on the local api_client `Api`), so
    the loop always has an input and an endpoint outage never blocks Phase 2b.
    """
    nav = navmod.build_nav_pack(graph)
    state: dict[str, Any] = {"skeleton_doc": None}

    try:
        agent = _build_draft_agent(graph, nav, state, lang)
        task = (
            "Divide this codebase into the ordered stages of a system handbook. "
            "Call get_orientation, then call propose_skeleton with your draft."
        )
        result = agent.run(message=task, context={"working_directory": os.getcwd()})
        final_text = result[0] if isinstance(result, tuple) else str(result)
        logger.info("draft agent finished: %s", (final_text or "")[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("draft agent failed (%s); falling back to one-shot synth", e)

    skeleton_doc = state.get("skeleton_doc")
    if not skeleton_doc or not skeleton_doc.get("stages"):
        logger.warning("draft agent produced no skeleton; using one-shot fallback")
        fallback_api = api or Api()
        skeleton_doc = synth_stages.synthesize_skeleton(
            fallback_api, graph, file_purposes, lang=lang)
    return skeleton_doc


# ─── Step B: the convergence loop ────────────────────────────────────────────


def _count_overloaded(doctor, skeleton_doc: dict, assign_result: dict) -> int:
    """Number of currently-overloaded stages — the doctor's balance signal.

    Used by the loop's stuck-detection to tell a real balance improvement (an
    overloaded stage got split) from skeleton churn that absorbs nothing.
    """
    stats = doctor.compute_file_stage_stats(skeleton_doc, assign_result)
    return sum(1 for s in stats["per_stage"].values() if s["overloaded"])


def synth_agent_loop(api: Api, graph: dict, file_purposes: dict[str, dict],
                     *, max_rounds: int = 6, assign_workers: int = 6,
                     assign_batch_size: int = 25, doctor_workers: int = 1,
                     doctor_llm_workers: int | None = None,
                     use_agent_draft: bool = True, lang: str = "en"
                     ) -> tuple[dict, dict]:
    """Full agentic Phase 2b: draft -> assign -> (doctor -> reassign)* until every
    file is assigned or `max_rounds` is hit.

    Returns `(skeleton_doc, assign_result)` with the SAME shapes
    `synth_stages.synth` returns, so the caller (synth_stages.synth dispatch) and
    everything downstream (Phase 2c / 3) are unchanged.

    `api` is the local api_client `Api` — it drives the doctor and file_assign. The Step
    A drafting agent connects to the standard endpoint itself (LLM_* env) and does
    NOT use `api`; `api` is only passed to `draft_skeleton` for the one-shot
    fallback.

    `use_agent_draft`:
      - True (default, synth_mode="agent"): Step A is the NexAU drafting agent on
        the standard endpoint (LLM_* env), with a one-shot fallback if it fails.
      - False (synth_mode="doctor"): SKIP the NexAU agent entirely — draft with the
        one-shot `synth_stages.synthesize_skeleton` (local api_client `Api`), then run the
        SAME doctor convergence loop. No NexAU dependency, no LLM_* endpoint needed.
        This is the "one-shot draft + doctor enrich" path that, in practice,
        converges to the richest skeletons.

    Concurrency knobs (all api_client LLM calls; the bottleneck is LLM latency):
      - assign_workers: parallel file→stage batches in the first assign and in
        each round's subset re-assignment.
      - doctor_workers: parallel actor-critic diagnoses per round. 1 = single
        global actor (critics still run in parallel). >1 = one split-only
        actor-critic per overloaded stage + one global add/merge/remove pass, all
        concurrent (disjoint scopes, so proposals never collide).
      - doctor_llm_workers: GLOBAL cap on concurrent LLM calls from the doctor
        across BOTH nested pools (diagnosis × critics). Without it the naive peak
        is doctor_workers × 3, which silently multiplies the load. Defaults to a
        bounded ceiling (max(assign_workers, doctor_workers, 3)) so raising
        doctor_workers does not blow past the concurrency file_assign already
        uses safely; pass an explicit value to match your endpoint's rate limit.
    """
    import skeleton_doctor_files as doctor

    # Bound total concurrent doctor LLM calls across BOTH nested pools (per-round
    # diagnosis × per-proposal critics). The naive product (doctor_workers × 3)
    # would silently multiply load on the endpoint, so the default ceiling is the
    # same order of concurrency file_assign already runs at — not the product.
    if doctor_llm_workers is None:
        doctor_llm_workers = max(assign_workers, doctor_workers, 3)
    doctor.set_llm_concurrency(doctor_llm_workers)

    # Step A — draft. Either the NexAU agent (with one-shot fallback) or, when
    # use_agent_draft is False, the one-shot synth directly (no NexAU, no LLM_*).
    if use_agent_draft:
        skeleton_doc = draft_skeleton(graph, file_purposes, api=api, lang=lang)
        logger.info("synth_agent: agent draft has %d stage(s)",
                    len(skeleton_doc["stages"]))
    else:
        skeleton_doc = synth_stages.synthesize_skeleton(api, graph, file_purposes, lang=lang)
        logger.info("synth_agent: one-shot draft has %d stage(s) (doctor mode — "
                    "no NexAU agent)", len(skeleton_doc["stages"]))

    # First full assignment (every file, against the draft).
    assign_result = file_assign.assign_files(
        api, graph, skeleton_doc, purposes=file_purposes,
        max_workers=assign_workers, batch_size=assign_batch_size)
    cov = assign_result["coverage"]
    logger.info("synth_agent: initial assign %d/%d, %d unassigned",
                cov["n_assigned"], cov["n_files"], len(cov["unassigned"]))

    # Convergence loop.
    no_progress_rounds = 0
    for r in range(max_rounds):
        n_unassigned_before = len(assign_result["coverage"]["unassigned"])
        n_overloaded_before = _count_overloaded(doctor, skeleton_doc, assign_result)
        doc_result = doctor.run_doctor_files(
            api, skeleton_doc, assign_result, purposes=file_purposes,
            doctor_workers=doctor_workers, lang=lang)
        logger.info("synth_agent: round %d/%d — %s",
                    r + 1, max_rounds, doc_result["summary"])

        if doc_result["skeleton_changed"]:
            # Re-assign the affected files plus whatever is still unassigned.
            to_reassign = (doc_result["affected_files"]
                           | set(assign_result["coverage"]["unassigned"]))
            assign_result = doctor.reassign_subset(
                api, graph, skeleton_doc, to_reassign, assign_result,
                purposes=file_purposes, batch_size=assign_batch_size,
                max_workers=assign_workers)
            cov = assign_result["coverage"]
            logger.info("synth_agent: round %d reassigned %d file(s) -> "
                        "%d/%d assigned, %d unassigned",
                        r + 1, len(to_reassign), cov["n_assigned"],
                        cov["n_files"], len(cov["unassigned"]))

        n_unassigned_after = len(assign_result["coverage"]["unassigned"])
        n_overloaded_after = _count_overloaded(doctor, skeleton_doc, assign_result)

        # Converged: nothing unassigned AND the doctor made no change this round
        # (a stable skeleton with full coverage).
        if not n_unassigned_after and not doc_result["skeleton_changed"]:
            logger.info("synth_agent: CONVERGED after %d round(s) "
                        "(0 unassigned, skeleton stable)", r + 1)
            break

        # Stuck-detection. A round makes REAL progress if it improved EITHER
        # signal the doctor optimizes: coverage (fewer unassigned) OR balance
        # (fewer overloaded stages). Checking both avoids two false outcomes:
        #   - junk-add (caught): skeleton CHANGED (e.g. a new crosscut every round)
        #     but neither unassigned nor overloaded dropped — the doctor grows the
        #     skeleton with stages that absorb nothing. A naive
        #     "skeleton_changed => progress" check would spin all max_rounds here
        #     AND litter the skeleton with junk stages.
        #   - balance split when unassigned is already 0 (NOT a false stop): a
        #     legitimate split reduces overloaded count, so it still counts as
        #     progress even though coverage is unchanged.
        # Bail after two consecutive no-progress rounds; residual files stay
        # honestly in the unassigned bucket.
        made_progress = (n_unassigned_after < n_unassigned_before
                         or n_overloaded_after < n_overloaded_before)
        if not made_progress:
            no_progress_rounds += 1
            if no_progress_rounds >= 2:
                logger.warning("synth_agent: no coverage/balance progress for 2 "
                               "rounds (%d unassigned, %d overloaded) — stopping "
                               "early", n_unassigned_after, n_overloaded_after)
                break
        else:
            no_progress_rounds = 0
    else:
        cov = assign_result["coverage"]
        if cov["unassigned"]:
            logger.warning("synth_agent: hit max_rounds=%d with %d file(s) still "
                           "unassigned — left in the 'unassigned' bucket",
                           max_rounds, len(cov["unassigned"]))

    return skeleton_doc, assign_result


# ─── CLI (standalone: run Step A only, or the full loop) ─────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse
    import read_files
    import skeleton_yaml

    logging.basicConfig(format="[%(asctime)s][%(levelname)5s] %(message)s",
                        level=logging.INFO)
    ap = argparse.ArgumentParser(description="Agentic skeleton synthesis (Phase 2b)")
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--cards-dir", type=Path, required=True,
                    help="cards/ dir from read_files.py (2a)")
    ap.add_argument("--draft-only", action="store_true",
                    help="Run only Step A (draft) and print the skeleton tree")
    ap.add_argument("--max-rounds", type=int, default=6)
    ap.add_argument("--assign-workers", type=int, default=6,
                    help="parallel file→stage batches (first assign + each "
                         "round's subset re-assignment)")
    ap.add_argument("--assign-batch-size", type=int, default=25,
                    help="files per LLM call in file→stage assignment")
    ap.add_argument("--doctor-workers", type=int, default=1,
                    help="parallel actor-critic diagnoses per round. 1 = single "
                         "global actor (critics still parallel); >1 = one split-only "
                         "actor-critic per overloaded stage + a global "
                         "add/merge/remove pass, all concurrent (disjoint scopes).")
    ap.add_argument("--doctor-llm-workers", type=int, default=None,
                    help="global cap on concurrent doctor LLM calls across both "
                         "nested pools (diagnosis x critics). Defaults to "
                         "max(assign_workers, doctor_workers, 3).")
    ap.add_argument("--no-agent-draft", action="store_true",
                    help="doctor mode: skip the NexAU drafting agent — one-shot "
                         "drafts the skeleton (local api_client Api, no NexAU/LLM_*), then "
                         "the SAME doctor loop enriches it.")
    ap.add_argument("--skeleton-out", type=Path)
    ap.add_argument("--file-stage-out", type=Path)
    ap.add_argument("--lang", choices=["en", "zh"], default="en",
                    help="stage title/description language (en default; zh = Chinese)")
    args = ap.parse_args(argv)

    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    purposes = read_files.load_cards(args.cards_dir)
    api = Api()

    if args.draft_only:
        skeleton_doc = draft_skeleton(graph, purposes, api=api, lang=args.lang)
        print(f"\n=== DRAFT SKELETON: {len(skeleton_doc['stages'])} stage(s) ===")
        for s in skeleton_doc["stages"]:
            indent = "  " if s.get("parent") else ""
            tag = " [crosscut]" if s.get("crosscut") else ""
            print(f"{indent}{s['id']}: {s['title']}{tag}")
        return 0

    skeleton_doc, assign_result = synth_agent_loop(
        api, graph, purposes, max_rounds=args.max_rounds,
        assign_workers=args.assign_workers,
        assign_batch_size=args.assign_batch_size,
        doctor_workers=args.doctor_workers,
        doctor_llm_workers=args.doctor_llm_workers,
        use_agent_draft=not args.no_agent_draft, lang=args.lang)
    cov = assign_result["coverage"]
    print(f"\n=== FINAL: {len(skeleton_doc['stages'])} stage(s), "
          f"{cov['n_assigned']}/{cov['n_files']} assigned, "
          f"{len(cov['unassigned'])} unassigned ===")
    if args.skeleton_out:
        skeleton_yaml.save_yaml(skeleton_doc, args.skeleton_out)
        print(f"wrote {args.skeleton_out}")
    if args.file_stage_out:
        args.file_stage_out.parent.mkdir(parents=True, exist_ok=True)
        args.file_stage_out.write_text(
            json.dumps(assign_result, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"wrote {args.file_stage_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
