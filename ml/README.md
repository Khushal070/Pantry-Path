# Pantry Path model pipeline

Self-hosted models for the three model seams in [`docs/model-pipeline-plan.md`](../docs/model-pipeline-plan.md): request parsing (**A1**, chat → `RequestInfo`), unseen dishes (**A2**, dish name → dish profile) and the grounded answer (**A3**, `result.json` → what the shopper reads). The data pipeline stays unchanged; `pantrypath_ml` imports its public functions.

One candidate is evaluated this milestone: `qwen3-4b`, the hybrid Qwen3-4B (thinking on or off by switch) at 4-bit or 8-bit. Plan v2 named a second one (Gemma 4 E4B); it was left out because of laptop disk and compute limits (see [`docs/model-pipeline-plan.md`](../docs/model-pipeline-plan.md), section 3). Model selection this milestone is therefore over settings of one model: prompt variant, temperature and quantization. Adding a candidate is one block in `configs/candidates.yaml` (an example is commented there).

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
| `configs/candidates.yaml` | Servers (URL, JSON mode), client settings (temperature, **think**, output caps, context length, timeouts, cache), candidates (size, model id per server and quantization). The only place model ids live. |
| `configs/evalsets.yaml` | A2 popularity tiers; the A1 generator's seed, split and phrase bank; the realistic cases left out for independence; sensitivity paraphrases; the A3 generator |
| `configs/parse_request.yaml` | A1 prompt variants (`base`, `fewshot`, `rules`, `fewshot_rules`, `rules_v2`, the default), few-shot examples, unsupported-need labels, the cross-check's word tables |
| `configs/refine_dish.yaml` | A2 prompt variants: `refine_dish` (the pipeline's prompt) and `refine_dish_unknown` (the default) |
| `configs/answer.yaml` | A3 prompt file, default setting (temperature 0, thinking off; not searched), substitute screen, the warnings added by code |
| `configs/substitutes.yaml` | A3's vetted substitution table (by food family, hand-written; review and extend). Every candidate has a required `contains` list (allergens, `meat`, `animal`), checked against the shopper's avoid list and diet together with the name screen |
| `configs/search.yaml` | The staged local search that was run (`local_prompts`, `local_temperature`, `local_loo`; `local_think` is opt-in) and the `explorer` grid |
| `configs/gates.yaml` | Hard gates, the targets from section 8 of the plan as reported goals, bias gate (blocking for A1, a warning for A2), latency limits, selection score, CI sample |
| `configs/alerts.yaml` | Notification settings (recipients come from secrets) |
| `prompts/parse_request.txt` | The A1 prompt (`parse_request/1`) |
| `prompts/refine_dish_unknown.txt` | A2 prompt for a dish name with no recipe data (`refine_dish_unknown/1`), run through the pipeline's `refine_dish` with the same schema, validation and `build_profile` |
| `prompts/answer.txt` | The A3 prompt, variant `hybrid` (`answer/2`: text around the decided list; the default) |
| `prompts/answer_swaps.txt` | Variant `hybrid_swaps` (`answer/3`): the hybrid plus an explicit "use X instead of Y" list and the rule to write the substitute's name, never the original's (`evaluate_a3 --variant hybrid_swaps`) |
| `prompts/answer_model_list.txt` | The first A3 prompt, variant `model_list` (`answer/1`: the model writes the whole list), kept so its dev run is reproducible (`evaluate_a3 --variant model_list`) |

Environment overrides for one run: `MODEL_SERVER_URL` (base URL without `/v1`), `MODEL_NAME` (the candidate's model id), `MLFLOW_TRACKING_URI`. For the private Cloud Run service: `MODEL_SERVER_TOKEN` (a fixed bearer token; one from `gcloud auth print-identity-token` lasts about an hour, fine for a script) or `MODEL_SERVER_AUTH=google` (the caller mints and refreshes Google ID tokens itself; use this for the backend).

**Thinking.** `client.think` (default `false`) is part of every cache folder name. With `json_mode: ollama_format` the client sends Ollama's `think` option on `/api/chat`; with `response_format` it appends Qwen's `/no_think` to the prompt. Ollama's `/v1` endpoint ignores `/no_think` (tested on Ollama 0.40.15: thinking stayed on), so the `ollama` server uses `ollama_format`; vLLM uses `response_format`. Ollama's plain `qwen3:4b` tag is the thinking-only 2507 build, which ignores the switch, so it is not used.

**Output caps, context and timeouts.** With thinking off, replies are capped per task (`max_output_tokens_no_think`: A1 256 tokens, A2 1536, A3 2048); with thinking on, `max_output_tokens` (8192) applies. `client.context_length` (8192) is sent to Ollama with every request as `num_ctx`, because a plain `ollama serve` uses a short default and silently cuts long prompts. On vLLM the prompt plus `max_output_tokens` must fit `--max-model-len`, which is why `slurm/run_search.sbatch` serves 16384. A read timeout is tried at most twice (`read_timeout_attempts`), since at temperature 0 the same prompt tends to run as long again. The A1 schema caps `avoid` at 10 items (Ollama enforces `maxItems`; it ignores `uniqueItems`, so repeats are dropped when the reply is validated).

**Caches.** The data pipeline's cache key ignores which model answered, so every model + quantization + prompt version + temperature + think (+ JSON mode) gets its own folder under `cache/llm/`. Each evaluation run starts with an empty `runs/<run_id>/` subfolder, so latency is always measured on real calls; cache hits are still counted. A3 in production has no cache at all (below).

## Running each step locally

| Step | Command | Output |
|---|---|---|
| A1 evaluation sets (seeded; already generated) | `python -m pantrypath_ml.evalsets generate-a1` | `evalsets/a1_dev.jsonl`, `a1_test.jsonl`, `a1_review_sample.csv` |
| A1 everyday-phrasing **dev** split from the independent draft | `python -m pantrypath_ml.evalsets convert-a1-csv --csv evalsets/a1_realistic_dev_source.csv` | `evalsets/a1_realistic_dev.jsonl` (`--split realistic_dev`, tuned on). Dishes resolved with the catalog lookup; chats on A1 test dishes, or whose dish doesn't resolve, are left out and listed |
| A3 evaluation sets (seeded; already generated) | `python -m pantrypath_ml.evalsets generate-a3` | `evalsets/a3_dev.jsonl`, `a3_test.jsonl` (commit them; CI checks they are current) |
| A2 evaluation | `python -m pantrypath_ml.evaluate_a2 --candidate qwen3-4b --split dev\|loo [--variant refine_dish] [--limit N]` (`--split loo_full`: every catalog dish, held out, for `select final`'s bias check) | `reports/a2/<run_id>/` + MLflow |
| A1 evaluation | `python -m pantrypath_ml.evaluate_a1 --candidate qwen3-4b --split dev [--variant base] [--limit N]` | `reports/a1/<run_id>/` + MLflow |
| A3 evaluation | `python -m pantrypath_ml.evaluate_a3 --candidate qwen3-4b --split dev [--limit N]` | `reports/a3/<run_id>/` + MLflow |
| Local search (staged: prompts, then temperature 0.2, A2 loo; thinking opt-in) | `python -m pantrypath_ml.search run --grid local_prompts --search-id <id>`, then the same with `local_temperature`, `local_loo` (and `local_think` if wanted) | one run per task, then: |
| Comparison table and charts | `python -m pantrypath_ml.aggregate --search-id <id>` | `reports/search/<id>/` |
| Bias report | `python -m pantrypath_ml.bias --a1 reports/a1/<run> --a2 reports/a2/<run>` | `reports/bias/<runs>/` |
| Rescore A2 runs after a matcher change (saved profiles, no model) | `python -m pantrypath_ml.evaluate_a2 --rescore reports/a2/<run> ...` | `summary.json` rewritten; the old one kept as `summary.before-rescore.json` |
| Rescore A3 runs after a check change (saved replies, no model) | `python -m pantrypath_ml.evaluate_a3 --rescore reports/a3/<run> ...` | the same. Runs save every attempt (`history`: first reply, retry; the repair is `answer`), so a first reply that now passes is kept. Runs saved before that (A3 dev `20261112T224725Z-a3-d56521`) have only each case's last reply, and the rescored summary says so |
| Audit the A3 substitution table against Open Food Facts (not run in CI: needs the snapshot, `dvc pull data-pipeline/data/processed/off_products.parquet.dvc`) | `python -m pantrypath_ml.audit_substitutes [--declared-min 0.10] [--trace-min 0.20] [--min-products 5]` | `reports/audit/<id>/audit.json`, `audit.md`: per substitute, the allergens and diet tags its matching products declare that its `contains` list misses, with example products (read them: dishes made with the substitute can still match), and the substitutes with too few products to audit |
| Sensitivity: perturbations | `python -m pantrypath_ml.sensitivity perturb --candidate qwen3-4b --limit 40 [--only paraphrase ...]` | `reports/sensitivity/<id>/` (with per-case `cases.jsonl`) |
| Sensitivity: setting curves | `python -m pantrypath_ml.sensitivity curves --search-id <id>` | `reports/sensitivity/<id>/` |
| Choose a setting | `python -m pantrypath_ml.select choose --search-id <id> --write-proposed`, or `--runs reports/a1/<run> reports/a2/<run> reports/a3/<run> ...` for existing runs | `reports/selection/<id>/`, `registry/proposed.json` |
| Final confirmation (held-out sets, once) | `python -m pantrypath_ml.select final` | `reports/final/<id>/`; exit 1 on a failed gate |
| Promote | commit `reports/final/<id>/`, then `python -m pantrypath_ml.select promote --final reports/final/<id>/final.json` (or `--latest-full`) | `registry/champion.json` (old one in `champion.previous.json`) |

Common options: `--server ollama|vllm`, `--quantization 4bit|8bit`, `--temperature`, `--think/--no-think`, `--no-mlflow`, `--run-id`. `--limit N` takes a seeded sample spread over phrasing styles, diets and allergens (A1) or cuisines (A2, A3), not the first N items, which would all come from the first few dishes. Every command prints a one-line JSON summary on stdout; logs go to stderr.

**The held-out splits** (`data/golden/recipes_heldout.json`, `evalsets/a1_test.jsonl`, `evalsets/a1_realistic.jsonl`, `evalsets/a3_test.jsonl`) are read only when asked for by name (`--split test`, `--split realistic`, or `select final`). Each read is logged as a warning and tagged in MLflow. Use them for gates and final results only, never for tuning. CI never reads them. The one exception is the leave-one-out split: it reads the **dish names** of the golden test recipes, only to leave those dishes out, so search and selection never see a test dish. `loo_full` (every catalog dish, the test dishes too) is held out like `test`: only `select final`'s A2 bias check uses it.

**`realistic`** is 45 hand-written chats in everyday phrasing ("my son can't handle dairy", "sesame makes me break out in hives", an allergy list typed after the assistant asks). The generated A1 sets share their wording with the cross-check's word lists, so the cross-check is close to 100% on them by construction. Four realistic chats are **removed for independence** (`evalsets.yaml` `a1.realistic_excluded`: `a1r-banana-bread-0`, `a1r-butter-chicken-0`, `a1r-spinach-artichoke-dip-0`, `a1r-vegan-pancake-0`): they paraphrase the review examples the cross-check was fixed on ("cashews especially", "nut allergy", "lactose intolerant"). They stay in the file and in the unit tests, but not in the realistic metrics or gate, which use the other 41. Teammates should add cases in their own words (same format), never by copying wording from `configs/parse_request.yaml`.

**`select final`** runs, on the deployed server type (Ollama): A1 on `test` and `realistic`, A2 on `test` and A3 on `test`. The A2 bias gap comes from the proposed setting's **full** leave-one-out run over all 100 catalog dishes (`a2.final_bias_run` in `proposed.json`: `select choose` records it when a `--split loo_full` run of the chosen A2 setting, or an older `loo` run made before loo left the test dishes out, is in `--runs`; it is never used to choose), since the 27-dish test set has only one cuisine with 10 dishes. Search and choose use the loo run without the test dishes (`a2.bias_run`). Rescore an older full run with the current matcher first (`evaluate_a2 --rescore`). A gap that can't be measured is reported as **insufficient data**, never as a pass: it fails A1 in `select final` (`gates.yaml` `bias.on_insufficient`) and is a warning elsewhere and for A2.

**Selection.** Settings that pass the hard gates (A1 allergens captured 100%, diet captured 100%, unsupported needs flagged 100%, valid JSON ≥ 99%; A2 valid profile ≥ 99%; A3 final answer clean 100%, valid JSON ≥ 95%, repaired at most 50%) and, for A1, the bias gate on dev (for A2 a gap over the limit is recorded as a warning with its reason: small cuisine groups, and leave-one-out references are catalog entries, not ground truth); per model and quantization, the best A1 and the best A2 setting (A1 and A2 may use different prompts, temperature and think settings on the same server); the smallest model wins, then the best score in `gates.yaml`. A3 is not searched: an A3 dev run in `--runs` must pass its hard gates and is recorded as `a3.dev_run`; without one, `configs/answer.yaml` defaults are used. `select choose --exclude-a3` proposes A1 and A2 only: A3 is recorded as "evaluated, not promoted" (`a3.status: evaluated_not_promoted`, with its dev run, dev results and failing gates), `select final` doesn't run A3 on its test split and says so in `final.md`, and `runtime.load()` builds no A3 client (`models.a3_available` is False; the app shows `result.json` without an A3 answer). The plan's other targets are reported as goals, met or not. A champion is replaced only by a setting that is no worse on every metric without a hard gate (the goals and the score metrics) and better on the score. It is never replaced by one it can't be compared with.

**What is committed from `reports/`.** Only the `reports/final/<id>/` folder of the run being promoted: `select promote` refuses a final report that isn't committed and unchanged in Git, and CI promotes from it (`--latest-full`). Every other report folder (`a1/`, `a2/`, `a3/`, `bias/`, `search/`, `selection/`, `sensitivity/`, `replay/` and the other `final/` runs) is ignored (`.gitignore`) and stays on the machine that made it; share them as a zip when a teammate needs them. Never commit `mlruns/` or `cache/`.

## A3: the grounded answer

A3 turns `result.json` (plus A1's extras) into what the shopper reads: a shopping list where each ingredient is `buy` (one of its **safe** products), `substitute`, `check_label` (only unverified products) or `skip` (a common ingredient), with quantities for the servings, 3 to 8 steps and a summary. Same model, its own prompt (`prompts/answer.txt`) and settings (`configs/answer.yaml`: temperature 0, thinking off; neither was searched this milestone).

**A hybrid** (`answer/2`). Code decides the list (`answer.decide`): a safe product is bought at the nearest store that has one; an ingredient that itself conflicts with the avoid list or diet (butter for a milk-avoider) gets the first substitute from `configs/substitutes.yaml` that passes the screen, else is skipped (common) or gets an open substitute note (core); any other ingredient without a safe product gets a label check when some products are unverified, then a table substitute, then skip or the open note. The model writes only the summary, steps and quantities around that fixed list, and the prompt tells it to mention only the given items. The first version let the model write the whole list (`answer/1`): on dev it needed the repair in 93% of answers (wrong statuses, unsafe substitutes), which is why the list moved to code.

The model never sets a verdict, and the prompt shows it only the names of safe products. After it, `answer.check` holds the reply to `result.json`:
- every ingredient once, none invented;
- `buy` only with a product `result.json` marked safe for that ingredient, at a store that sells it;
- no product that isn't safe named anywhere in the text;
- every substitute passes a word screen for the avoid list and diet;
- the summary, steps and notes name no avoided allergen and nothing the diet excludes. Free to name: the dish as a whole ("chicken curry", not "chicken"), an ingredient the answer **buys**, and a substitute. An ingredient that is substituted, label-checked or skipped may be named only inside a substitution or avoid phrase ("instead of", "replace", "swap", "without", "no", "skip", and lists after them: "without milk, cheese or bacon"). So "Stir in the heavy cream." fails for a milk-avoider when heavy cream is a substitute and passes when it is bought; "Replace the heavy cream with coconut milk." passes.

`diet_rules` judges products (label evidence), not ingredient names, so substitutes and steps get this word screen instead ("almond milk" fails for tree nuts, "coconut milk" passes for milk). One retry shows the model what broke; anything still broken is repaired, so the answer that is shown always passes the check. Warnings that must always appear (unverified products, estimated quantities, needs A1 couldn't check, the nuts question, the Open Food Facts attribution) are added by code.

The evaluation sets hold one synthetic `result.json` per catalog dish: synthetic stores and products (never Kroger data), the same test dishes as A1, each validated against `pantrypath_core.result.Result`. Metrics are scored on the model's reply **before** repair; `final_clean_rate` and valid JSON are hard gates, verdict, substitute and text safety, coverage and missing-ingredient handling are goals (`gates.yaml`).

In production, A3's client has **no reply cache**: its prompt holds Kroger product names, and Kroger's terms forbid keeping them.

## Running everything on a laptop (no Explorer)

Ollama is enough for the whole milestone; Explorer only makes the search wider. Times measured on the development laptop (Apple Silicon, Qwen3-4B 4-bit, thinking off): about 2.5 s per A1 chat and 12 to 15 s per A2 dish. Time your first 5 cases to check yours.

| Step | Command | Time |
|---|---|---|
| A1 dev (420 chats) | `python -m pantrypath_ml.evaluate_a1 --candidate qwen3-4b --split dev` | about 20 min |
| A2 dev (16 dishes) / leave-one-out | `python -m pantrypath_ml.evaluate_a2 --candidate qwen3-4b --split dev` / `--split loo` | about 4 / 20 min |
| A3 dev (70 results) | `python -m pantrypath_ml.evaluate_a3 --candidate qwen3-4b --split dev` | see the latest run's summary |
| Perturbation test | `python -m pantrypath_ml.sensitivity perturb --candidate qwen3-4b --limit 40` | about 8 min |
| Final confirmation | `python -m pantrypath_ml.select final` | A1 test and realistic, A2 test, A3 test |
| Combined final, or restamp | `python -m pantrypath_ml.select final --reuse-final reports/final/<id>/final.json` | Reuses that final's A1, A1 realistic and A2 runs, and its A3 run if it has one; runs only what is missing (A3 test for a final without A3). Refused unless every reused task has the proposed model, quantization, prompt variant, temperature and thinking, the same A2 bias run and the prompt versions the code has now. Gates are applied again and the new report is stamped with the current commit, so after a merge, reusing a combined final restamps it without running any model. `--rerun a1` (or `a2`, `a3`) runs that task again and reuses the rest, e.g. after an A1 cross-check fix |

Run them one at a time (parallel runs compete for the same model and distort latency), keep the machine awake, and say in the report which grid was run.

## Latency

`gates.yaml` targets 3 s per A1 request and 15 s per A2 dish on the deployed server. On Cloud Run's CPUs these are unlikely to hold for a 4B model. Measure before deciding, with the same limits as `cloudrun.yaml`:

```bash
docker build --build-arg MODEL=qwen3:4b-q4_K_M -t pp-model ml/serving
docker run --rm --cpus 4 --memory 8g -p 8080:8080 pp-model
MODEL_SERVER_URL=http://localhost:8080 python ml/serving/latency_check.py   # needs a champion
```

Then choose one and write the reason in the report: more CPUs (Cloud Run allows up to 8), a Cloud Run GPU, or revised targets. CI's post-deploy check (`--smoke`) reports latency but only fails on unusable replies.

## Explorer (Slurm)

1. Fill in every `__SET_ME__` in `slurm/explorer.env`: partition, GPU `--gres`, time limits, Python module, shared storage path. Nothing there is guessed; the scripts refuse to run until it is filled in. Ask for an A100-class GPU or newer (vLLM's AWQ kernels need compute capability 7.5+).
2. On a **login node** (compute nodes may have no internet): `bash ml/slurm/setup_env.sh`. This creates the environments, installs vLLM 0.32.0 (or Ollama 0.40.15 with `PP_SERVER=ollama`), downloads every model in the explorer grid and pulls the catalog.
3. Submit: `bash ml/slurm/submit.sh [SEARCH_ID]`. This submits one array task per grid task (`run_search.sbatch`: start the server on the task's GPU, wait until it is ready, run that A1 or A2 evaluation, stop the server), plus `aggregate.sbatch` with `--dependency=afterok`.
4. Monitor: `squeue -u $USER`, `sacct -j <array job> --format JobID,State,Elapsed`. Server and job logs are in `$PP_ROOT/logs/`. If a task fails, `afterok` holds the aggregate job back: rerun that task with `sbatch --array=<i> ...` (same `SEARCH_ID`), then run `python -m pantrypath_ml.aggregate --search-id <id> --log-mlflow` by hand.
5. MLflow: array tasks run with `--no-mlflow` (many writers on one SQLite file over a network filesystem is unsafe). The aggregate job logs every run into `ml/mlruns/` from one process. To look at the runs on a laptop, copy the reports back (`rsync -av explorer:<repo>/ml/reports/ ml/reports/`) and run `python -m pantrypath_ml.aggregate --search-id <id> --log-mlflow` there. Or copy `ml/mlruns/` itself and run `mlflow ui --backend-store-uri sqlite:///mlruns/mlflow.db`.

The search may pick a setting on vLLM (AWQ or FP8 weights), but `select final` runs on Ollama (GGUF), which is what gets deployed. The confirmed numbers are the deployed model's.

## Deployment, CI/CD and rollback

- **GCP setup, once**: `GCP_PROJECT_ID=... GCP_REGION=... GITHUB_REPO=owner/name bash infra/gcp/bootstrap.sh`. It enables the APIs, creates both Artifact Registry repositories, the runtime and CI service accounts with their roles, and workload identity federation (restricted to that repository), then prints the GitHub variables and secrets to set.
- **Model server image** (`serving/Dockerfile`): Ollama 0.40.15 with the champion's model baked in, on port 8080. `serving/cloudrun.yaml` gives it 4 CPUs, 8 GiB, concurrency 1, and lets it scale to zero (private: callers need an identity token). The first request after a cold start also loads the model.
- **Using the models from the backend** (`pantrypath_ml.runtime`): `models = runtime.load()` reads `registry/champion.json` and returns the A1 parser (`models.parse(messages)`: model, cross-check and ZIP check), the A2 client and prompt to pass to the data pipeline (`build_result(..., llm_client=models.a2_client, llm_template=models.a2_template)`) and A3 (`models.answer(result_json, parsed)`: the checked answer plus warnings to show), each with the champion's own prompt, temperature and thinking. Without `llm_template`, `build_result()` would use the pipeline's own `refine_dish` prompt, which is not the champion's A2 prompt. Set `MODEL_SERVER_URL` to the Cloud Run URL and `MODEL_SERVER_AUTH=google`. A dish query the catalog doesn't know is retried without leading diet words and trailing serving forms (`parse_request.yaml` `query_cleanup`: "vegan sugar cookies" → "sugar cookies"), and the cleaned one is used only if the catalog knows it (noted in `notes`). A `parse` result is `ok`, `needs_confirmation` (show `conflicts` and `suggested`; "nut allergy": the suggestion avoids both tree nuts and peanuts, to confirm), `incomplete` (ask for `missing`) or `invalid`.
- **Evaluation runner image** (`Dockerfile`, built from the repo root): `docker build -f ml/Dockerfile -t pantrypath-ml .`
- **Registry** (`python -m pantrypath_ml.registry push-image --tag <tag> | push-champion | pull-champion`): the image goes to an Artifact Registry Docker repository, and the champion record to a generic repository, one version per promotion.
- **Deploy**: `IMAGE=<uri> bash serving/deploy.sh`. Latency check against the deployed URL: `MODEL_SERVER_URL=<url> MODEL_SERVER_TOKEN="$(gcloud auth print-identity-token)" python serving/latency_check.py`.
- **CI** (`.github/workflows/model-pipeline.yml`, on changes to `ml/`, `shared/`, the data pipeline's code, prompts and config, and `data/golden/`):
  1. Unit tests and ruff; the committed A3 sets must match `generate-a3`.
  2. The proposed setting (`registry/proposed.json`) on a seeded sample of the A1, A2 and A3 **dev** sets, on the runner's CPU with Ollama. CI never reads the held-out sets.
  3. Hard gates and the A1 bias gate; the job fails on a breach (an A2 bias gap, or one CI's small sample can't measure, is reported as a warning).
  4. Comparison with the champion (pulled from Artifact Registry on main) using the newest **committed** passing full final report for the proposed setting. Without one, nothing is promoted: run `select final` and commit `reports/final/<id>/`. A CI result is never promoted.
  5. On main, if promoted: image push, champion push, new Cloud Run revision, then a smoke test of the new revision (`latency_check.py --smoke`). If the smoke test fails, `rollback.sh` runs automatically.
  6. Email and Slack notifications on failure, failed gates and completion.
- **Rollback**: `bash serving/rollback.sh` (or `REVISION=<name> bash serving/rollback.sh`). It sends all traffic back to the previous Cloud Run revision, restores the previous champion record (`registry/champion.previous.json`, or the second-newest version in Artifact Registry) and uploads it as the newest version.

## What you must configure

| Where | Names |
|---|---|
| GitHub repository **variables** | `GCP_PROJECT_ID`, `GCP_REGION`, `AR_DOCKER_REPO`, `AR_GENERIC_REPO`, `CLOUD_RUN_SERVICE`, `RUN_SERVICE_ACCOUNT` (the release job is skipped until these are set) |
| GitHub repository **secrets** | `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT` (workload identity federation), `ALERT_EMAIL_TO`, `ALERT_SMTP_USER`, `ALERT_SMTP_PASSWORD`, `SLACK_WEBHOOK_URL` (each optional; without them notifications are only logged) |
| GCP | Everything is created by `infra/gcp/bootstrap.sh`: two Artifact Registry repositories (Docker and generic), a Cloud Run runtime service account, and a CI service account with Artifact Registry Writer, Cloud Run Admin, Cloud Run Invoker (for the smoke test) and Service Account User on the runtime account |
| `slurm/explorer.env` | Every `__SET_ME__` |

## Tests

```bash
python -m pytest -q
ruff check src tests serving
```

The tests use fake HTTP servers, fake model clients, a fake six-dish catalog, a temporary MLflow store and fake `docker`/`gcloud` runners. They need no network, model server, key or GCP project, and never run a model on the real held-out sets (one test checks the labels of `a1_realistic.jsonl`).

**MLflow** tracks experiments (every run's settings, metrics and report folder). It is not the registry: the champion itself is recorded in `registry/champion.json` and in Artifact Registry (`registry.py`), one version per promotion. By default each machine has its own store in `ml/mlruns/`; set `MLFLOW_TRACKING_URI` to a shared server for one team view.
