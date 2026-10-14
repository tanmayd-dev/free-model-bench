#!/usr/bin/env bash
#
# The overnight entry point.
#
#   ./scripts/run-eval.sh                      # same as --mode auto (daily)
#   ./scripts/run-eval.sh --mode smoke         # 1 model x 1 task, ~10 min
#   ./scripts/run-eval.sh --mode full --force  # monthly: re-run everything
#   ./scripts/run-eval.sh --mode auto --budget-minutes 360
#
# Everything it does is idempotent: it reads state/eval.db, works out what is
# missing, and only runs that. Interrupt it and it will pick up where it left
# off next time.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export MODEL_EVAL_STATE="${MODEL_EVAL_STATE:-$ROOT/state}"

MODE="auto"
ARGS=()
PASSTHROUGH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)            MODE="$2"; ARGS+=(--mode "$2"); shift 2 ;;
    --mode=*)          MODE="${1#*=}"; ARGS+=(--mode "${1#*=}"); shift ;;
    --smoke)           MODE="smoke"; ARGS+=(--mode smoke); shift ;;
    --daily)           MODE="daily"; ARGS+=(--mode daily); shift ;;
    --monthly|--full)  MODE="full"; ARGS+=(--mode full); shift ;;
    --force)           ARGS+=(--force); shift ;;
    --concurrency)     ARGS+=(--concurrency "$2"); shift 2 ;;
    --concurrency=*)   ARGS+=(--concurrency "${1#*=}"); shift ;;
    --budget-minutes)  ARGS+=(--budget-minutes "$2"); shift 2 ;;
    --budget-minutes=*) ARGS+=(--budget-minutes "${1#*=}"); shift ;;
    --models)          ARGS+=(--models "$2"); shift 2 ;;
    --models=*)        ARGS+=(--models "${1#*=}"); shift ;;
    --tasks)           ARGS+=(--tasks "$2"); shift 2 ;;
    --tasks=*)         ARGS+=(--tasks "${1#*=}"); shift ;;
    --profile)         ARGS+=(--profile "$2"); shift 2 ;;
    --profile=*)       ARGS+=(--profile "${1#*=}"); shift ;;
    --source)          ARGS+=(--source "$2"); shift 2 ;;
    --no-judge)        ARGS+=(--no-judge); shift ;;
    --no-refresh)      ARGS+=(--no-refresh); shift ;;
    -h|--help)         sed -n '2,16p' "$0"; exit 0 ;;
    *)                 ARGS+=("$1"); shift ;;
  esac
done

# One harness at a time: a systemd timer and a manual run must not collide.
mkdir -p "$MODEL_EVAL_STATE/locks"
LOCK="$MODEL_EVAL_STATE/locks/run-eval.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another run-eval.sh is already running ($LOCK)" >&2
  exit 75   # EX_TEMPFAIL
fi

mkdir -p "$MODEL_EVAL_STATE/jobs" "$ROOT/results"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$MODEL_EVAL_STATE/jobs/run-$STAMP-$MODE.log"

echo "== model_eval  mode=$MODE  started $(date -u +%FT%TZ)"
echo "== log: $LOG"
set -x
python3 "$ROOT/src/cli.py" run "${ARGS[@]}" 2>&1 | tee -a "$LOG"
set +x
RC=${PIPESTATUS[0]}

echo "== finished $(date -u +%FT%TZ)  rc=$RC"
if [[ -f "$ROOT/results/leaderboard.md" ]]; then
  echo "== leaderboard: $ROOT/results/leaderboard.md"
fi
exit "$RC"
