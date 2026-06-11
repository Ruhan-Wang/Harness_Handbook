#!/usr/bin/env bash
# run_in_apptainer.sh — run the eval harness inside an isolated Apptainer container.
#
# Restricted (nested/k8s) hosts block apptainer's loop/overlay/caps, so we first create a
# user namespace with `unshare --user --map-root-user --mount` (where we are root and have
# the needed capabilities), then `apptainer exec --contain`.
#
# Isolation: the project is bind-mounted READ-ONLY, so the agent cannot modify harness
# code. ONLY the outputs dir (roundtrip_eval/runs) is bind-mounted writable ON TOP of the
# read-only project — so writes are confined to runs/ and nowhere else in the harness.
# Host network is shared, so the agent reaches local vLLM.
#
# Usage:
#   LLM_BASE_URL=http://localhost:8000/v1 \
#     bash run_in_apptainer.sh --arm baseline --cases Q1
#   -> outputs (+ sandboxes) under roundtrip_eval/runs/<arm>/<case>/
#
# Overridable env: PROJ, IMG, OUT, CONDA_ENV, LLM_MODEL, LLM_API_KEY, LLM_BASE_URL
set -euo pipefail

PROJ="${PROJ:-/cq_1/share_1603164/user/ruhwang/Project}"        # project root (its REAL path)
IMG="${IMG:-/tmp/base_dir}"                                     # apptainer sandbox DIR (not .sif)
OUT="${OUT:-$PROJ/Harness_Translation/roundtrip_eval/runs}"     # writable outputs dir
CONDA_ENV="${CONDA_ENV:-/opt/conda/envs/torch-base}"
RUN_EVAL="$PROJ/Harness_Translation/roundtrip_eval/run_eval.py"

mkdir -p "$OUT"

exec unshare --user --map-root-user --mount \
  apptainer exec --contain --cleanenv \
    --bind "$CONDA_ENV:$CONDA_ENV:ro" \
    --bind "$PROJ:$PROJ:ro" \
    --bind "$OUT:$OUT:rw" \
    --env PATH="$CONDA_ENV/bin:/usr/bin:/bin" \
    --env HOME=/tmp \
    --env no_proxy="localhost,127.0.0.1,0.0.0.0,::1" \
    --env NO_PROXY="localhost,127.0.0.1,0.0.0.0,::1" \
    --env http_proxy="" --env https_proxy="" \
    --env HTTP_PROXY="" --env HTTPS_PROXY="" \
    --env PYTHONPATH="$PROJ" \
    --env EVAL_WORK_ROOT="$OUT" \
    --env LLM_MODEL="${LLM_MODEL:-Qwen3-Coder-30B}" \
    --env LLM_API_KEY="${LLM_API_KEY:-EMPTY}" \
    --env LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:8000/v1}" \
    --env LLM_API_TYPE="${LLM_API_TYPE:-}" \
    --env PYTHONUNBUFFERED=1 \
    --env LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-}" \
    --env LLM_MAX_CONTEXT="${LLM_MAX_CONTEXT:-}" \
    --env TOOL_OUTPUT_LIMIT="${TOOL_OUTPUT_LIMIT:-}" \
    "$IMG" \
    "$CONDA_ENV/bin/python" "$RUN_EVAL" "$@"
