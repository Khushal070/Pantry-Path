"""Collect one search's runs into a comparison table and charts (reports/search/<id>/).

Each row is one setting with its dev metrics, its largest slice gap on the main metric
and whether it passes the validation and bias gates. For A2, a setting's loo run (if the
grid has one) supplies the bias gap, since 100 dishes slice better than 16.
With --log-mlflow every run is logged to MLflow from this one process (the Explorer
path: array tasks run with --no-mlflow).

    python -m pantrypath_ml.aggregate --search-id ID [--log-mlflow]
"""

import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Any

from pantrypath_ml import bias, config
from pantrypath_ml.charts import bar_chart, markdown_table, num, pct
from pantrypath_ml.local_llm import Candidate
from pantrypath_ml.reporting import log_run, read_jsonl, write_json
from pantrypath_ml.select import gate
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import DataUnavailableError
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger(__name__)

A1_COLUMNS = ["allergens_captured", "allergen_exact", "allergen_precision",
              "extra_allergen_rate", "diet_captured", "diet_accuracy", "dish_resolution",
              "zip_accuracy", "valid_json_rate", "unsupported_recall", "crosscheck_catch_rate",
              "false_confirmation_rate", "latency_mean_s", "latency_p95_s"]
A2_COLUMNS = ["recall", "core_precision", "valid_profile_rate", "false_refusal_rate",
              "error_rate", "latency_mean_s", "latency_p95_s"]
SETTING = ["candidate", "quantization", "server", "temperature", "think"]
RECORDS = {"a1": "cases.jsonl", "a2": "dishes.jsonl"}


def search_runs(search_id: str) -> list[tuple[Path, dict[str, Any]]]:
    """(folder, summary) of every A1/A2 run tagged with this search id."""
    found = []
    for task in ("a1", "a2"):
        for path in sorted((config.REPORTS / task).glob("*/summary.json")):
            summary = json.loads(path.read_text())
            if summary.get("tags", {}).get("search_id") == search_id:
                found.append((path.parent, summary))
    if not found:
        raise DataUnavailableError(f"No runs found for search {search_id} under {config.REPORTS}")
    return found


def setting_key(summary: dict[str, Any]) -> tuple:
    p = summary["params"]
    return (summary["task"], *(p[k] for k in SETTING), p.get("prompt_variant"))


def rows(runs: list[tuple[Path, dict[str, Any]]], gates: Config) -> list[dict[str, Any]]:
    size = int(gates["bias"]["min_group_size"])
    by_setting: dict[tuple, dict[str, tuple[Path, dict[str, Any]]]] = {}
    for folder, summary in runs:
        by_setting.setdefault(setting_key(summary), {})[summary["params"]["split"]] = (
            folder, summary)
    out = []
    for key, splits in by_setting.items():
        task = key[0]
        if "dev" not in splits:
            continue
        folder, summary = splits["dev"]
        bias_folder, _ = splits.get("loo", splits["dev"])
        result = bias.analyze(task, read_jsonl(bias_folder / RECORDS[task]), size)
        main = gates["bias"]["main_metric"][task]
        verdict = gate(task, summary, result["largest_gap"][main]["gap"], gates)
        p = summary["params"]
        columns = A1_COLUMNS if task == "a1" else A2_COLUMNS
        out.append({
            "task": task, **{k: p[k] for k in SETTING}, "model": p["model"],
            "params_b": p["params_b"], "prompt_variant": p.get("prompt_variant"),
            "prompt_version": p["prompt_version"], "cases": summary["cases"],
            **{c: summary.get(c) for c in columns},
            "bias_gap": result["largest_gap"][main]["gap"],
            "bias_split": "loo" if "loo" in splits else "dev",
            "passed": verdict["passed"], "failures": verdict["failures"],
            "run_id": summary["run_id"], "folder": str(folder),
        })
    return sorted(out, key=lambda r: (r["task"], -int(r["passed"]), r["run_id"]))


def label(row: dict[str, Any]) -> str:
    variant = f" {row['prompt_variant']}" if row["prompt_variant"] else ""
    think = "think" if row["think"] else "no think"
    return f"{row['quantization']}{variant} t={row['temperature']:g} {think}"


def charts(table: list[dict[str, Any]], folder: Path) -> list[Path]:
    made = []
    for task, metrics in (("a1", ["allergens_captured", "allergen_exact", "dish_resolution"]),
                          ("a2", ["recall", "core_precision", "valid_profile_rate"])):
        part = [r for r in table if r["task"] == task]
        if not part:
            continue
        labels = [label(r) for r in part]
        made.append(bar_chart(folder / f"{task}_quality.png", f"{task.upper()} on dev, by setting",
                              labels, {m: [r[m] for r in part] for m in metrics}))
        made.append(bar_chart(folder / f"{task}_latency.png",
                              f"{task.upper()} latency per item (mean, seconds)", labels,
                              {"latency_mean_s": [r["latency_mean_s"] for r in part]},
                              xmax=max((r["latency_mean_s"] or 0) for r in part) or 1,
                              fmt="{:.1f}s"))
    return made


def render(search_id: str, table: list[dict[str, Any]], made: list[Path]) -> str:
    lines = [f"# Search {search_id}: comparison", "",
             "Dev-split metrics per setting. **Passes** = validation and bias gates "
             "(configs/gates.yaml). Bias gap = largest gap between slices on the main metric.",
             ""]
    for task, metrics in (("a1", ["allergens_captured", "allergen_exact", "diet_captured",
                                  "dish_resolution", "valid_json_rate", "unsupported_recall"]),
                          ("a2", ["recall", "core_precision", "valid_profile_rate",
                                  "false_refusal_rate"])):
        part = [r for r in table if r["task"] == task]
        if not part:
            continue
        lines += [f"## {task.upper()}", "", markdown_table(
            ["Setting", *metrics, "Latency", "Bias gap", "Passes"],
            [[label(r), *(pct(r[m]) for m in metrics), num(r["latency_mean_s"], " s"),
              pct(r["bias_gap"]), "yes" if r["passed"] else "no: " + "; ".join(r["failures"])]
             for r in part]), ""]
    lines += [f"![{p.stem}]({p.name})" for p in made]
    return "\n".join(lines) + "\n"


def log_to_mlflow(runs: list[tuple[Path, dict[str, Any]]], ccfg: Config) -> int:
    logged = 0
    for folder, summary in runs:
        if summary.get("mlflow_run_id"):
            continue
        p = summary["params"]
        candidate = Candidate(p["candidate"], p["server"], p["model"], p["quantization"],
                              p["params_b"])
        summary["mlflow_run_id"] = log_run(
            task=summary["task"], candidate=candidate, prompt_version=p["prompt_version"],
            split=p["split"], run_id=summary["run_id"], params=p, summary=summary,
            folder=folder, tags=summary.get("tags"))
        write_json(folder / "summary.json", summary)
        logged += 1
    return logged


def run(search_id: str, *, use_mlflow: bool = False) -> dict[str, Any]:
    runs = search_runs(search_id)
    gates = config.load("gates")
    if use_mlflow:
        log.info("Logged %d runs to MLflow", log_to_mlflow(runs, config.load("candidates")))
    table = rows(runs, gates)
    folder = config.REPORTS / "search" / search_id
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "comparison.json", {"search_id": search_id, "rows": table})
    with open(folder / "comparison.csv", "w", newline="") as f:
        fields = sorted({k for r in table for k in r})
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(table)
    made = charts(table, folder)
    (folder / "comparison.md").write_text(render(search_id, table, made))
    log.info("Search %s: %d settings, %d pass -> %s", search_id, len(table),
             sum(r["passed"] for r in table), folder)
    return {"search_id": search_id, "settings": len(table),
            "passing": sum(r["passed"] for r in table), "folder": str(folder)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantrypath_ml.aggregate",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--search-id", required=True)
    parser.add_argument("--log-mlflow", action="store_true",
                        help="log runs that were run with --no-mlflow")
    args = parser.parse_args(argv)
    setup_logging()
    print(json.dumps(run(args.search_id, use_mlflow=args.log_mlflow)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
