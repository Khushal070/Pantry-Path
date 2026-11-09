# Model Pipeline and Deployment Plan

**Version 2** (November 7, 2026). Replaces the November 1 draft after a full review of the data pipeline code. Targets and owners are proposals until the team agrees.

**What changed from version 1:** the main model now covers request parsing (A1) and unseen dishes (A2), built on code that already exists; no fine-tuning this milestone; the answer model (A3) and the risk classifier (B) move to the next milestone; evaluation sets avoid storing Kroger data; targets use the pipeline's existing metrics.

---

## 1. Ground rules (from `docs/data-pipeline-handoff.md`)

1. **Only `diet_rules.assess` sets verdicts.** No model, before or after `result.json`, may set or flip a verdict. A model may only make results more cautious.
2. **Unknown is never safe.**
3. **No stored Kroger data:** no evaluation sets, logs or caches containing Kroger product payloads.
4. **No fine-tuning on LLM-written content** (the 254 cached catalog replies) until the provider's terms are checked. Evaluation use is fine.
5. **Change the `result.json` contract only through `schema_version`.**

A model that runs before `result.json` is still safety-critical: if it drops an allergen from the request, every verdict is correct for the wrong question.

---

## 2. Where the models plug in

```
chat ──A1──► RequestInfo(query, zip, avoid, diet)   (+ extras such as servings, kept aside)
                │
                ▼
        build_result(cfg, query, zip, avoid, diet, llm_client=A2 client)
           ├─ resolve_dish: catalog (exact, alias, fuzzy ≥ 90) ── miss ──► A2 ──► DishProfile(source="llm")
           ├─ gather_products (Kroger)
           ├─ off_evidence (Open Food Facts snapshot)
           └─ judge(): pick_evidence → diet_rules.assess  [B, next milestone: may only turn safe → unknown]
                │
                ▼
            result.json v1.0
                │
                ▼
        A3 (next milestone): grounded answer; explains and suggests substitutes, never changes a verdict
```

| Model | Milestone | Input | Output | Seam |
|---|---|---|---|---|
| **A1** request parsing | This one | Chat messages | A JSON object valid as `pantrypath_core.result.RequestInfo`, plus an `extras` object for anything the contract can't hold | Backend, before `build_result()` |
| **A2** unseen dishes | This one | Dish name | The existing `REFINE_SCHEMA` reply (core **and** common ingredients, at least 3 with 1 core) | `build_result(..., llm_client=...)` through `refine.profile_for_unknown_dish` |
| **A3** answer | Next | `result.json` + A1 extras | Answer grounded in `result.json` | Backend, after `build_result()` |
| **B** risk classifier | Next | The chosen `ProductEvidence` + avoid/diet | Per-allergen and diet risk, applied only to `safe` | `request.judge()`, after `assess()` |

---

## 3. The model

- **Candidates:** Qwen3-4B and Gemma 4 E4B (instruction-tuned, permissive licenses). Qwen3-8B only if both miss the targets.
- **Selection rule:** the smallest model that passes every gate.
- **Serving:** an OpenAI-compatible server (Ollama or llama.cpp on CPU; vLLM on Explorer for evaluation throughput), with schema-constrained JSON output.
- **Client:** a new `LocalLLMClient` implementing the existing `LLMClient` protocol (`complete_json(prompt, schema, *, purpose) -> LLMReply`), with a configurable base URL, no API key, and token and latency logging through the existing usage reporting.
- **Single source of truth:** the model id lives in configuration only (the repo currently names three different models across `.env.example` and the docs).
- **Cache trap:** the existing cache key ignores which model answered, so each candidate gets its **own cache directory**. Otherwise a new model silently replays Claude's answers.

---

## 4. A2: unseen dishes

**Task:** given only a dish name, produce the catalog-format profile, reusing `prompts/refine_dish.txt` (version `refine_dish/1`), `REFINE_SCHEMA`, `RefineReply`, validation and the one-retry logic in `refine.py`.

**Evaluation sets**

| Set | Contents | Use |
|---|---|---|
| Golden dev | `data/golden/recipes.json` (16 reference recipes) | Prompt and setting choices |
| Golden test | `data/golden/recipes_heldout.json` (27 reference recipes) | **Gates and final results only**; never used for tuning |
| Leave-one-out catalog | Each of the 100 catalog dishes asked by name only, compared with its catalog entry | Broader coverage and bias slicing; catalog entries are LLM-written, so this is a consistency check, not ground truth |

**Metrics:** valid-profile rate (passes `DishProfile` validation), ingredient recall and core precision with the pipeline's `evaluate.Matcher` (same synonyms, skip list and modifiers as the catalog evaluation), "not a dish" false-refusal rate, and latency per dish.

---

## 5. A1: request parsing

**Task:** chat messages → `RequestInfo` (`query`, `zip`, `avoid` from the 10 allergens, `diet` from none, vegetarian, vegan) plus `extras` (servings, tweaks such as "crustless", unsupported needs).

**Safety design**
- **Deterministic cross-check:** a keyword scan of the chat for allergen and diet words, including synonyms ("dairy" → milk, "celiac" → gluten, "nuts" → tree_nuts, with peanuts handled explicitly). If the model's `avoid` misses anything the scan found, the request is refused or sent back for confirmation.
- **Unsupported needs are never dropped:** halal, kosher, low-sodium, diabetic, pregnancy and other diets the rules can't check go to `extras.unsupported`, and the app tells the user plainly.
- **Negations handled:** "no nuts" means avoid nuts; "nuts are fine" means don't.

**Evaluation set (synthetic, reviewed):** generated from catalog dish names and aliases, an allergen and diet phrase bank, negations, typos, casual and formal phrasing, multi-turn corrections and unsupported diets. Split by dish into dev and test, so test dishes never appear in tuning.

**Metrics:** allergen-set exact match, diet accuracy, dish resolution (does `find_dish` reach the intended catalog dish), ZIP accuracy, valid-JSON rate, unsupported-need detection, latency.

---

## 6. Selection, tuning, bias and sensitivity

- **Search space:** model × prompt variant (base prompt, few-shot examples, extra rules) × temperature (0, 0.2, 0.5) × quantization (4-bit, 8-bit).
- **Runs:** Slurm job arrays on Explorer, one task per combination, logged to MLflow (settings, metrics, model and prompt versions).
- **Bias slicing (Fairlearn `MetricFrame`):** A2 by cuisine and dish popularity (note the known cuisine-label quirks, such as Caesar salad labelled Latin American); A1 by allergen, diet, and phrasing type (negation, synonym, typo, multi-turn).
- **Mitigation:** targeted few-shot examples and prompt rules for weak slices; synonym additions for the cross-check; documented trade-offs.
- **Sensitivity:** A1 perturbation tests (paraphrase, typo, allergy mentioned late); temperature, quantization and few-shot-count curves for both tasks. SHAP comes with the classifier next milestone.
- **Selection after bias checks:** best score among combinations that pass both the validation and bias gates.

---

## 7. Targets

| Metric | Target |
|---|---|
| A1 allergens and diet captured (test set) | 100%, with the cross-check as a backstop |
| A1 unsupported needs flagged | 100% |
| A1 dish resolves to the intended catalog dish | ≥ 95% |
| A1 valid JSON | ≥ 99% |
| A2 valid profile | ≥ 99% |
| A2 ingredient recall on the golden test set | ≥ 0.75 (the bar the pipeline already uses for the catalog) |
| A2 core precision on the golden test set | ≥ 0.85 |
| Largest gap between slices | ≤ 10 percentage points on the main metric |
| A1 latency on the deployed server | ≤ 3 s per request |
| A2 latency on the deployed server | ≤ 15 s per dish (catalog misses only) |

---

## 8. Registry, CI/CD and deployment

- **Tracking:** MLflow (added to the requirements); every run records model, quantization, prompt version and metrics.
- **Registry:** the chosen configuration (model id, quantization, prompt versions, settings) and the model-server image go to GCP Artifact Registry.
- **CI/CD (GitHub Actions on changes to `ml/`):** unit tests → evaluate the small model on a held-out subset on the runner's CPU → validation gate → bias gate → compare with the current champion → push to the registry → notify; a worse model is never promoted, and the previous Cloud Run revision stays available for rollback.
- **Deployment:** the model server on Cloud Run (CPU, scales to zero), exposing the OpenAI-compatible endpoint that `LocalLLMClient` calls. The backend that calls `build_result()` is still empty and belongs to the backend milestone; it will use this server for A1 and A2.

---

## 9. Repository layout

```
ml/
  README.md
  Dockerfile                 evaluation runner
  serving/                   model-server image and Cloud Run config
  configs/                   candidates.yaml, search.yaml, gates.yaml
  prompts/                   parse_request (A1); A2 reuses data-pipeline/prompts/refine_dish.txt
  evalsets/                  A1 dev and test sets (generated, reviewed)
  src/pantrypath_ml/
    local_llm.py             LocalLLMClient (LLMClient protocol), per-model cache
    parse_request.py         A1 prompt, schema, cross-check
    evalsets.py              A1 generator; A2 golden and leave-one-out sets
    evaluate_a1.py           A1 metrics
    evaluate_a2.py           A2 metrics (reuses evaluate.Matcher)
    bias.py                  Fairlearn slicing
    sensitivity.py           perturbations and setting curves
    select.py                gates, champion comparison
    registry.py              MLflow logging, Artifact Registry push
  slurm/                     Explorer job-array scripts
  tests/
  reports/
.github/workflows/model-pipeline.yml
```

The data pipeline stays unchanged; `ml/` imports its public functions.

---

## 10. Schedule and split (two people)

| Dates | Person 1: models and evaluation | Person 2: infrastructure |
|---|---|---|
| Nov 7–8 | A2 harness on golden dev; first baseline | `LocalLLMClient`, MLflow, `ml/` skeleton, Explorer environment |
| Nov 9–10 | A1 prompt, cross-check, generated evaluation set (team reviews) | Slurm job arrays, full candidate runs |
| Nov 11 | Tuning, bias slicing, sensitivity; choose the model | Model-server image, registry push |
| Nov 12 | Confirm on the test sets | Cloud Run deployment, latency check |
| Nov 13 | Report sections | CI/CD with gates, notifications, rollback |
| Nov 14–15 | Buffer and submission | Buffer and submission |

---

## 11. Next milestone

- **A3:** grounded answers, with evaluation inputs built from the synthetic store fixtures (never stored Kroger data) and a groundedness checker; quantities need an explicit carve-out, since `result.json` has none.
- **B:** the risk classifier, evaluated with the pipeline's bias harness (`bias.run` accepts a replacement `assess_fn`); its held-out set grows weekly from products added after October 26. Its training text (Open Food Facts) differs from most request-time evidence (Kroger), which needs a plan.
- **Contract extensions** (servings, tweaks, classifier flags) only through a `schema_version` bump.

---

## 12. Notes for the data pipeline owner

- Numbers disagree across the docs (snapshot rows and size, cuisine mix, task counts); the PDF may lag the Markdown.
- `resolve_dish` requires `LLM_API_KEY` even when a keyless local client could run; passing `llm_client` works around it.
- Store providers are rebuilt per request unless passed in, so the 15-minute cache and the Kroger token don't persist; reusing them makes Target's per-request credit budget process-wide.
