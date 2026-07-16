#!/usr/bin/env bash
# Regenerate a plain-language handbook (deep Phase 2a + Phase 3) into NEW dirs
# (originals untouched). Point it at your source and your OpenAI-compatible model.
set -uo pipefail
cd "$(dirname "$0")"

export PYTHONUNBUFFERED=1   # flush logs live so `tail -f` shows progress immediately

# LLM: any OpenAI-compatible endpoint. Set these in your shell before running, or
# edit the defaults below. For a keyless local endpoint use OPENAI_API_KEY=EMPTY.
export OPENAI_API_KEY="${OPENAI_API_KEY:?set OPENAI_API_KEY (or =EMPTY for a keyless local endpoint)}"
export OPENAI_MODEL="${OPENAI_MODEL:-gpt-4o-mini}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.openai.com/v1}"

SRC="${SRC:-/path/to/source/repo}"
RW=32        # read (2a) workers
PW=32        # phase3 workers

run_one () {
  local lang="$1" work="$2"
  echo "############ $(date '+%H:%M:%S')  START lang=$lang work=$work ############"
  python3 run.py --source-root "$SRC" --work-dir "$work" \
      --phase 2a,3 --read-detail deep --read-batch-size 1 --read-workers "$RW" \
      --resume \
      --narrate-lang "$lang" --phase3-workers "$PW" --phase3-refresh
  echo "############ $(date '+%H:%M:%S')  DONE  lang=$lang work=$work (exit $?) ############"
}

run_one en work/repo_plain
run_one zh work/repo_zh_plain
echo "ALL DONE $(date '+%H:%M:%S')"
