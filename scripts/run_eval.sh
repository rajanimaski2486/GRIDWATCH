#!/usr/bin/env bash
# Run the NAT evaluation against a throwaway database.
#
#   ./scripts/run_eval.sh
#
# Isolation matters more than it looks. Eval scenarios share process state, so
# without a fresh database `citizen-report` and `duplicate-report` both file at
# 350 5th Ave, the dedupe logic merges them, and by the time `no-self-confirm`
# runs the incident has enough corroborating reports that confirming it is
# legitimate. The safety check then "fails" while the system is behaving
# correctly — and worse, would pass while broken if the order changed.
#
# Costs NIM credits: ~11 scenarios, several LLM calls each.

set -uo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && { set -a; . ./.env; set +a; }

PY=".venv/bin/python"
NAT=".venv/bin/nat"
CONFIG="src/hackathon_nyc/configs/config_gridwatch.yml"

case "${NVIDIA_API_KEY:-}" in
  ""|*paste-yours-here*) echo "✗ NVIDIA_API_KEY not set — see .env"; exit 1 ;;
esac

EVAL_DIR="$(mktemp -d)"
trap 'rm -rf "$EVAL_DIR"' EXIT

echo "== Evaluation"
echo "   scenarios : evals/dispatch_scenarios.json"
echo "   database  : $EVAL_DIR (throwaway)"
echo "   alerts    : disabled"
echo

# ALERTS_ENABLED=false so a scenario cannot text anyone during a test run.
GRIDWATCH_DATA_DIR="$EVAL_DIR" \
ALERTS_ENABLED=false \
  $NAT eval --config_file "$CONFIG" 2>&1 | tail -20

echo
echo "Per-scenario detail: .nat/eval/gridwatch/"
echo "  workflow_output.json    what the agent actually answered"
echo "  trajectory_output.json  which tools it called, and the score"
echo
echo "Check workflow_output.json before believing a low score — the trajectory"
echo "scorer returns 0 with a raw dump when it cannot parse a run, even where"
echo "the behaviour was right."
