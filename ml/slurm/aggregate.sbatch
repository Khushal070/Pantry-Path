#!/usr/bin/env bash
# After every array task succeeded (--dependency=afterok): log all runs to MLflow from this
# one job, build the comparison table and charts, the sensitivity curves, and a proposed
# setting (written to reports/selection/<id>/chosen.json, not to registry/).
#SBATCH --job-name=pp-aggregate
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
set -euo pipefail
: "${PP_REPO:?submit with ml/slurm/submit.sh}" "${SEARCH_ID:?submit with ml/slurm/submit.sh}"
source "$PP_REPO/ml/slurm/common.sh"
pp_python
cd "$PP_REPO/ml"

python -m pantrypath_ml.aggregate --search-id "$SEARCH_ID" --log-mlflow
python -m pantrypath_ml.sensitivity curves --search-id "$SEARCH_ID"
python -m pantrypath_ml.select choose --search-id "$SEARCH_ID" \
  || echo "No setting passed every gate; see reports/search/$SEARCH_ID/comparison.md"
