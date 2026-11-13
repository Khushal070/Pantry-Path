# Final confirmation 20261113T233257Z-final (full)

Setting: `{"candidate": "qwen3-4b", "quantization": "4bit", "a1": {"prompt_variant": "rules_v2", "temperature": 0.0, "think": false, "dev_run": "20261111T225007Z-a1-a7af2c"}, "a2": {"prompt_variant": "refine_dish_unknown", "temperature": 0.0, "think": false, "dev_run": "20261111T222859Z-a2-ef04d0", "bias_run": "20261112T223331Z-a2-6f49bb", "final_bias_run": "20261112T004423Z-a2-181930"}, "a3": {"prompt_variant": "hybrid_swaps", "temperature": 0.0, "think": false, "dev_run": "20261113T024012Z-a3-12165d"}}`

**Passed** the hard and bias gates.

**Combined final.** A1, A1_REALISTIC, A2, A3 are the runs of final `20261113T044153Z-final` (commit `76ed16d8b6f55c8b2630d852dc7e5032fa3701a3`; the same setting and prompt versions), reused as they are; no model ran. Every gate below was applied again at commit `720839416cf3ff708e3e6e4e377570336e9b1265`.

## A1 (test)

Gates:

| Metric | Value | Limit | Passed |
|---|---|---|---|
| allergens_captured | 100.0% | ≥ 100.0% | yes |
| diet_captured | 100.0% | ≥ 100.0% | yes |
| unsupported_recall | 100.0% | ≥ 100.0% | yes |
| valid_json_rate | 100.0% | ≥ 99.0% | yes |
| bias gap (max) | 0.0% | ≤ 10.0% | yes |


Goals (section 8 of the plan; reported, not gated):

| Metric | Value | Target | Met |
|---|---|---|---|
| allergens_captured | 100.0% | 100.0% | yes |
| diet_captured | 100.0% | 100.0% | yes |
| unsupported_recall | 100.0% | 100.0% | yes |
| dish_resolution | 98.9% | 95.0% | yes |
| valid_json_rate | 100.0% | 99.0% | yes |


## A2 (test)

Gates:

| Metric | Value | Limit | Passed |
|---|---|---|---|
| valid_profile_rate | 100.0% | ≥ 99.0% | yes |
| bias gap (max), warning only | 13.5% | ≤ 10.0% | no (warning) |


Goals (section 8 of the plan; reported, not gated):

| Metric | Value | Target | Met |
|---|---|---|---|
| valid_profile_rate | 100.0% | 99.0% | yes |
| recall | 55.8% | 75.0% | no |
| core_precision | 62.2% | 85.0% | no |


Warning: bias gap 0.1345 > 0.1. Reported, not blocking: the A2 cuisine groups are small (golden dev: at most 5 dishes per cuisine; search leave-one-out, without the golden test dishes: 48 american, 7 or fewer for every other cuisine; the full leave-one-out in select final: 60 american, 10 or fewer for every other cuisine), and the leave-one-out references are catalog entries written by an LLM, not ground truth, so a gap there is a consistency signal, not a measured error rate.

## A1_REALISTIC (realistic)

Gates:

| Metric | Value | Limit | Passed |
|---|---|---|---|
| allergens_captured | 100.0% | ≥ 100.0% | yes |
| diet_captured | 100.0% | ≥ 100.0% | yes |
| valid_json_rate | 100.0% | ≥ 97.0% | yes |


Goals (section 8 of the plan; reported, not gated):

| Metric | Value | Target | Met |
|---|---|---|---|
| allergens_captured | 100.0% | 100.0% | yes |
| diet_captured | 100.0% | 100.0% | yes |


## A3 (test)

Gates:

| Metric | Value | Limit | Passed |
|---|---|---|---|
| final_clean_rate | 100.0% | ≥ 100.0% | yes |
| valid_json_rate | 100.0% | ≥ 95.0% | yes |
| repaired_rate | 10.0% | ≤ 50.0% | yes |


Goals (section 8 of the plan; reported, not gated):

| Metric | Value | Target | Met |
|---|---|---|---|
| verdict_safety_rate | 100.0% | 95.0% | yes |
| substitute_safety_rate | 100.0% | 90.0% | yes |
| text_safety_rate | 90.0% | 90.0% | yes |
| coverage_rate | 100.0% | 90.0% | yes |
| missing_handled_rate | 91.8% | 90.0% | yes |

