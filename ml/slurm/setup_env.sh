#!/usr/bin/env bash
# One-time setup on an Explorer LOGIN node (compute nodes may have no internet):
# Python environments, the model server, every model the explorer grid needs, and the
# DVC catalog. Run from anywhere inside the repo checkout (on shared storage):
#   bash ml/slurm/setup_env.sh
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
pp_require PP_PYTHON_MODULE PP_ROOT PP_SERVER
mkdir -p "$PP_ROOT"/{hf,ollama-models,logs}

module load "$PP_PYTHON_MODULE"
if [ ! -x "$PP_ROOT/venv-ml/bin/python" ]; then
  python3 -m venv "$PP_ROOT/venv-ml"
fi
"$PP_ROOT/venv-ml/bin/pip" install --upgrade pip
"$PP_ROOT/venv-ml/bin/pip" install -r "$PP_REPO/ml/requirements.txt"
"$PP_ROOT/venv-ml/bin/pip" install --no-deps -e "$PP_REPO/shared" -e "$PP_REPO/data-pipeline" \
  -e "$PP_REPO/ml"
pp_python

# The DVC catalog (public remote; no credentials needed)
(cd "$PP_REPO" && dvc pull data-pipeline/data/processed/dish_profiles.json.dvc \
  data-pipeline/data/processed/pantrypath.sqlite.dvc)

models="$(cd "$PP_REPO/ml" && for i in $(seq 0 $(($(python -m pantrypath_ml.search count \
  --grid explorer --server "$PP_SERVER") - 1))); do python -m pantrypath_ml.search model \
  --grid explorer --server "$PP_SERVER" --index "$i"; done | sort -u)"
echo "Models for the explorer grid on $PP_SERVER: $models"

if [ "$PP_SERVER" = "vllm" ]; then
  # vLLM gets its own environment: its torch pins must not touch the evaluation's
  if [ ! -x "$PP_ROOT/venv-vllm/bin/vllm" ]; then
    python3 -m venv "$PP_ROOT/venv-vllm"
    "$PP_ROOT/venv-vllm/bin/pip" install "vllm==$PP_VLLM_VERSION"
  fi
  for model in $models; do
    python -c "from huggingface_hub import snapshot_download; snapshot_download('$model')"
  done
else
  if [ ! -x "$PP_ROOT/ollama/bin/ollama" ]; then
    command -v zstd >/dev/null || { echo "zstd is needed to unpack Ollama" >&2; exit 1; }
    mkdir -p "$PP_ROOT/ollama"
    curl -fsSL "https://github.com/ollama/ollama/releases/download/v$PP_OLLAMA_VERSION/ollama-linux-amd64.tar.zst" \
      | zstd -d | tar -x -C "$PP_ROOT/ollama"
  fi
  OLLAMA_HOST=127.0.0.1:11999 "$PP_ROOT/ollama/bin/ollama" serve >"$PP_ROOT/logs/ollama-setup.log" 2>&1 &
  pid=$!
  trap 'kill $pid' EXIT
  until OLLAMA_HOST=127.0.0.1:11999 "$PP_ROOT/ollama/bin/ollama" list >/dev/null 2>&1; do sleep 1; done
  for model in $models; do
    OLLAMA_HOST=127.0.0.1:11999 "$PP_ROOT/ollama/bin/ollama" pull "$model"
  done
fi
echo "Setup done. Submit with: bash ml/slurm/submit.sh"
