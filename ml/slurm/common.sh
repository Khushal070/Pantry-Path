# Shared by the Explorer scripts: load explorer.env and refuse placeholder values.
# shellcheck shell=bash
PP_REPO="${PP_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export PP_REPO
# shellcheck source=explorer.env
source "$PP_REPO/ml/slurm/explorer.env"

pp_require() {
  local missing=()
  for name in "$@"; do
    if [ -z "${!name:-}" ] || [ "${!name}" = "__SET_ME__" ]; then
      missing+=("$name")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "Set ${missing[*]} in ml/slurm/explorer.env first" >&2
    exit 1
  fi
}

pp_python() {
  module load "$PP_PYTHON_MODULE"
  # shellcheck disable=SC1091
  source "$PP_ROOT/venv-ml/bin/activate"
  export HF_HOME="$PP_ROOT/hf" OLLAMA_MODELS="$PP_ROOT/ollama-models"
  export MLFLOW_DISABLE_AGENT_HINT=1
}
