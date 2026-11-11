# Pantry Path model pipeline

Self-hosted models for the two model seams in [`docs/model-pipeline-plan.md`](../docs/model-pipeline-plan.md): request parsing (**A1**, chat → `RequestInfo`) and unseen dishes (**A2**, dish name → dish profile). The data pipeline stays unchanged; `pantrypath_ml` imports its public functions.

Only one candidate is configured for now: `qwen3-4b`, the hybrid Qwen3-4B (thinking on or off by switch) at 4-bit or 8-bit.

## Setup

Python 3.12, from the repo root:

```bash
cd ml
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -e ../shared -e ../data-pipeline -e .
# the dish catalog (public DVC remote, no credentials)
(cd .. && dvc pull data-pipeline/data/processed/dish_profiles.json.dvc \
                   data-pipeline/data/processed/pantrypath.sqlite.dvc)
ollama pull qwen3:4b-q4_K_M        # a local model server (tests don't need one)
```

`requirements.txt` holds the data pipeline's dependencies plus MLflow and Fairlearn, at the data pipeline's exact versions wherever the two overlap. Edit `requirements.in`, then regenerate it with the command at the top of that file.

## Configuration

| File | What it sets |
|---|---|
| `configs/candidates.yaml` | Servers (URL, JSON mode), client settings (temperature, **think**, output caps, timeouts, cache), candidates (size, model id per server and quantization). The only place model ids live. |
| `configs/evalsets.yaml` | A2 popularity tiers; the A1 generator's seed, split and phrase bank; sensitivity paraphrases |
| `configs/parse_request.yaml` | A1 prompt variants (`base`, `fewshot`, `rules`, `fewshot_rules`, `rules_v2`), few-shot examples, unsupported-need labels, the cross-check's synonym tables |
| `configs/refine_dish.yaml` | A2 prompt variants: `refine_dish` (the pipeline's prompt, the default) and `refine_dish_unknown` |
| `configs/search.yaml` | The `local` and `explorer` search grids |
| `configs/gates.yaml` | Targets from section 7 of the plan, bias gate, latency limits, selection score, CI subset size |
| `configs/alerts.yaml` | Notification settings (recipients come from secrets) |
| `prompts/parse_request.txt` | The A1 prompt (`parse_request/1`); A2's default variant reuses `data-pipeline/prompts/refine_dish.txt` unchanged |
| `prompts/refine_dish_unknown.txt` | A2 prompt for a dish name with no recipe data (`refine_dish_unknown/1`), run through the pipeline's `refine_dish` with the same schema, validation and `build_profile` |

Environment overrides for one run: `MODEL_SERVER_URL` (base URL without `/v1`), `MODEL_NAME` (the candidate's model id), `MODEL_SERVER_TOKEN` (bearer token for a private Cloud Run service), `MLFLOW_TRACKING_URI`.

**Thinking.** `client.think` (default `false`) is part of every cache folder name. With `json_mode: ollama_format` the client sends Ollama's `think` option on `/api/chat`; with `response_format` it appends Qwen's `/no_think` to the prompt. Ollama's `/v1` endpoint ignores `/no_think` (tested on Ollama 0.40.15: thinking stayed on), so the `ollama` server uses `ollama_format`; vLLM uses `response_format`. Ollama's plain `qwen3:4b` tag is the thinking-only 2507 build, which ignores the switch, so it is not used.

**Output caps and timeouts.** With thinking off, replies are capped per task (`max_output_tokens_no_think`: A1 256 tokens, A2 1536); with thinking on, `max_output_tokens` (8192) applies. A read timeout is tried at most twice (`read_timeout_attempts`), since at temperature 0 the same prompt tends to run as long again. The A1 schema caps `avoid` at 10 items (Ollama enforces `maxItems`; it ignores `uniqueItems`, so repeats are dropped when the reply is validated).

**Caches.** The data pipeline's cache key ignores which model answered, so every model + quantization + prompt version + temperature + think (+ JSON mode) gets its own folder under `cache/llm/`. Each evaluation run starts with an empty `runs/<run_id>/` subfolder, so latency is always measured on real calls; cache hits are still counted.

## Running each step locally

| Step | Command | Output |
|---|---|---|
| A1 evaluation sets (seeded; already generated) | `python -m pantrypath_ml.evalsets generate-a1` | `evalsets/a1_dev.jsonl`, `a1_test.jsonl`, `a1_review_sample.csv` |
| A2 evaluation | `python -m pantrypath_ml.evaluate_a2 --candidate qwen3-4b --split dev [--variant refine_dish_unknown] [--limit N]` | `reports/a2/<run_id>/` + MLflow |
| A1 evaluation | `python -m pantrypath_ml.evaluate_a1 --candidate qwen3-4b --split dev [--variant fewshot] [--limit N]` | `reports/a1/<run_id>/` + MLflow |
| Local search (2 prompts × 2 temperatures × think on/off) | `python -m pantrypath_ml.search run --grid local` | one run per task, then: |
| Comparison table and charts | `python -m pantrypath_ml.aggregate --search-id <id>` | `reports/search/<id>/` |
| Bias report | `python -m pantrypath_ml.bias --a1 reports/a1/<run> --a2 reports/a2/<run>` | `reports/bias/<runs>/` |
| Sensitivity: perturbations | `python -m pantrypath_ml.sensitivity perturb --candidate qwen3-4b --limit 40` | `reports/sensitivity/<id>/` |
| Sensitivity: setting curves | `python -m pantrypath_ml.sensitivity curves --search-id <id>` | `reports/sensitivity/<id>/` |
| Choose a setting | `python -m pantrypath_ml.select choose --search-id <id> --write-proposed` | `reports/selection/<id>/`, `registry/proposed.json` |
| Final confirmation (**test** sets, once) | `python -m pantrypath_ml.select final` | `reports/final/<id>/`; exit 1 on a failed gate |
| Promote | `python -m pantrypath_ml.select promote --final reports/final/<id>/final.json` | `registry/champion.json` (old one in `champion.previous.json`) |

Common options: `--server ollama|vllm`, `--quantization 4bit|8bit`, `--temperature`, `--think/--no-think`, `--no-mlflow`, `--run-id`. Every command prints a one-line JSON summary on stdout; logs go to stderr.

**The test splits** (`data/golden/recipes_heldout.json`, `evalsets/a1_test.jsonl`) are read only when asked for by name (`--split test`, or `select final`). Each read is logged as a warning and tagged `eval_split=test` in MLflow. Use them for gates and final results only, never for tuning.

**Selection.** Settings that pass the validation and bias gates on dev; per model and quantization, the best A1 and the best A2 setting (A1 and A2 may use different temperature and think settings on the same server); the smallest model wins, then the best score in `gates.yaml`. A champion is replaced only by a setting that is no worse on any gated metric and better on the score. It is never replaced by one it can't be compared with.

## Explorer (Slurm)

1. Fill in every `__SET_ME__` in `slurm/explorer.env`: partition, GPU `--gres`, time limits, Python module, shared storage path. Nothing there is guessed; the scripts refuse to run until it is filled in.
2. On a **login node** (compute nodes may have no internet): `bash ml/slurm/setup_env.sh`. This creates the environments, installs vLLM 0.32.0 (or Ollama 0.40.11 with `PP_SERVER=ollama`), downloads every model in the explorer grid and pulls the catalog.
3. Submit: `bash ml/slurm/submit.sh [SEARCH_ID]`. This submits one array task per grid task (`run_search.sbatch`: start the server on the task's GPU, wait until it is ready, run that A1 or A2 evaluation, stop the server), plus `aggregate.sbatch` with `--dependency=afterok`.
4. Monitor: `squeue -u $USER`, `sacct -j <array job> --format JobID,State,Elapsed`. Server and job logs are in `$PP_ROOT/logs/`. If a task fails, `afterok` holds the aggregate job back: rerun that task with `sbatch --array=<i> ...` (same `SEARCH_ID`), then run `python -m pantrypath_ml.aggregate --search-id <id> --log-mlflow` by hand.
5. MLflow: array tasks run with `--no-mlflow` (many writers on one SQLite file over a network filesystem is unsafe). The aggregate job logs every run into `ml/mlruns/` from one process. To look at the runs on a laptop, copy the reports back (`rsync -av explorer:<repo>/ml/reports/ ml/reports/`) and run `python -m pantrypath_ml.aggregate --search-id <id> --log-mlflow` there. Or copy `ml/mlruns/` itself and run `mlflow ui --backend-store-uri sqlite:///mlruns/mlflow.db`.

The search may pick a setting on vLLM (AWQ or FP8 weights), but `select final` runs on Ollama (GGUF), which is what gets deployed. The confirmed numbers are the deployed model's.

## Deployment, CI/CD and rollback

- **Model server image** (`serving/Dockerfile`): Ollama 0.40.11 with the champion's model baked in, on port 8080. `serving/cloudrun.yaml` gives it 4 CPUs, 8 GiB, concurrency 1, and lets it scale to zero (private: callers need an identity token).
- **Evaluation runner image** (`Dockerfile`, built from the repo root): `docker build -f ml/Dockerfile -t pantrypath-ml .`
- **Registry** (`python -m pantrypath_ml.registry push-image --tag <tag> | push-champion | pull-champion`): the image goes to an Artifact Registry Docker repository, and the champion record to a generic repository, one version per promotion.
- **Deploy**: `IMAGE=<uri> bash serving/deploy.sh`. Latency check against the deployed URL: `MODEL_SERVER_URL=<url> MODEL_SERVER_TOKEN="$(gcloud auth print-identity-token)" python serving/latency_check.py`.
- **CI** (`.github/workflows/model-pipeline.yml`, on changes to `ml/`):
  1. Unit tests.
  2. The proposed setting (`registry/proposed.json`) on a small slice of the A1 and A2 test sets, on the runner's CPU with Ollama.
  3. Validation and bias gates; the job fails on a breach.
  4. Comparison with the champion, pulled from Artifact Registry on main.
  5. On main, if promoted: image push, champion push, new Cloud Run revision.
  6. Email and Slack notifications on failure, failed gates and completion.
- **Rollback**: `bash serving/rollback.sh` (or `REVISION=<name> bash serving/rollback.sh`). It sends all traffic back to the previous Cloud Run revision, restores the previous champion record (`registry/champion.previous.json`, or the second-newest version in Artifact Registry) and uploads it as the newest version.

## What you must configure

| Where | Names |
|---|---|
| GitHub repository **variables** | `GCP_PROJECT_ID`, `GCP_REGION`, `AR_DOCKER_REPO`, `AR_GENERIC_REPO`, `CLOUD_RUN_SERVICE`, `RUN_SERVICE_ACCOUNT` (the release job is skipped until these are set) |
| GitHub repository **secrets** | `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT` (workload identity federation), `ALERT_EMAIL_TO`, `ALERT_SMTP_USER`, `ALERT_SMTP_PASSWORD`, `SLACK_WEBHOOK_URL` (each optional; without them notifications are only logged) |
| GCP | Two Artifact Registry repositories (Docker and generic) and a Cloud Run runtime service account. The CI service account needs Artifact Registry Writer, Cloud Run Admin and Service Account User on the runtime account. |
| `slurm/explorer.env` | Every `__SET_ME__` |

## Tests

```bash
python -m pytest -q
ruff check src tests serving
```

The tests use fake HTTP servers, fake model clients, a fake six-dish catalog, a temporary MLflow store and fake `docker`/`gcloud` runners. They need no network, model server, key or GCP project, and never read the real held-out sets.
