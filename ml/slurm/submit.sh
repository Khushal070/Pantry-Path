#!/usr/bin/env bash
# Submit the explorer grid as a job array plus the aggregate job that waits for it.
#   bash ml/slurm/submit.sh [SEARCH_ID]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
pp_require PP_PARTITION PP_GPU_GRES PP_TIME PP_AGG_PARTITION PP_AGG_TIME PP_PYTHON_MODULE \
  PP_ROOT PP_SERVER
pp_python
cd "$PP_REPO/ml"

SEARCH_ID="${1:-$(date -u +%Y%m%dT%H%M%SZ)-explorer}"
count="$(python -m pantrypath_ml.search count --grid explorer --server "$PP_SERVER")"
account=()
[ -n "$PP_ACCOUNT" ] && account=(--account "$PP_ACCOUNT")
export PP_REPO SEARCH_ID

array_job="$(sbatch --parsable "${account[@]}" --partition "$PP_PARTITION" \
  --gres "$PP_GPU_GRES" --time "$PP_TIME" --array "0-$((count - 1))%$PP_MAX_PARALLEL" \
  --output "$PP_ROOT/logs/%x-%A_%a.out" --export ALL slurm/run_search.sbatch)"
aggregate_job="$(sbatch --parsable "${account[@]}" --partition "$PP_AGG_PARTITION" \
  --time "$PP_AGG_TIME" --dependency "afterok:$array_job" \
  --output "$PP_ROOT/logs/%x-%j.out" --export ALL slurm/aggregate.sbatch)"

echo "Search $SEARCH_ID: $count tasks in array job $array_job, aggregate job $aggregate_job"
echo "Monitor: squeue -u \$USER; sacct -j $array_job --format JobID,State,Elapsed"
