"""A2 evaluation: how well a model profiles a dish from its name only.

Each dish goes through the pipeline's own fallback, refine.profile_for_unknown_dish, so
the prompt (refine_dish/1), schema, validation and one retry are the ones the request
flow uses. Another prompt variant (configs/refine_dish.yaml, e.g. refine_dish_unknown) is
passed as its `template`, exactly as the request flow's build_result(llm_template=...) does,
with the same schema, validation, retry and build_profile. The client is the candidate's
LocalLLMClient behind a fresh per-run cache and a RecordingClient, which gives each dish
its latency, tokens and cache hits.

Scores use the pipeline's evaluate.Matcher and score_split (same synonyms, skip list and
modifiers as the catalog evaluation): ingredient recall and core precision over the dishes
with a valid profile, plus valid-profile rate, false-refusal rate ("not a dish" for a real
dish) and latency. The same scores per cuisine and popularity tier feed the bias report.

    python -m pantrypath_ml.evaluate_a2 --candidate qwen3-4b --split dev|loo|loo_full|test \
        [--variant refine_dish_unknown] [--limit N]
    python -m pantrypath_ml.evaluate_a2 --rescore reports/a2/<run> [reports/a2/<run> ...]

The loo split (search) leaves out the golden test dishes; loo_full asks every catalog dish
and is held out like test (the final A2 bias check). --limit takes a seeded sample spread
over cuisines, not the first N dishes.
"""

import argparse
import json
import logging
import string
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pantrypath_ml import config
from pantrypath_ml.evalsets import a2_cases
from pantrypath_ml.local_llm import (
    Candidate,
    RecordingClient,
    cached_client,
    client_settings,
    get_candidate,
    load_candidates,
)
from pantrypath_ml.reporting import (
    add_model_args,
    log_run,
    new_run_id,
    ratio,
    read_jsonl,
    run_dir,
    sample,
    usage_stats,
    write_json,
    write_jsonl,
)
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import LLMError, PipelineError
from pantrypath_pipeline.evaluate import Matcher, score_split
from pantrypath_pipeline.logging_setup import setup_logging
from pantrypath_pipeline.refine import load_template, profile_for_unknown_dish

log = logging.getLogger(__name__)

TASK = "a2"
SLICES = ("cuisine", "popularity")
# profile_for_unknown_dish raises LLMError for both; this phrase marks the refusal
REFUSAL = "is not a dish"
Template = tuple[string.Template, str]


def load_variant(pcfg: Config, variant: str) -> tuple[Template | None, str]:
    """(template, prompt version) of a configs/refine_dish.yaml variant; the template is
    None for the pipeline's own prompt."""
    rcfg = config.load("refine_dish")
    if variant not in rcfg["variants"]:
        raise PipelineError(f"Unknown A2 prompt variant {variant!r}; use one of "
                            f"{sorted(rcfg['variants'])}")
    prompt_file = rcfg["variants"][variant].get("prompt_file")
    if not prompt_file:
        return None, load_template(pcfg)[1]
    path = rcfg.path(prompt_file)
    if not path.is_file():
        raise PipelineError(f"Prompt file not found: {path}")
    text = path.read_text()
    first = text.partition("\n")[0]
    if not first.startswith("prompt_version:"):
        raise PipelineError(f"{path} must start with 'prompt_version: ...'")
    version = first.split(":", 1)[1].strip()
    return (string.Template(text), version), version


def evaluate_case(pcfg: Config, case: dict[str, Any], client: RecordingClient,
                  template: Template | None = None) -> dict[str, Any]:
    """One dish: valid profile, refusal or error, with its calls' latency and tokens."""
    status, error, profile = "valid", None, None
    try:
        profile = profile_for_unknown_dish(pcfg, case["name"], client=client,
                                           template=template).model_dump(mode="json")
    except LLMError as exc:
        status = "refusal" if REFUSAL in str(exc) else "error"
        error = str(exc)
    return {
        "case_id": case["case_id"], "name": case["name"], "dish_id": case["dish_id"],
        "cuisine": case["cuisine"], "popularity": case["popularity"],
        "status": status, "error": error, "profile": profile, "reference": case["reference"],
        **client.take(),
    }


def score(pcfg: Config, records: list[dict[str, Any]]) -> dict[str, Any]:
    """score_split over the valid profiles; each reference is mapped to its own profile."""
    settings = pcfg["catalog_eval"]
    valid = [r for r in records if r["status"] == "valid"]
    profiles = [{**r["profile"], "dish_id": r["case_id"]} for r in valid]
    overrides = {r["reference"]["dish"].lower(): r["case_id"] for r in valid}
    return score_split([r["reference"] for r in valid], profiles,
                       Matcher(settings.get("synonyms")), {**settings, "dish_ids": overrides})


def summarize(pcfg: Config, records: list[dict[str, Any]], *,
              with_slices: bool = True) -> dict[str, Any]:
    n = len(records)
    scored = score(pcfg, records)
    summary: dict[str, Any] = {
        "cases": n,
        "valid_profile_rate": ratio(sum(r["status"] == "valid" for r in records), n),
        "false_refusal_rate": ratio(sum(r["status"] == "refusal" for r in records), n),
        "error_rate": ratio(sum(r["status"] == "error" for r in records), n),
        "recall": scored["recall"],
        "core_precision": scored["core_precision"],
        "dishes_fully_covered": scored["dishes_fully_covered"],
        **usage_stats(records),
    }
    if with_slices:
        summary["slices"] = {
            name: {str(value): summarize(pcfg, [r for r in records if r[name] == value],
                                         with_slices=False)
                   for value in sorted({r[name] for r in records}, key=str)}
            for name in SLICES
        }
    return summary


def per_dish_scores(pcfg: Config, records: list[dict[str, Any]]) -> None:
    """Attach each valid dish's score_dish entry (found/required, core/core_in_reference)."""
    by_case = {d["dish_id"]: d for d in score(pcfg, records)["dishes"]}
    for r in records:
        r["score"] = by_case.get(r["case_id"])


def rescore(folder: Path, pcfg: Config | None = None) -> dict[str, Any]:
    """Score a run's saved profiles again with the current matcher (catalog_eval synonyms,
    skip list), without calling the model. The first rescore keeps the old summary as
    summary.before-rescore.json; MLflow keeps the run's original metrics."""
    pcfg = pcfg or config.pipeline_config()
    records = read_jsonl(folder / "dishes.jsonl")
    old = json.loads((folder / "summary.json").read_text())
    backup = folder / "summary.before-rescore.json"
    if not backup.is_file():
        write_json(backup, old)
    per_dish_scores(pcfg, records)
    keep = {k: old[k] for k in ("task", "run_id", "params", "tags", "mlflow_run_id") if k in old}
    summary = {**keep, **summarize(pcfg, records),
               "rescored": {"at": datetime.now(UTC).isoformat(timespec="seconds"),
                            "synonym_groups": len(pcfg["catalog_eval"].get("synonyms") or [])}}
    write_jsonl(folder / "dishes.jsonl", records)
    write_json(folder / "summary.json", summary)
    log.info("A2 run %s rescored: recall %s -> %s, core precision %s -> %s", folder.name,
             old.get("recall"), summary["recall"], old.get("core_precision"),
             summary["core_precision"])
    return summary


def run(candidate_name: str, split: str, *, variant: str | None = None,
        limit: int | None = None, server: str | None = None, quantization: str | None = None,
        overrides: dict[str, Any] | None = None, run_id: str | None = None,
        use_mlflow: bool = True, tags: dict[str, str] | None = None,
        ccfg: Config | None = None, pcfg: Config | None = None) -> dict[str, Any]:
    """Evaluate one setting on one split; writes per-dish JSONL and summary.json."""
    ccfg = ccfg or load_candidates()
    pcfg = pcfg or config.pipeline_config()
    candidate = get_candidate(ccfg, candidate_name, server=server, quantization=quantization)
    settings = client_settings(ccfg, overrides, task=TASK)
    variant = variant or config.load("refine_dish")["default"]
    template, prompt_version = load_variant(pcfg, variant)
    cases = a2_cases(pcfg, config.load("evalsets"), split,
                     allow_test=split in ("test", "loo_full"))
    cases = sample(cases, limit, key=lambda c: c["cuisine"])
    run_id = run_id or new_run_id(TASK)
    client = RecordingClient(cached_client(ccfg, candidate, prompt_version, settings=settings,
                                           run_id=run_id))
    log.info("A2 %s (%s) on %s: %d dishes (run %s)", candidate.name, variant, split,
             len(cases), run_id)
    records = [evaluate_case(pcfg, case, client, template) for case in cases]
    per_dish_scores(pcfg, records)
    return finish(records, candidate=candidate, settings=settings, split=split,
                  variant=variant, prompt_version=prompt_version, run_id=run_id,
                  use_mlflow=use_mlflow, tags=tags, pcfg=pcfg)


def finish(records: list[dict[str, Any]], *, candidate: Candidate, settings: dict[str, Any],
           split: str, prompt_version: str, run_id: str, use_mlflow: bool,
           tags: dict[str, str] | None, pcfg: Config,
           variant: str | None = None) -> dict[str, Any]:
    folder = run_dir(TASK, run_id)
    params = _params(candidate, settings, split, prompt_version, len(records), variant)
    summary = {"task": TASK, "run_id": run_id, "params": params, "tags": tags or {},
               **summarize(pcfg, records)}
    write_jsonl(folder / "dishes.jsonl", records)
    write_json(folder / "summary.json", summary)
    if use_mlflow:
        summary["mlflow_run_id"] = log_run(
            task=TASK, candidate=candidate, prompt_version=prompt_version, split=split,
            run_id=run_id, params=params, summary=summary, folder=folder, tags=tags)
        write_json(folder / "summary.json", summary)
    log.info("A2 run %s: recall %s, core precision %s, valid %s -> %s", run_id,
             summary["recall"], summary["core_precision"], summary["valid_profile_rate"], folder)
    return summary


def _params(candidate: Candidate, settings: dict[str, Any], split: str, prompt_version: str,
            n: int, variant: str | None = None) -> dict[str, Any]:
    params = {"candidate": candidate.name, "server": candidate.server, "model": candidate.model,
              "quantization": candidate.quantization, "params_b": candidate.params_b,
              "temperature": settings["temperature"], "think": settings["think"],
              "prompt_version": prompt_version, "split": split, "cases": n}
    if variant:
        params["prompt_variant"] = variant
    if split == "loo":
        params["held_out_excluded"] = True  # the golden test dishes are left out
    return params


def main(argv: list[str] | None = None) -> int:
    rescoring = argparse.ArgumentParser(add_help=False)
    rescoring.add_argument("--rescore", type=Path, nargs="+")
    known, _ = rescoring.parse_known_args(argv)
    if known.rescore:  # no model, so no --candidate
        setup_logging()
        for folder in known.rescore:
            s = rescore(folder)
            print(json.dumps({"run_id": s["run_id"], "recall": s["recall"],
                              "core_precision": s["core_precision"]}))
        return 0
    parser = argparse.ArgumentParser(prog="pantrypath_ml.evaluate_a2",
                                     description=__doc__.splitlines()[0])
    add_model_args(parser)
    parser.add_argument("--split", choices=["dev", "loo", "loo_full", "test"], default="dev",
                        help="test and loo_full are held out: gates and final results only")
    parser.add_argument("--variant", default=None, help="prompt variant (configs/"
                        "refine_dish.yaml; default refine_dish_unknown)")
    parser.add_argument("--rescore", type=Path, nargs="+",
                        help="score these run folders' saved profiles again (no model calls)")
    args = parser.parse_args(argv)
    setup_logging()
    try:
        summary = run(args.candidate, args.split, variant=args.variant, limit=args.limit,
                      server=args.server,
                      quantization=args.quantization,
                      overrides={"temperature": args.temperature, "think": args.think},
                      run_id=args.run_id, use_mlflow=not args.no_mlflow)
    except PipelineError as exc:
        log.error("A2 evaluation failed: %s", exc)
        return 1
    print(json.dumps({k: v for k, v in summary.items() if k != "slices"}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
