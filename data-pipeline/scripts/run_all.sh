#!/usr/bin/env bash
# Run the whole batch pipeline without Airflow, in the same order as the DAG
# (refine-all refines every dish in one step instead of one Airflow task per dish).
#
# Usage (from anywhere):
#   data-pipeline/scripts/run_all.sh                 # every step
#   data-pipeline/scripts/run_all.sh assemble-catalog  # resume from this step
# Stops at the first failing step. PYTHON overrides the interpreter
# (default: data-pipeline/.venv/bin/python).
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"

STEPS=(
  download-off filter-off validate-off
  download-foodcom clean-recipes group-dishes select-dishes pull-llm-cache refine-all
  assemble-catalog validate-catalog eval-catalog
  load-sqlite compute-stats check-drift bias-report dvc-version notify
)

start="${1:-${STEPS[0]}}"
if [[ ! " ${STEPS[*]} " == *" $start "* ]]; then
  echo "Unknown step '$start'. Steps: ${STEPS[*]}" >&2
  exit 2
fi

running=false
for step in "${STEPS[@]}"; do
  [[ "$step" == "$start" ]] && running=true
  $running || continue
  echo "==> $step"
  if ! "$PYTHON" -m pantrypath_pipeline "$step"; then
    echo "Step '$step' failed; fix it, then resume with: $0 $step" >&2
    exit 1
  fi
done
echo "All steps finished."
