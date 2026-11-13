# Model Pipeline and Deployment Plan

**Version 3** (November 12, 2026). Version 2 (November 7) replaced the November 1 draft after a full review of the data pipeline code. Targets and owners are proposals until the team agrees.

**Version 3 changes**, after a review of the `ml/` code and the first local runs:
- **One candidate, Qwen3-4B.** Gemma 4 E4B is dropped for this milestone: the team's machines have little free disk (Ollama's Gemma 4 E4B download is 9.6 GB) and the runs happen on laptops. Selection is over settings of one model (prompt variant, temperature, quantization). A second candidate can be added later as one config block.
- **Final defaults:** A1 `rules_v2`, A2 `refine_dish_unknown`, temperature 0, thinking off (sections 4, 5 and 8). Targets split into hard gates and reported goals (section 8).
- **A3 is built this milestone** with the same model (section 6): grounded answers on synthetic `result.json` files, a deterministic grounding check that repairs what breaks, and gates. The risk classifier (B) stays next milestone.
- **Held-out everyday phrasing:** `ml/evalsets/a1_realistic.jsonl` (45 hand-written chats; 41 count, see section 5). The generated A1 sets share their wording with the cross-check, so the cross-check scores close to 100% on them by construction.
- **Cross-check fixes:** a narrower "fine" word no longer clears a whole allergen ("allergic to shrimp but crab is fine" kept shellfish off the list before); lists in parentheses or after "allergies:"; more cues ("lactose intolerant", "can't do", "nothing with", "but not"); foods made from an allergen ("no butter or cheese" -> milk; "seafood" -> fish and shellfish); "nuts" without peanuts needs confirmation, with a suggestion that avoids both tree nuts and peanuts (scored apart from false confirmations).
- **ZIP check:** the chat is the authority for the ZIP (a ZIP the chat doesn't contain is dropped; a different one needs confirmation; ZIP+4 is cut).
- **Leakage fix:** the leave-one-out set used by search and selection leaves out the golden test dishes, so selection never sees a test dish. The final A2 bias check uses the full 100-dish leave-one-out run (`loo_full`, held out like the test set), since only it has four cuisines with 10 dishes.
- **Bias gate:** a gap that can't be measured is reported as "insufficient data" and fails A1 in the final run, instead of passing silently; the final A2 bias gap comes from the chosen setting's full leave-one-out run.
- **CI** evaluates a seeded dev sample (never the held-out sets) and promotes only from a committed full final report; a deploy is smoke-tested and rolled back automatically if it fails.
- **Runtime adapter** (`pantrypath_ml.runtime`) for the backend, and Google ID tokens for the private Cloud Run service.
- **Two data-pipeline changes**, agreed with its owner (section 13): `build_result()` takes an optional A2 prompt template (`llm_template`, passed down to `refine.profile_for_unknown_dish`; default unchanged), so the backend's A2 uses the chosen prompt; and six matcher synonyms in `catalog_eval`, so the catalog and A2 are scored with the same matcher (section 4).
- **Deviation from the scoping report**, for the record: the scoping report planned Qwen2.5-7B / Llama 3.1 8B, recipe adaptation by the model, and LoRA fine-tuning if prompting fell short. Version 2 narrowed the model to request parsing (A1) and unseen dishes (A2); version 3 adds the grounded answer (A3), which covers substitutions; no fine-tuning was needed.

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
        A3 (this milestone, v3): grounded answer; explains and suggests substitutes, never changes a verdict
```

| Model | Milestone | Input | Output | Seam |
|---|---|---|---|---|
| **A1** request parsing | This one | Chat messages | A JSON object valid as `pantrypath_core.result.RequestInfo`, plus an `extras` object for anything the contract can't hold | Backend, before `build_result()` |
| **A2** unseen dishes | This one | Dish name | The existing `REFINE_SCHEMA` reply (core **and** common ingredients, at least 3 with 1 core) | `build_result(..., llm_client=...)` through `refine.profile_for_unknown_dish` |
| **A3** answer | This one (version 3) | `result.json` + A1 extras | Answer grounded in `result.json` (section 6) | Backend, after `build_result()` |
| **B** risk classifier | Next | The chosen `ProductEvidence` + avoid/diet | Per-allergen and diet risk, applied only to `safe` | `request.judge()`, after `assess()` |

---

## 3. The model

- **Candidate:** Qwen3-4B (Apache 2.0), one model for A1, A2 and A3. Version 2 also listed Gemma 4 E4B; it is out of this milestone (version 3, disk and compute). Qwen3-8B only if Qwen3-4B misses the targets.
- **Selection rule:** among the settings that pass every hard gate, the smallest model, then the best score. With one candidate this picks the prompt variant, temperature and quantization.
- **Serving:** an OpenAI-compatible server (Ollama or llama.cpp on CPU; vLLM on Explorer for evaluation throughput), with schema-constrained JSON output.
- **Client:** a new `LocalLLMClient` implementing the existing `LLMClient` protocol (`complete_json(prompt, schema, *, purpose) -> LLMReply`), with a configurable base URL, no API key, and token and latency logging through the existing usage reporting.
- **Single source of truth:** the model id lives in configuration only (the repo currently names three different models across `.env.example` and the docs).
- **Cache trap:** the existing cache key ignores which model answered, so each candidate gets its **own cache directory**. Otherwise a new model silently replays Claude's answers.

---

## 4. A2: unseen dishes

**Task:** given only a dish name, produce the catalog-format profile with `ml/prompts/refine_dish_unknown.txt` (version `refine_dish_unknown/1`), a prompt written for a dish name with no recipe data. It runs through `refine.refine_dish` with the pipeline's `REFINE_SCHEMA`, `RefineReply`, validation, one-retry logic and `build_profile`, so the reply and the profile are the same as the request flow's; only the prompt differs.

**Baseline:** the pipeline's own prompt, `prompts/refine_dish.txt` (version `refine_dish/1`), through `refine.profile_for_unknown_dish` exactly as the request flow runs it. It was written to refine scraped recipe data, not to profile a dish from its name. On the golden dev set (16 dishes, Qwen3-4B 4-bit, temperature 0, thinking off) the baseline reached ingredient recall 0.47 and core precision 0.58; `refine_dish_unknown` reached 0.64 and 0.63, both with every profile valid. Both prompts stay in `ml/configs/refine_dish.yaml`, and every A2 report records which one it used.

**In the request flow.** `build_result(..., llm_client=..., llm_template=...)` passes an optional prompt template down to `refine.profile_for_unknown_dish` (data-pipeline change of November 12; without it the prompt is still `refine_dish/1`). `pantrypath_ml.runtime.load()` gives the champion's A2 prompt as `models.a2_template`, and the A2 evaluation goes through the same function with the same template, so what is evaluated is what the backend runs.

**Matcher synonyms.** Six pairs were added to `catalog_eval.synonyms` in the data pipeline config (November 12, all from the dev split): mayonnaise/mayo, spaghetti/spaghetti noodles, rice noodles/pad thai noodles, tamarind paste/tamarind, sushi-grade salmon/salmon, whole wheat bread/bread. Each is the same product on the shelf. The catalog evaluation was rerun with them: its numbers did not change (dev recall 91.1%, test 89.8%; none of the pairs covers a catalog miss). Every A2 run used for selection was rescored from its saved profiles (`evaluate_a2 --rescore`, no model calls): `refine_dish` on dev 0.47 → 0.52 recall, 0.58 → 0.63 core precision; `refine_dish_unknown` 0.64 → 0.65 and 0.63 → 0.65; the ranking of the settings did not change. The numbers above are from before the rescore.

**Evaluation sets**

| Set | Contents | Use |
|---|---|---|
| Golden dev | `data/golden/recipes.json` (16 reference recipes) | Prompt and setting choices |
| Golden test | `data/golden/recipes_heldout.json` (27 reference recipes) | **Gates and final results only**; never used for tuning |
| Leave-one-out catalog | Each catalog dish asked by name only, compared with its catalog entry, **except the golden test dishes** (left out by name, so selection never sees a test dish) | Broader coverage and the A2 bias gap, in selection and in the final run; catalog entries are LLM-written, so this is a consistency check, not ground truth |

**Metrics:** valid-profile rate (passes `DishProfile` validation), ingredient recall and core precision with the pipeline's `evaluate.Matcher` (same synonyms, skip list and modifiers as the catalog evaluation), "not a dish" false-refusal rate, and latency per dish.

---

## 5. A1: request parsing

**Task:** chat messages → `RequestInfo` (`query`, `zip`, `avoid` from the 10 allergens, `diet` from none, vegetarian, vegan) plus `extras` (servings, tweaks such as "crustless", unsupported needs).

**Safety design**
- **Deterministic cross-check:** a keyword scan of the chat for allergen and diet words, including synonyms and foods made from an allergen ("dairy", "butter", "cheese" → milk, "celiac" → gluten, "seafood" → fish and shellfish, "nuts" → tree_nuts, with peanuts handled explicitly). Mollusks (oyster, clam, mussel, scallop) count as shellfish, as in `diet_rules`. If the model's `avoid` misses anything the scan found, its diet is less strict, or it lists an allergen the chat never mentions, the request is sent back for confirmation with a stricter suggested request.
- **Ambiguous words:** "nuts" could mean tree nuts, peanuts or both. Unless the chat mentions peanuts or the model already avoids them, the request needs confirmation; the suggested request avoids both tree nuts and peanuts, but peanuts are never added on their own. In scoring, a confirmation asked only for this question, on an otherwise exact parse, is not a false confirmation: it is reported apart (`ambiguous_confirmation_rate`).
- **Unsupported needs are never dropped:** halal, kosher, low-sodium, diabetic, pregnancy and other diets the rules can't check go to `extras.unsupported`, and the app tells the user plainly.
- **Negations handled:** "no nuts" means avoid nuts; "nuts are fine" means don't. A "fine" phrase clears a whole allergen only when it names the whole allergen: "allergic to shrimp but crab is fine" still avoids shellfish.
- **ZIP check:** the chat is the authority. A ZIP the model gives that the chat doesn't contain is dropped (the request is incomplete), a missing one is taken from the chat, a different one needs confirmation; ZIP+4 is cut to five digits.

**Evaluation set (synthetic, reviewed):** generated from catalog dish names and aliases, an allergen and diet phrase bank, negations, typos, casual and formal phrasing, multi-turn corrections and unsupported diets. Split by dish into dev and test, so test dishes never appear in tuning.

**Realistic set (hand-written, held out):** `ml/evalsets/a1_realistic.jsonl`, 45 chats in everyday phrasing on dev dishes, read only by the final run. The generated sets share their wording with the cross-check's word lists, so this is the check on wording neither was written against. Four chats are **removed for independence** from its metrics and gate (`a1r-banana-bread-0`, `a1r-butter-chicken-0`, `a1r-spinach-artichoke-dip-0`, `a1r-vegan-pancake-0`): they paraphrase the review examples the cross-check was fixed on ("cashews especially", "nut allergy", "lactose intolerant"). They stay as unit tests, and 41 chats count.

**Metrics:** allergen-set exact match, diet accuracy, dish resolution (does `find_dish` reach the intended catalog dish), ZIP accuracy, valid-JSON rate, unsupported-need detection, latency.

---

## 6. A3: grounded answer

**Task:** `result.json` plus A1's extras → the answer the shopper reads: a shopping list where each ingredient is `buy` (one of its safe products, at a store that sells it), `substitute`, `check_label` (only unverified products) or `skip` (a common ingredient), with quantities for the servings, 3 to 8 short steps and a summary. Same model as A1 and A2, its own prompt `ml/prompts/answer.txt` and a closed JSON schema.

**A hybrid (`answer/2`).** Code decides each ingredient's status and its product and store from `result.json`, and picks substitutes from a vetted table (`ml/configs/substitutes.yaml`, hand-written by food family), each checked against the shopper's allergens and diet; the model writes only the summary, steps and quantities around those fixed decisions, mentioning only the given items. The first version (`answer/1`), where the model wrote the whole list, needed the repair in 93% of dev answers (statuses `result.json` doesn't allow, unsafe substitutes such as whole wheat tortillas for a wheat-avoider), so it was not promoted.

**Safety design**
- **The model never sets a verdict or a status.** Verdicts come only from `diet_rules.assess` and are shown from `result.json`; statuses, products and substitutes come from code. The prompt shows the model only the decided list: a bought product's name and a substitute, never a product that isn't safe.
- **Deterministic grounding check** (`answer.check`): every ingredient of `result.json` exactly once and none invented; `buy` only with a product marked safe for that ingredient, at a store `result.json` lists it for; no product that isn't safe named anywhere in the text; every substitute passes a word screen for the avoid list and diet (`diet_rules` judges products, not ingredient names, so "almond milk" fails for tree nuts, "coconut milk" passes for milk); the summary, steps and notes name no avoided allergen and nothing the diet excludes, other than the dish's own name, ingredients the answer buys and substitutes; a substituted, label-checked or skipped ingredient may be named only inside a substitution or avoid phrase ("instead of", "replace", "swap", "without"), so "Stir in the heavy cream." fails for a milk-avoider when the cream is substituted.
- **The same check and repair** as before run on the whole answer (code's list plus the model's text), so a decision bug would be caught too; on the dev results every decided list passes on its own.
- **Retry, then repair:** one retry shows the model what broke; whatever is still broken is repaired, so the answer that is shown always passes the check. Every attempt (first reply, retry) is saved with its violations, and the repair is the shown answer, so a run can be rescored after a check change without calling the model.
- **Warnings added by code**, never left to the model: unverified products, estimated quantities, needs A1 couldn't check, the nuts question and the Open Food Facts attribution.
- **Quantities** are the carve-out `result.json` has no field for: they are labelled as estimates for the servings.
- **No Kroger data kept:** in production the A3 client has no reply cache, since its prompt holds Kroger product names.

**Evaluation sets:** one synthetic `result.json` per catalog dish (`python -m pantrypath_ml.evalsets generate-a3`, seeded): synthetic stores and products named after the catalog's ingredients, verdicts from the ingredient's category and name against a sampled avoid list and diet, about 20% unknown. Never Kroger data. Each case is validated against `pantrypath_core.result.Result`, and the split is A1's (the same test dishes): `ml/evalsets/a3_dev.jsonl` (70) and `a3_test.jsonl` (30), committed; CI checks they are current.

**Metrics** (no reference answers: every metric is a rule `result.json` decides, scored on the model's reply **before** repair): valid JSON, grounded (no rule broken), verdict safety, substitute safety, text safety, coverage, missing ingredients handled, quantities given, steps given, and the final clean rate (the shown answer passes the check), plus the same per cuisine, diet, avoid count and number of ingredients without a safe product.

**Setting:** temperature 0 and thinking off, like the chosen A1 and A2 settings. Neither was searched for A3 this milestone; A3 is evaluated on dev and confirmed on test with that setting.

---

## 7. Selection, tuning, bias and sensitivity

- **Search space:** model × prompt variant (base prompt, few-shot examples, extra rules) × temperature (0, 0.2, 0.5) × quantization (4-bit, 8-bit).
- **Runs:** Slurm job arrays on Explorer, one task per combination, logged to MLflow (settings, metrics, model and prompt versions).
- **Bias slicing (Fairlearn `MetricFrame`):** A2 by cuisine and dish popularity (note the known cuisine-label quirks, such as Caesar salad labelled Latin American); A1 by allergen, diet, and phrasing type (negation, synonym, typo, multi-turn).
- **Mitigation:** targeted few-shot examples and prompt rules for weak slices; synonym additions for the cross-check; documented trade-offs.
- **Sensitivity:** A1 perturbation tests (paraphrase, typo, allergy mentioned late); temperature, quantization and few-shot-count curves for both tasks. SHAP comes with the classifier next milestone.
- **Selection after bias checks:** best score among combinations that pass the hard gates; the bias gate blocks for A1 and is a reported warning for A2 (see section 8). A gap that can't be measured is reported as "insufficient data", never as a pass. A3 is not searched: its dev run must pass its hard gates and is recorded with the chosen setting.

---

## 8. Targets

Targets are of two kinds (`ml/configs/gates.yaml`):

- **Hard gates** guard safety and usable output. A setting below one is never chosen or promoted.
- **Reported goals** are the remaining targets. Every selection and final report states whether each was met, but missing one does not block. Instead, a new setting must be **no worse than the current champion** on every goal without a hard gate and on every metric in the selection score, and better on that score.

| Metric | Target | Kind |
|---|---|---|
| A1 allergens captured (test set) | 100%, with the cross-check as a backstop | Hard gate |
| A1 diet captured (test set) | 100%, with the cross-check as a backstop | Hard gate |
| A1 unsupported needs flagged | 100% | Hard gate |
| A1 valid JSON | ≥ 99% | Hard gate |
| A2 valid profile | ≥ 99% | Hard gate |
| A1 dish resolves to the intended catalog dish | ≥ 95% | Goal; no regression |
| A2 ingredient recall on the golden test set | ≥ 0.75 (the bar the pipeline already uses for the catalog) | Goal; no regression |
| A2 core precision on the golden test set | ≥ 0.85 | Goal; no regression |
| Largest gap between slices, A1 (allergens captured) | ≤ 10 percentage points | Hard gate |
| Largest gap between slices, A2 (ingredient recall) | ≤ 10 percentage points | Reported warning |
| A1 allergens and diet captured, realistic set (41 chats) | 100% | Hard gate |
| A1 valid JSON, realistic set | ≥ 97% | Hard gate |
| A3 shown answer passes the grounding check (final clean rate) | 100% | Hard gate |
| A3 valid JSON | ≥ 95% | Hard gate |
| A3 answers that needed the repair (repaired rate) | ≤ 50% | Hard gate |
| A3 verdict safety (model's reply before repair) | ≥ 95% | Goal; no regression |
| A3 substitute safety, text safety, coverage, missing ingredients handled | ≥ 90% each | Goal; no regression |
| A1 latency on the deployed server | ≤ 3 s per request (to be measured on Cloud Run's CPUs first; see `ml/README.md`, Latency) | Checked after deployment |
| A2 latency on the deployed server | ≤ 15 s per dish (catalog misses only; same caveat) | Checked after deployment |

**Why A3's hard gates are on the shown answer.** The repair step guarantees that what the shopper sees passes every grounding rule, so the safety guarantee is the final clean rate, gated at 100%. The model's own reply before repair is a quality measure: a low score means more repaired answers (and more generic notes), not unsafe ones, so those metrics are goals with no regression allowed. One exception is gated: at most half the answers may need the repair, since a model the check rewrites more often than not is not the one writing the answer.

**Why the A2 bias gap is a warning, not a gate.** The cuisine groups are small. The golden dev set has at most 5 dishes per cuisine, below the 10-dish minimum for comparing groups. The leave-one-out catalog, without the 27 golden test dishes, has 73 dishes: 48 American and at most 7 for each other cuisine, so only its popularity tiers can be compared. Its references are catalog entries written by an LLM, not ground truth, so a gap there is a consistency signal, not a measured error rate. A gap over 10 points is recorded in the selection and final reports with this reason. It is not ignored: the first leave-one-out run (which still included the test dishes) showed a 15.8-point recall gap between European and East/Southeast Asian dishes, which remains a target for prompt mitigation (section 7); on the 73-dish set the cuisine gap can't be measured, and the largest measurable gap is 7.7 points (popularity tiers). The A1 gap stays a gate, since a missed allergen in any phrasing style is a safety failure.

**Why thinking is off.** Qwen3's thinking mode was tried with the chosen prompts on the local Ollama server (Qwen3-4B 4-bit, temperature 0):

- **A1:** on 60 dev cases, a mean of 20.7 s per request, against 2.5 s without thinking (about 8×) and a 3 s limit. It scored higher on those 60 cases (allergen exact match 100%, against 89% on all 420 cases without thinking), but no gain in accuracy makes up for missing the latency limit by that much.
- **A2:** a mean of 135 s per dish against 12.3 s without (about 11×) and a 15 s limit. The valid-profile rate also fell to 93.75%, below the 99% hard gate.

Thinking stays a search option (`think` in `ml/configs/search.yaml`) but is off by default.

---

## 9. Registry, CI/CD and deployment

- **Tracking:** MLflow (added to the requirements); every run records model, quantization, prompt version and metrics.
- **Registry:** the chosen configuration (model id, quantization, prompt versions, settings) and the model-server image go to GCP Artifact Registry.
- **CI/CD (GitHub Actions on changes to `ml/`, `shared/`, the data pipeline's code, prompts and config, and `data/golden/`):** unit tests → evaluate the proposed setting on a seeded dev sample (spread over phrasing styles, diets, allergens and cuisines) on the runner's CPU → hard gates → bias gate → compare with the current champion using the committed full final report → push to the registry → deploy → smoke test, with automatic rollback on failure → notify. CI never reads the held-out sets, a CI result is never promoted, and a worse model is never promoted.
- **What is committed:** only the `reports/final/<id>/` folder of the run being promoted; `select promote` refuses a final report that isn't committed. All other report folders are ignored.
- **Deployment:** the model server on Cloud Run (CPU, scales to zero), exposing the OpenAI-compatible endpoint that `LocalLLMClient` calls. The backend that calls `build_result()` is still empty and belongs to the backend milestone; it will use this server for A1, A2 and A3 through `pantrypath_ml.runtime`.

---

## 10. Repository layout

```
ml/
  README.md
  Dockerfile                 evaluation runner
  serving/                   model-server image and Cloud Run config
  configs/                   candidates.yaml, search.yaml, gates.yaml, refine_dish.yaml, answer.yaml
  prompts/                   parse_request (A1), refine_dish_unknown (A2), answer (A3)
  evalsets/                  A1 dev and test (generated, reviewed), a1_realistic (hand-written),
                             A3 dev and test (synthetic result.json)
  src/pantrypath_ml/
    local_llm.py             LocalLLMClient (LLMClient protocol), per-model cache
    parse_request.py         A1 prompt, schema, cross-check
    evalsets.py              A1 and A3 generators; A2 golden and leave-one-out sets
    evaluate_a1.py           A1 metrics
    evaluate_a2.py           A2 metrics (reuses evaluate.Matcher)
    answer.py                A3 prompt, grounding check, repair
    evaluate_a3.py           A3 metrics
    bias.py                  Fairlearn slicing
    sensitivity.py           perturbations and setting curves
    select.py                gates, champion comparison
    registry.py              MLflow logging, Artifact Registry push
    runtime.py               the champion for the backend: A1 parser, A2 client, A3 answer
  slurm/                     Explorer job-array scripts
  tests/
  reports/                   ignored, except the promoted reports/final/<id>/
.github/workflows/model-pipeline.yml
infra/gcp/bootstrap.sh       one-time GCP setup (repositories, service accounts, WIF)
```

The data pipeline stays unchanged; `ml/` imports its public functions.

---

## 11. Schedule and split (two people)

| Dates | Person 1: models and evaluation | Person 2: infrastructure |
|---|---|---|
| Nov 7–8 | A2 harness on golden dev; first baseline | `LocalLLMClient`, MLflow, `ml/` skeleton, Explorer environment |
| Nov 9–10 | A1 prompt, cross-check, generated evaluation set (team reviews) | Slurm job arrays, full candidate runs |
| Nov 11 | Tuning, bias slicing, sensitivity; choose the model | Model-server image, registry push |
| Nov 12 | Confirm on the test sets | Cloud Run deployment, latency check |
| Nov 13 | Report sections | CI/CD with gates, notifications, rollback |
| Nov 14–15 | Buffer and submission | Buffer and submission |

---

## 12. Next milestone

- **A3** (built this milestone, section 6): search its temperature and prompt; a hand-reviewed sample of answers; answers on real request-time results (never stored).
- **B:** the risk classifier, evaluated with the pipeline's bias harness (`bias.run` accepts a replacement `assess_fn`); its held-out set grows weekly from products added after October 26. Its training text (Open Food Facts) differs from most request-time evidence (Kroger), which needs a plan.
- **Contract extensions** (servings, tweaks, classifier flags) only through a `schema_version` bump.

---

## 13. Notes for the data pipeline owner

- **Done (November 12, agreed):** `refine.profile_for_unknown_dish(..., template=None)`, `request.llm_profile` / `resolve_dish` / `build_result(..., llm_template=None)` take an optional prompt template; the default is unchanged. Tests in `data-pipeline/tests/test_refine.py` and `test_request.py`.
- **Done (November 12, agreed):** six synonym pairs in `catalog_eval.synonyms` (section 4); `reports/catalog_eval.*` regenerated (numbers unchanged).
- `diet_rules`' shellfish words include mollusks (oyster, clam, mussel, scallop); the A1 cross-check maps them the same way (checked by a unit test).
- `RequestInfo` must keep rejecting a 4-digit ZIP (a parser test relies on it).
- A3's synthetic `result.json` files are validated against `pantrypath_core.result.Result`; if the contract changes (chains, providers), `ml/src/pantrypath_ml/evalsets.py` `STORES` / `meta` follow.

- Numbers disagree across the docs (snapshot rows and size, cuisine mix, task counts); the PDF may lag the Markdown.
- `resolve_dish` requires `LLM_API_KEY` even when a keyless local client could run; passing `llm_client` works around it.
- Store providers are rebuilt per request unless passed in, so the 15-minute cache and the Kroger token don't persist; reusing them makes Target's per-request credit budget process-wide.
