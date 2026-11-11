#!/usr/bin/env bash
# One task of the explorer grid: start the model server on this node's GPU, run the task's
# A1 or A2 dev evaluation, stop the server. Submitted as an array by submit.sh, which sets
# partition, GPU, time, PP_REPO and SEARCH_ID; reports land in ml/reports/ on shared storage.
#SBATCH --job-name=pp-search
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
set -euo pipefail
: "${PP_REPO:?submit with ml/slurm/submit.sh}" "${SEARCH_ID:?submit with ml/slurm/submit.sh}"
source "$PP_REPO/ml/slurm/common.sh"
pp_python
cd "$PP_REPO/ml"

index="$SLURM_ARRAY_TASK_ID"
model="$(python -m pantrypath_ml.search model --grid explorer --server "$PP_SERVER" --index "$index")"
port=$((20000 + index % 10000))  # tasks sharing a node need different ports
echo "Task $index: $PP_SERVER serving $model on port $port"

if [ "$PP_SERVER" = "vllm" ]; then
  HF_HUB_OFFLINE=1 "$PP_ROOT/venv-vllm/bin/vllm" serve "$model" --port "$port" \
    --max-model-len 8192 --gpu-memory-utilization 0.85 --reasoning-parser qwen3 \
    >"$PP_ROOT/logs/server-$SLURM_ARRAY_JOB_ID-$index.log" 2>&1 &
  ready="http://127.0.0.1:$port/v1/models"
else
  OLLAMA_HOST="127.0.0.1:$port" OLLAMA_CONTEXT_LENGTH=8192 "$PP_ROOT/ollama/bin/ollama" serve \
    >"$PP_ROOT/logs/server-$SLURM_ARRAY_JOB_ID-$index.log" 2>&1 &
  ready="http://127.0.0.1:$port/api/version"
fi
server_pid=$!
trap 'kill $server_pid 2>/dev/null || true' EXIT
for _ in $(seq 1 180); do
  curl -sf "$ready" >/dev/null && break
  kill -0 "$server_pid" 2>/dev/null || { echo "Model server exited; see its log" >&2; exit 1; }
  sleep 5
done
curl -sf "$ready" >/dev/null || { echo "Model server not ready after 15 minutes" >&2; exit 1; }

export MODEL_SERVER_URL="http://127.0.0.1:$port"
python -m pantrypath_ml.search run-one --grid explorer --server "$PP_SERVER" --index "$index" \
  --search-id "$SEARCH_ID" --no-mlflow
