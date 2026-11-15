"""A3 evaluation: grounded answers on synthetic result.json files (evalsets/a3_*.jsonl).

There are no reference answers: every metric is a rule that result.json decides, scored
on the model's own reply before any repair (the repaired answer always passes):
- valid_json_rate: a usable reply (after at most one retry);
- grounded_rate: the reply broke no rule at all;
- verdict_safety_rate: no "buy" without a safe product, no product that is not safe to
  buy (or at a store result.json doesn't list it for), no non-safe product named in the
  text (the safety metric);
- text_safety_rate: the summary and steps name no avoided allergen and nothing the diet
  excludes (outside bought ingredients, substitutes and avoid phrases);
- substitute_safety_rate: every substitute passed the allergen and diet screen;
- coverage_rate: every ingredient of result.json exactly once, none invented;
- missing_handled_rate: of the ingredients with no safe product, the share given a
  substitute, a label check or (common ones) a skip;
- quantity_rate: share of buy and substitute items with a quantity; steps_rate: replies
  with at least 2 steps;
- final_clean_rate: of the usable replies, the share whose shown answer passes the check
  (1.0 unless the repair has a bug).

    python -m pantrypath_ml.evaluate_a3 --candidate qwen3-4b --split dev|test \
        [--variant hybrid|model_list] [--limit N]
    python -m pantrypath_ml.evaluate_a3 --rescore reports/a3/<run> [reports/a3/<run> ...]

--variant: hybrid (answer/2, the default: code decides the list, the model writes the
text) or model_list (answer/1: the model writes the whole answer). Every report records
it (params.prompt_variant). --limit takes a seeded sample spread over cuisines. --rescore
checks a run's saved replies again with the current check and repair, without calling the
model (see rescore()).
"""

import argparse
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pantrypath_ml import config
from pantrypath_ml.answer import (
    Facts,
    SubstituteCheck,
    SubstituteTable,
    answer,
    check,
    load_prompt,
    load_table,
    repair,
    variant_name,
    variant_of_version,
    warnings,
)
from pantrypath_ml.evalsets import load_a3
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
from pantrypath_pipeline.errors import PipelineError
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger(__name__)
TASK = "a3"
SAFETY_KINDS = {"buy_without_safe", "product", "store", "text_product"}
COVERAGE_KINDS = {"invented", "missing", "duplicate"}
RATES = ("valid_json_rate", "grounded_rate", "verdict_safety_rate", "substitute_safety_rate",
         "text_safety_rate", "coverage_rate", "steps_rate")


def prompt_version(acfg: Config, variant: str | None = None) -> str:
    return load_prompt(acfg, variant)[1]


def score_case(case: dict[str, Any], out: dict[str, Any],
               screen: SubstituteCheck) -> dict[str, Any]:
    facts = Facts.of(case["result"])
    valid = out["status"] != "invalid"
    kinds = {v["kind"] for v in out["violations"]}
    raw = out["raw"] or {"ingredients": [], "steps": []}
    by_name = {i["name"].strip().lower(): i for i in raw["ingredients"]}
    missing = [n for n in facts.roles if not facts.safe[n]]
    handled = 0
    for name in missing:
        item = by_name.get(name.lower())
        if item and ((item["status"] == "substitute" and item.get("substitute"))
                     or item["status"] == "check_label"
                     or (item["status"] == "skip" and facts.roles[name] == "common")):
            handled += 1
    quantified = [i for i in raw["ingredients"] if i["status"] in ("buy", "substitute")]
    return {
        "valid_json_rate": valid,
        "grounded_rate": valid and not kinds,
        "verdict_safety_rate": valid and not kinds & SAFETY_KINDS,
        "substitute_safety_rate": valid and "substitute" not in kinds,
        "text_safety_rate": valid and "text_conflict" not in kinds,
        "coverage_rate": valid and not kinds & COVERAGE_KINDS,
        "steps_rate": valid and len(raw["steps"]) >= 2,
        "final_clean_rate": valid and not check(out["answer"], facts, screen),
        "missing": len(missing), "missing_handled": handled if valid else 0,
        "quantity_items": len(quantified),
        "quantified": sum(i.get("quantity") is not None for i in quantified),
        "violation_kinds": sorted(kinds),
    }


def summarize(records: list[dict[str, Any]], *, with_slices: bool = True) -> dict[str, Any]:
    n = len(records)
    scores = [r["score"] for r in records]
    summary: dict[str, Any] = {"cases": n, **{k: ratio(sum(s[k] for s in scores), n)
                                             for k in RATES}}
    # over usable replies: an invalid one shows no answer (valid_json_rate counts it)
    valid = [s for s in scores if s["valid_json_rate"]]
    summary["final_clean_rate"] = ratio(sum(s["final_clean_rate"] for s in valid), len(valid))
    summary["missing_handled_rate"] = ratio(sum(s["missing_handled"] for s in scores),
                                            sum(s["missing"] for s in scores))
    summary["quantity_rate"] = ratio(sum(s["quantified"] for s in scores),
                                     sum(s["quantity_items"] for s in scores))
    summary["repaired_rate"] = ratio(sum(r["result"]["status"] == "repaired" for r in records), n)
    kinds: dict[str, int] = {}
    for s in scores:
        for k in s["violation_kinds"]:
            kinds[k] = kinds.get(k, 0) + 1
    summary["violations_by_kind"] = dict(sorted(kinds.items()))
    summary.update(usage_stats(records))
    if with_slices:
        summary["slices"] = {
            name: {v: summarize([r for r in records if str(r["slices"][name]) == v],
                                with_slices=False)
                   for v in sorted({str(r["slices"][name]) for r in records})}
            for name in ("cuisine", "diet", "avoid_count", "without_safe")}
    return summary


def recheck(acfg: Config, case: dict[str, Any], out: dict[str, Any],
            screen: SubstituteCheck) -> dict[str, Any]:
    """A saved answer() result checked and repaired again. With the attempt history, a
    first reply that now passes is kept as answer() would have (no retry); otherwise, and
    for runs saved before the history, the last usable reply (`raw`) is checked."""
    facts = Facts.of(case["result"])
    first = (out.get("history") or [{}])[0].get("reply")
    if first is not None and not check(first, facts, screen):
        reply = first
    elif out["raw"] is not None:
        reply = out["raw"]
    else:
        return out
    violations = check(reply, facts, screen)
    final = repair(reply, facts, screen) if violations else reply
    error = "; ".join(f"{v.ingredient or 'text'}: {v.detail}" for v in violations)[:800]
    return {**out, "status": "repaired" if violations else "ok", "answer": final, "raw": reply,
            "warnings": warnings(acfg, facts, case.get("extras") or {}, final),
            "violations": [asdict(v) for v in violations], "error": error or None}


def rescore(folder: Path) -> dict[str, Any]:
    """Check and score a run's saved replies again with the current check, repair and
    screen, without calling the model (see recheck). A run saved before the attempt
    history has only each case's last usable reply: a reply the old check sent to the
    retry is not replayed, the retry's reply is the one scored, and the summary says so.
    The first rescore keeps the old summary as summary.before-rescore.json; MLflow keeps
    the run's original metrics."""
    acfg = config.load("answer")
    screen = SubstituteCheck(acfg, config.load("parse_request"))
    records = read_jsonl(folder / "cases.jsonl")
    old = json.loads((folder / "summary.json").read_text())
    split = old["params"]["split"]
    cases = {c["case_id"]: c for c in load_a3(config.load("evalsets"), split,
                                              allow_test=split == "test")}
    backup = folder / "summary.before-rescore.json"
    if not backup.is_file():
        write_json(backup, old)
    with_history = all("history" in r["result"] for r in records)
    retried = sum(r["result"]["attempts"] > 1 for r in records)
    for r in records:
        case = cases[r["case_id"]]
        r["result"] = recheck(acfg, case, r["result"], screen)
        r["score"] = score_case(case, r["result"], screen)
    keep = {k: old[k] for k in ("task", "run_id", "params", "tags", "mlflow_run_id") if k in old}
    if "prompt_variant" not in keep["params"]:   # a run from before the variants
        keep["params"] = {**keep["params"], "prompt_variant": variant_of_version(
            acfg, keep["params"].get("prompt_version"))}
    basis = ("every attempt (attempt history)" if with_history else
             f"each case's last usable reply only: this run was saved before the attempt "
             f"history, so for the {retried} cases with a retry, the retry's reply is scored "
             "even where the first reply might now pass")
    summary = {**keep, **summarize(records),
               "rescored": {"at": datetime.now(UTC).isoformat(timespec="seconds"),
                            "from": basis, "history": with_history,
                            "retried_cases": retried}}
    write_jsonl(folder / "cases.jsonl", records)
    write_json(folder / "summary.json", summary)
    log.info("A3 run %s rescored: grounded %s -> %s, text safety %s -> %s, repaired %s -> %s",
             folder.name, old.get("grounded_rate"), summary["grounded_rate"],
             old.get("text_safety_rate"), summary["text_safety_rate"],
             old.get("repaired_rate"), summary["repaired_rate"])
    return summary


def run(candidate_name: str, split: str, *, limit: int | None = None,
        server: str | None = None, quantization: str | None = None,
        overrides: dict[str, Any] | None = None, run_id: str | None = None,
        use_mlflow: bool = True, tags: dict[str, str] | None = None,
        cases: list[dict[str, Any]] | None = None, variant: str | None = None,
        ccfg: Config | None = None) -> dict[str, Any]:
    """Evaluate one setting on one split; writes cases.jsonl and summary.json."""
    ccfg = ccfg or load_candidates()
    acfg = config.load("answer")
    variant = variant_name(acfg, variant)
    candidate = get_candidate(ccfg, candidate_name, server=server, quantization=quantization)
    settings = client_settings(ccfg, {**acfg["defaults"], **(overrides or {})}, task=TASK)
    if cases is None:
        cases = load_a3(config.load("evalsets"), split, allow_test=split == "test")
    cases = sample(cases, limit, key=lambda c: c["slices"]["cuisine"])
    template, version = load_prompt(acfg, variant)
    run_id = run_id or new_run_id(TASK)
    # cached: the evaluation results are synthetic (no Kroger data)
    client = RecordingClient(cached_client(ccfg, candidate, version, settings=settings,
                                           run_id=run_id))
    screen = SubstituteCheck(acfg, config.load("parse_request"))
    table = SubstituteTable(load_table(acfg))
    log.info("A3 %s (%s) on %s: %d cases (run %s)", candidate.name, variant, split,
             len(cases), run_id)
    records = []
    for case in cases:
        out = answer(acfg, case["result"], client, extras=case.get("extras"), screen=screen,
                     template=template, table=table, variant=variant).to_dict()
        records.append({"case_id": case["case_id"], "dish_id": case["dish_id"],
                        "split": case["split"], "slices": case["slices"], "result": out,
                        "score": score_case(case, out, screen), **client.take()})
    return finish(records, candidate=candidate, settings=settings, split=split,
                  prompt_version=version, run_id=run_id, use_mlflow=use_mlflow, tags=tags,
                  variant=variant)


def finish(records: list[dict[str, Any]], *, candidate: Candidate, settings: dict[str, Any],
           split: str, prompt_version: str, run_id: str, use_mlflow: bool,
           tags: dict[str, str] | None, variant: str) -> dict[str, Any]:
    folder = run_dir(TASK, run_id)
    params = {"candidate": candidate.name, "server": candidate.server, "model": candidate.model,
              "quantization": candidate.quantization, "params_b": candidate.params_b,
              "temperature": settings["temperature"], "think": settings["think"],
              "prompt_version": prompt_version, "prompt_variant": variant, "split": split,
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
    log.info("A3 run %s: verdict safety %s, grounded %s, substitutes %s -> %s", run_id,
             summary["verdict_safety_rate"], summary["grounded_rate"],
             summary["substitute_safety_rate"], folder)
    return summary


def main(argv: list[str] | None = None) -> int:
    rescoring = argparse.ArgumentParser(add_help=False)
    rescoring.add_argument("--rescore", type=Path, nargs="+")
    known, _ = rescoring.parse_known_args(argv)
    if known.rescore:  # no model, so no --candidate
        setup_logging()
        for folder in known.rescore:
            s = rescore(folder)
            print(json.dumps({"run_id": s["run_id"], **{k: s[k] for k in (
                *RATES, "final_clean_rate", "missing_handled_rate", "repaired_rate")}}))
        return 0
    parser = argparse.ArgumentParser(prog="pantrypath_ml.evaluate_a3",
                                     description=__doc__.splitlines()[0])
    add_model_args(parser)
    parser.add_argument("--split", choices=["dev", "test"], default="dev",
                        help="test is held out: gates and final results only")
    parser.add_argument("--variant", default=None,
                        help="hybrid (answer/2, default) or model_list (answer/1)")
    parser.add_argument("--rescore", type=Path, nargs="+",
                        help="check these run folders' saved replies again (no model calls)")
    args = parser.parse_args(argv)
    setup_logging()
    try:
        summary = run(args.candidate, args.split, limit=args.limit, server=args.server,
                      quantization=args.quantization,
                      overrides={"temperature": args.temperature, "think": args.think},
                      run_id=args.run_id, use_mlflow=not args.no_mlflow,
                      variant=args.variant)
    except PipelineError as exc:
        log.error("A3 evaluation failed: %s", exc)
        return 1
    print(json.dumps({k: v for k, v in summary.items() if k != "slices"}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
