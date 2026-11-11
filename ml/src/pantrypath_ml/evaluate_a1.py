"""A1 evaluation: chat messages -> RequestInfo, scored against the generated cases.

Metrics (rates over all cases; an unusable reply counts as a miss):
- allergen_exact: the model's avoid equals the expected set.
- allergen_precision: of all allergens the model put in avoid, the share expected (over
  every case pooled); extra_allergen_rate: cases whose avoid has an allergen not expected;
  extra_flag_rate: of those cases, the share where the cross-check asks about an extra one
  because the chat never mentions it.
- allergens_captured: every expected allergen is in avoid, or flagged by the cross-check
  for the user to confirm (the safety target, 100%).
- diet_accuracy (exact) and diet_captured (the model's diet, or the cross-check's
  suggestion, is at least as strict as expected).
- zip_accuracy, servings_accuracy, valid_json_rate.
- dish_resolution: store.find_dish on the parsed query returns the intended dish.
- unsupported_recall (after the cross-check) and unsupported_model_recall (the model
  alone), over the cases that have an unsupported need.
- crosscheck_catch_rate: of the cases where the model missed an allergen or diet, the
  share the cross-check flagged; false_confirmation_rate: confirmations asked although
  the model's avoid and diet were exactly right.
- latency and tokens per case. The same metrics per phrasing style, diet and allergen.

    python -m pantrypath_ml.evaluate_a1 --candidate qwen3-4b --split dev|test \
        [--variant base] [--limit N]
"""

import argparse
import json
import logging
from collections.abc import Callable
from typing import Any

from pantrypath_ml import config
from pantrypath_ml.evalsets import load_a1
from pantrypath_ml.local_llm import (
    Candidate,
    RecordingClient,
    cached_client,
    client_settings,
    get_candidate,
    load_candidates,
)
from pantrypath_ml.parse_request import (
    DIET_RANK,
    CrossCheck,
    load_prompt,
    parse_request,
    prompt_version,
)
from pantrypath_ml.reporting import (
    add_model_args,
    log_run,
    new_run_id,
    ratio,
    run_dir,
    usage_stats,
    write_json,
    write_jsonl,
)
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import PipelineError
from pantrypath_pipeline.logging_setup import setup_logging
from pantrypath_pipeline.store import find_dish

log = logging.getLogger(__name__)

TASK = "a1"
RATES = ("valid_json_rate", "allergen_exact", "allergens_captured", "extra_allergen_rate",
         "diet_accuracy", "diet_captured", "zip_accuracy", "servings_accuracy",
         "dish_resolution")


def dish_resolver(pcfg: Config) -> Callable[[str | None], str | None]:
    """query -> catalog dish_id, with the request flow's catalog and fuzzy threshold."""
    db = pcfg.path(pcfg["catalog"]["sqlite_output"])
    min_score = float(pcfg["request"]["fuzzy_min_score"])

    def resolve(query: str | None) -> str | None:
        if not query:
            return None
        dish = find_dish(db, query, min_score)
        return dish.dish_id if dish else None
    return resolve


def score_case(case: dict[str, Any], result: dict[str, Any],
               resolved: str | None) -> dict[str, Any]:
    """Per-case booleans; the cross-check's suggestion counts as captured (it is asked)."""
    exp = case["expected"]
    valid = result["status"] != "invalid"
    fields = result["fields"] or {}
    model_avoid = set(fields.get("avoid") or [])
    final = result["suggested"] or fields
    final_avoid = set(final.get("avoid") or [])
    expected_avoid = set(exp["avoid"])
    model_diet = fields.get("diet", "none")
    exp_rank = DIET_RANK[exp["diet"]]
    model_ok = valid and expected_avoid <= model_avoid and DIET_RANK[model_diet] >= exp_rank
    captured = valid and expected_avoid <= final_avoid \
        and DIET_RANK[final.get("diet", "none")] >= exp_rank
    unsupported = set(exp["unsupported"])
    extra = model_avoid - expected_avoid if valid else set()
    exact = valid and model_avoid == expected_avoid and model_diet == exp["diet"]
    return {
        "valid_json_rate": valid,
        "allergen_exact": valid and model_avoid == expected_avoid,
        "allergens_captured": valid and expected_avoid <= final_avoid,
        "avoid_listed": len(model_avoid) if valid else 0,
        "avoid_expected": len(model_avoid & expected_avoid) if valid else 0,
        "extra_allergen_rate": bool(extra),
        "extra_flagged": bool(extra & set(result.get("unmentioned") or [])),
        "diet_accuracy": valid and model_diet == exp["diet"],
        "diet_captured": valid and DIET_RANK[final.get("diet", "none")] >= exp_rank,
        "zip_accuracy": valid and fields.get("zip") == exp["zip"],
        "servings_accuracy": valid and result["extras"].get("servings") == exp["servings"],
        "dish_resolution": resolved == case["dish_id"],
        "unsupported_recall": unsupported <= set(result["extras"].get("unsupported") or [])
        if unsupported else None,
        "unsupported_model_recall": valid and unsupported <= set(result["model_unsupported"])
        if unsupported else None,
        "model_missed": not model_ok,
        "caught": not model_ok and captured,
        "false_confirmation": result["status"] == "needs_confirmation" and exact,
        # per expected allergen: found by the model, captured after the cross-check
        "per_allergen": {a: {"model": valid and a in model_avoid,
                             "captured": valid and a in final_avoid}
                         for a in sorted(expected_avoid)},
    }


def evaluate_case(prcfg: Config, case: dict[str, Any], client: RecordingClient, *,
                  variant: str, checker: CrossCheck, template: Any,
                  resolve: Callable[[str | None], str | None]) -> dict[str, Any]:
    result = parse_request(prcfg, case["messages"], client, variant=variant, template=template,
                           checker=checker).to_dict()
    resolved = resolve((result["fields"] or {}).get("query"))
    return {"case_id": case["case_id"], "dish_id": case["dish_id"], "split": case["split"],
            "messages": case["messages"], "expected": case["expected"],
            "slices": case["slices"], "result": result, "resolved_dish_id": resolved,
            "score": score_case(case, result, resolved), **client.take()}


def summarize(records: list[dict[str, Any]], *, with_slices: bool = True) -> dict[str, Any]:
    n = len(records)
    scores = [r["score"] for r in records]
    summary: dict[str, Any] = {"cases": n, **{k: ratio(sum(s[k] for s in scores), n)
                                             for k in RATES}}
    summary["allergen_precision"] = ratio(sum(s["avoid_expected"] for s in scores),
                                          sum(s["avoid_listed"] for s in scores))
    extra = [s for s in scores if s["extra_allergen_rate"]]
    summary["extra_flag_rate"] = ratio(sum(s["extra_flagged"] for s in extra), len(extra))
    with_unsupported = [s for s in scores if s["unsupported_recall"] is not None]
    summary["unsupported_cases"] = len(with_unsupported)
    for k in ("unsupported_recall", "unsupported_model_recall"):
        summary[k] = ratio(sum(s[k] for s in with_unsupported), len(with_unsupported))
    missed = [s for s in scores if s["model_missed"]]
    summary["model_missed"] = len(missed)
    summary["crosscheck_catch_rate"] = ratio(sum(s["caught"] for s in missed), len(missed))
    summary["false_confirmation_rate"] = ratio(sum(s["false_confirmation"] for s in scores), n)
    summary["needs_confirmation_rate"] = ratio(
        sum(r["result"]["status"] == "needs_confirmation" for r in records), n)
    summary.update(usage_stats(records))
    if with_slices:
        summary["slices"] = slices(records)
    return summary


def slices(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per style, diet and unsupported flag; per allergen, how often it was captured."""
    out: dict[str, Any] = {}
    for name in ("style", "diet", "has_unsupported"):
        values = sorted({str(r["slices"][name]) for r in records})
        out[name] = {v: summarize([r for r in records if str(r["slices"][name]) == v],
                                  with_slices=False) for v in values}
    allergens: dict[str, list[dict[str, bool]]] = {}
    for r in records:
        for a, hit in r["score"]["per_allergen"].items():
            allergens.setdefault(a, []).append(hit)
    out["allergen"] = {a: {"cases": len(hits),
                           "captured_rate": ratio(sum(h["captured"] for h in hits), len(hits)),
                           "model_recall": ratio(sum(h["model"] for h in hits), len(hits))}
                       for a, hits in sorted(allergens.items())}
    return out


def run(candidate_name: str, split: str, *, variant: str = "base", limit: int | None = None,
        server: str | None = None, quantization: str | None = None,
        overrides: dict[str, Any] | None = None, run_id: str | None = None,
        use_mlflow: bool = True, tags: dict[str, str] | None = None,
        cases: list[dict[str, Any]] | None = None, ccfg: Config | None = None,
        pcfg: Config | None = None) -> dict[str, Any]:
    """Evaluate one setting on one split (or on given cases); writes JSONL and summary.json."""
    ccfg = ccfg or load_candidates()
    pcfg = pcfg or config.pipeline_config()
    prcfg = config.load("parse_request")
    candidate = get_candidate(ccfg, candidate_name, server=server, quantization=quantization)
    settings = client_settings(ccfg, overrides, task=TASK)
    if cases is None:
        cases = load_a1(config.load("evalsets"), split, allow_test=split == "test")
    if limit:
        cases = cases[:limit]
    version = prompt_version(prcfg, variant)
    run_id = run_id or new_run_id(TASK)
    client = RecordingClient(cached_client(ccfg, candidate, version, settings=settings,
                                           run_id=run_id))
    checker, (template, _) = CrossCheck(prcfg["cross_check"]), load_prompt(prcfg)
    resolve = dish_resolver(pcfg)
    log.info("A1 %s (%s) on %s: %d cases (run %s)", candidate.name, variant, split, len(cases),
             run_id)
    records = [evaluate_case(prcfg, c, client, variant=variant, checker=checker,
                             template=template, resolve=resolve) for c in cases]
    return finish(records, candidate=candidate, settings=settings, split=split,
                  variant=variant, prompt_version=version, run_id=run_id,
                  use_mlflow=use_mlflow, tags=tags)


def finish(records: list[dict[str, Any]], *, candidate: Candidate, settings: dict[str, Any],
           split: str, variant: str, prompt_version: str, run_id: str, use_mlflow: bool,
           tags: dict[str, str] | None) -> dict[str, Any]:
    folder = run_dir(TASK, run_id)
    params = {"candidate": candidate.name, "server": candidate.server, "model": candidate.model,
              "quantization": candidate.quantization, "params_b": candidate.params_b,
              "temperature": settings["temperature"], "think": settings["think"],
              "prompt_variant": variant, "prompt_version": prompt_version, "split": split,
              "cases": len(records)}
    summary = {"task": TASK, "run_id": run_id, "params": params, "tags": tags or {},
               **summarize(records)}
    write_jsonl(folder / "cases.jsonl", records)
    write_json(folder / "summary.json", summary)
    if use_mlflow:
        summary["mlflow_run_id"] = log_run(
            task=TASK, candidate=candidate, prompt_version=prompt_version, split=split,
            run_id=run_id, params=params, summary=summary, folder=folder, tags=tags)
        write_json(folder / "summary.json", summary)
    log.info("A1 run %s: allergens captured %s, exact %s, dish %s -> %s", run_id,
             summary["allergens_captured"], summary["allergen_exact"],
             summary["dish_resolution"], folder)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantrypath_ml.evaluate_a1",
                                     description=__doc__.splitlines()[0])
    add_model_args(parser)
    parser.add_argument("--split", choices=["dev", "test"], default="dev",
                        help="test is the held-out set: gates and final results only")
    parser.add_argument("--variant", default="base", help="prompt variant (parse_request.yaml)")
    args = parser.parse_args(argv)
    setup_logging()
    try:
        summary = run(args.candidate, args.split, variant=args.variant, limit=args.limit,
                      server=args.server, quantization=args.quantization,
                      overrides={"temperature": args.temperature, "think": args.think},
                      run_id=args.run_id, use_mlflow=not args.no_mlflow)
    except PipelineError as exc:
        log.error("A1 evaluation failed: %s", exc)
        return 1
    print(json.dumps({k: v for k, v in summary.items() if k != "slices"}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
