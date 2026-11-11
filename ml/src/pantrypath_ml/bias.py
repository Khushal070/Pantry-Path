"""Bias slicing with Fairlearn's MetricFrame on per-case evaluation results.

A2 slices: cuisine and popularity tier (recall, core precision, valid-profile rate).
A1 slices: phrasing style and diet (allergens captured, allergen exact match), and
allergen (each expected allergen as one row: captured, found by the model).

The gap of a metric is the largest difference between two groups of a slice. Groups with
fewer than min_group_size items are listed but left out of the gap, because a group of a
few dishes swings by tens of points on one answer. The bias gate (gates.yaml) uses the
largest gap of each task's main metric.

    python -m pantrypath_ml.bias --a1 reports/a1/<run_id> --a2 reports/a2/<run_id>
"""

import argparse
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fairlearn.metrics import MetricFrame, count

from pantrypath_ml import config
from pantrypath_ml.charts import bar_chart, markdown_table, pct
from pantrypath_ml.reporting import read_jsonl, write_json
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger(__name__)


def ratio_of_sums(y_true: Any, y_pred: Any) -> float:
    """sum(pred) / sum(true): recall = found / required, precision = in reference / core."""
    total = float(np.sum(y_true))
    return float(np.sum(y_pred)) / total if total else float("nan")


def mean_pred(y_true: Any, y_pred: Any) -> float:
    return float(np.mean(y_pred)) if len(y_pred) else float("nan")


# metric -> (row filter, y_true column, y_pred column, metric function)
Spec = tuple[Callable[[pd.DataFrame], pd.Series] | None, str, str, Callable[..., float]]

A2_SPECS: dict[str, Spec] = {
    "recall": (lambda d: d["valid"] == 1, "required", "found", ratio_of_sums),
    "core_precision": (lambda d: d["valid"] == 1, "core", "core_in_reference", ratio_of_sums),
    "valid_profile_rate": (None, "one", "valid", mean_pred),
}
A1_SPECS: dict[str, Spec] = {
    "allergens_captured": (None, "one", "captured", mean_pred),
    "allergen_exact": (None, "one", "exact", mean_pred),
}
A1_ALLERGEN_SPECS: dict[str, Spec] = {
    "allergens_captured": (None, "one", "captured", mean_pred),
    "allergen_model_recall": (None, "one", "model", mean_pred),
}


def a2_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for r in records:
        s = r.get("score") or {}
        rows.append({"cuisine": str(r.get("cuisine")), "popularity": str(r.get("popularity")),
                     "one": 1, "valid": int(r["status"] == "valid"),
                     "required": s.get("required", 0), "found": s.get("found", 0),
                     "core": s.get("core", 0), "core_in_reference": s.get("core_in_reference", 0)})
    return pd.DataFrame(rows)


def a1_frames(records: list[dict[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(one row per case, one row per expected allergen of a case)."""
    cases = pd.DataFrame([{
        "style": r["slices"]["style"], "diet": r["slices"]["diet"], "one": 1,
        "captured": int(r["score"]["allergens_captured"]),
        "exact": int(r["score"]["allergen_exact"]),
    } for r in records])
    allergens = pd.DataFrame([{"allergen": a, "one": 1, "captured": int(hit["captured"]),
                               "model": int(hit["model"])}
                              for r in records for a, hit in r["score"]["per_allergen"].items()],
                             columns=["allergen", "one", "captured", "model"])
    return cases, allergens


def slice_metric(df: pd.DataFrame, spec: Spec, feature: str,
                 min_group_size: int) -> dict[str, Any]:
    """Per-group value and size, and the gap over the groups big enough to count."""
    keep, y_true, y_pred, fn = spec
    data = df[keep(df)] if keep is not None and len(df) else df
    if data.empty:
        return {"groups": {}, "gap": None, "gap_between": None, "too_small": []}
    frame = MetricFrame(metrics={"value": fn, "n": count}, y_true=data[y_true],
                        y_pred=data[y_pred], sensitive_features=data[feature])
    groups = {str(g): {"value": None if np.isnan(row["value"]) else round(float(row["value"]), 4),
                       "n": int(row["n"])}
              for g, row in frame.by_group.iterrows()}
    counted = {g: v["value"] for g, v in groups.items()
               if v["n"] >= min_group_size and v["value"] is not None}
    gap = between = None
    if len(counted) >= 2:
        ordered = sorted(counted, key=lambda g: (counted[g], g))
        low, high = ordered[0], ordered[-1]
        gap, between = round(counted[high] - counted[low], 4), [low, high]
    return {"groups": groups, "gap": gap, "gap_between": between,
            "too_small": sorted(g for g, v in groups.items() if v["n"] < min_group_size)}


def analyze(task: str, records: list[dict[str, Any]], min_group_size: int) -> dict[str, Any]:
    """{metric: {slice: slice_metric}} plus the largest gap per metric."""
    if task == "a2":
        df = a2_frame(records)
        plan = [(m, spec, df, f) for m, spec in A2_SPECS.items() for f in ("cuisine", "popularity")]
    else:
        cases, allergens = a1_frames(records)
        plan = [(m, spec, cases, f) for m, spec in A1_SPECS.items() for f in ("style", "diet")]
        plan += [(m, spec, allergens, "allergen") for m, spec in A1_ALLERGEN_SPECS.items()]
    metrics: dict[str, dict[str, Any]] = {}
    for metric, spec, df, feature in plan:
        metrics.setdefault(metric, {})[feature] = slice_metric(df, spec, feature, min_group_size)
    largest = {}
    for metric, by_slice in metrics.items():
        gaps = [(v["gap"], s) for s, v in by_slice.items() if v["gap"] is not None]
        gap, where = max(gaps) if gaps else (None, None)
        largest[metric] = {"gap": gap, "slice": where,
                           "between": by_slice[where]["gap_between"] if where else None}
    return {"task": task, "cases": len(records), "min_group_size": min_group_size,
            "metrics": metrics, "largest_gap": largest}


def render(result: dict[str, Any], folder: Path) -> str:
    task = result["task"].upper()
    lines = [f"## {task}: slices", "",
             f"{result['cases']} cases. Groups under {result['min_group_size']} items are "
             "shown but not counted in the gap.", ""]
    lines.append(markdown_table(
        ["Metric", "Largest gap", "Slice", "Between"],
        [[m, pct(v["gap"]), v["slice"] or "n/a", " vs ".join(v["between"] or []) or "n/a"]
         for m, v in result["largest_gap"].items()]))
    for metric, by_slice in result["metrics"].items():
        for feature, s in by_slice.items():
            if not s["groups"]:
                continue
            names = list(s["groups"])
            chart = bar_chart(folder / f"{result['task']}_{metric}_{feature}.png",
                              f"{task} {metric} by {feature}",
                              [f"{g} (n={s['groups'][g]['n']})" for g in names],
                              {metric: [s["groups"][g]["value"] for g in names]})
            lines += ["", f"### {metric} by {feature}", "", f"![{metric} by {feature}]"
                      f"({chart.name})", "",
                      markdown_table(["Group", "Value", "n"], [[g, pct(v["value"]), v["n"]]
                                                               for g, v in s["groups"].items()])]
    return "\n".join(lines) + "\n"


def run(a1_dir: Path | None, a2_dir: Path | None, *, out: Path | None = None,
        min_group_size: int | None = None) -> dict[str, Any]:
    gates = config.load("gates")
    size = int(min_group_size or gates["bias"]["min_group_size"])
    name = "+".join(p.name for p in (a1_dir, a2_dir) if p)
    folder = out or config.REPORTS / "bias" / name
    folder.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    md = ["# Bias report", ""]
    for task, run_folder, file in (("a1", a1_dir, "cases.jsonl"), ("a2", a2_dir, "dishes.jsonl")):
        if run_folder:
            result = analyze(task, read_jsonl(Path(run_folder) / file), size)
            result["run"] = str(run_folder)
            report[task] = result
            md.append(render(result, folder))
    write_json(folder / "bias.json", report)
    (folder / "bias.md").write_text("\n".join(md))
    log.info("Bias report -> %s", folder)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantrypath_ml.bias",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--a1", type=Path, help="an A1 run folder (reports/a1/<run_id>)")
    parser.add_argument("--a2", type=Path, help="an A2 run folder (reports/a2/<run_id>)")
    parser.add_argument("--min-group-size", type=int)
    args = parser.parse_args(argv)
    if not (args.a1 or args.a2):
        parser.error("give --a1 and/or --a2")
    setup_logging()
    report = run(args.a1, args.a2, min_group_size=args.min_group_size)
    print(json.dumps({t: r["largest_gap"] for t, r in report.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
