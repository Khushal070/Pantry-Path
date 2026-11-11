"""Sensitivity: A1 perturbation tests, and metric curves across the searched settings.

perturb: the same A1 dev cases asked as written and four ways changed (paraphrase, one
typo, the allergy moved to the end, sentences shuffled); the expected answer stays the
same, so any drop is sensitivity to wording.
curves: from a search's comparison table, the main metrics against temperature,
quantization (bits), few-shot example count and thinking. Several settings share an x
value, so each point is their mean.

    python -m pantrypath_ml.sensitivity perturb --candidate qwen3-4b [--variant base] --limit 40
    python -m pantrypath_ml.sensitivity curves --search-id ID
"""

import argparse
import copy
import json
import logging
import random
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pantrypath_ml import config, evaluate_a1
from pantrypath_ml.charts import bar_chart, line_chart, markdown_table, pct
from pantrypath_ml.evalsets import load_a1, typo
from pantrypath_ml.reporting import add_model_args, write_json
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger(__name__)

PERTURBATIONS = ("paraphrase", "typo", "late", "reorder")
METRICS = ("allergens_captured", "allergen_exact", "diet_accuracy", "dish_resolution",
           "valid_json_rate")
ALLERGY_SENTENCE = re.compile(r"allerg|\bno |free|can't have|leave out|celiac|coeliac", re.I)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.?!])\s+", text.strip()) if s]


def perturb(case: dict[str, Any], kind: str, rng: random.Random,
            paraphrases: dict[str, str]) -> dict[str, Any]:
    """A copy of the case with its user messages changed; the expected answer is unchanged."""
    out = copy.deepcopy(case)
    out["case_id"] = f"{case['case_id']}~{kind}"
    out["slices"] = {**case["slices"], "perturbation": kind}
    users = [m for m in out["messages"] if m["role"] == "user"]
    first = users[0]
    if kind == "paraphrase":
        for m in users:
            for old, new in paraphrases.items():
                m["content"] = re.sub(re.escape(old), new, m["content"], flags=re.I)
    elif kind == "typo":
        m = rng.choice(users)
        words = m["content"].split()
        candidates = [i for i, w in enumerate(words) if len(w) >= 4 and not re.search(r"\d", w)]
        if candidates:
            i = rng.choice(candidates)
            words[i] = typo(words[i], rng)
            m["content"] = " ".join(words)
    elif kind == "late":
        parts = _sentences(first["content"])
        allergy = [s for s in parts if ALLERGY_SENTENCE.search(s)]
        first["content"] = " ".join([s for s in parts if s not in allergy] + allergy)
    elif kind == "reorder":
        parts = _sentences(first["content"])
        rng.shuffle(parts)
        first["content"] = " ".join(parts)
    else:
        raise ValueError(f"unknown perturbation {kind!r}")
    return out


def run_perturbations(candidate: str, *, variant: str, limit: int, server: str | None,
                      quantization: str | None, overrides: dict[str, Any],
                      use_mlflow: bool) -> dict[str, Any]:
    ecfg = config.load("evalsets")
    settings = ecfg["sensitivity"]
    cases = load_a1(ecfg, "dev")[:limit]
    rng = random.Random(settings["seed"])
    sens_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-perturb"
    variants = {"original": cases} | {k: [perturb(c, k, rng, settings["paraphrases"])
                                          for c in cases] for k in PERTURBATIONS}
    summaries = {}
    for name, items in variants.items():
        summaries[name] = evaluate_a1.run(
            candidate, "dev", variant=variant, cases=items, server=server,
            quantization=quantization, overrides=overrides, run_id=f"{sens_id}-{name}",
            use_mlflow=use_mlflow, tags={"sensitivity_id": sens_id, "perturbation": name})
    folder = config.REPORTS / "sensitivity" / sens_id
    table = {name: {m: s.get(m) for m in METRICS} for name, s in summaries.items()}
    chart = bar_chart(folder / "perturbations.png", "A1 dev metrics by perturbation",
                      list(table), {m: [table[n][m] for n in table] for m in METRICS[:3]})
    base = table["original"]
    rows = [[name, *(f"{pct(v[m])} ({_delta(v[m], base[m])})" for m in METRICS)]
            for name, v in table.items()]
    md = [f"# A1 perturbation test {sens_id}", "",
          f"{len(cases)} dev cases, candidate {candidate}, prompt variant {variant}. "
          "In brackets: change from the unperturbed cases.", "",
          markdown_table(["Cases", *METRICS], rows), "", f"![perturbations]({chart.name})"]
    (folder / "perturbations.md").write_text("\n".join(md) + "\n")
    write_json(folder / "perturbations.json", {"id": sens_id, "metrics": table})
    log.info("Perturbation test -> %s", folder)
    return {"id": sens_id, "metrics": table, "folder": str(folder)}


def _delta(value: float | None, base: float | None) -> str:
    if value is None or base is None:
        return "n/a"
    return f"{(value - base) * 100:+.1f} pp"


# ---------------------------------------------------------------- curves


def curve(rows: list[dict[str, Any]], x: Callable[[dict], float], group: Callable[[dict], str],
          metric: str) -> tuple[list[float], dict[str, list[float | None]]]:
    """Mean of `metric` per (group, x): the x values and one y list per group."""
    xs = sorted({x(r) for r in rows})
    series: dict[str, list[float | None]] = {}
    for name in sorted({group(r) for r in rows}):
        ys: list[float | None] = []
        for value in xs:
            hits = [r[metric] for r in rows
                    if group(r) == name and x(r) == value and r[metric] is not None]
            ys.append(round(sum(hits) / len(hits), 4) if hits else None)
        series[name] = ys
    return xs, series


def _think(row: dict[str, Any]) -> str:
    return "think" if row["think"] else "no think"


def _bits(row: dict[str, Any]) -> float:
    """'4bit' -> 4.0, so quantization can be an x axis."""
    return float(re.sub(r"\D", "", row["quantization"]) or 0)


def run_curves(search_id: str) -> dict[str, Any]:
    table = json.loads((config.REPORTS / "search" / search_id / "comparison.json")
                       .read_text())["rows"]
    examples = {name: int(v.get("examples") or 0)
                for name, v in config.load("parse_request")["variants"].items()}
    folder = config.REPORTS / "sensitivity" / search_id
    a1 = [r for r in table if r["task"] == "a1"]
    a2 = [r for r in table if r["task"] == "a2"]
    plans = [
        ("a1_temperature", a1, lambda r: r["temperature"], _think, "allergen_exact",
         "temperature"),
        ("a1_fewshot", a1, lambda r: examples.get(r["prompt_variant"], 0), _think,
         "allergen_exact", "few-shot examples"),
        ("a1_quantization", a1, _bits, _think, "allergen_exact", "quantization (bits)"),
        ("a2_temperature", a2, lambda r: r["temperature"], _think, "recall", "temperature"),
        ("a2_quantization", a2, _bits, _think, "recall", "quantization (bits)"),
    ]
    md = [f"# Sensitivity to settings, search {search_id}", "",
          "Each point is the mean over the settings that share its x value.", ""]
    made: list[Path] = []
    for name, rows, x, group, metric, xlabel in plans:
        if not rows:
            continue
        xs, series = curve(rows, x, group, metric)
        if len(xs) < 2:
            continue  # one value: nothing to draw a curve through
        made.append(line_chart(folder / f"{name}.png", f"{metric} vs {xlabel}", xs, series,
                               xlabel=xlabel, ylabel=metric))
        md += [f"## {metric} vs {xlabel}", "", f"![{name}]({name}.png)", "",
               markdown_table([xlabel, *series], [[x_, *(pct(series[s][i]) for s in series)]
                                                  for i, x_ in enumerate(xs)]), ""]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "curves.md").write_text("\n".join(md) + "\n")
    return {"search_id": search_id, "charts": [str(p) for p in made], "folder": str(folder)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantrypath_ml.sensitivity",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("perturb")
    add_model_args(p)
    p.add_argument("--variant", default="base")
    c = sub.add_parser("curves")
    c.add_argument("--search-id", required=True)
    args = parser.parse_args(argv)
    setup_logging()
    if args.command == "curves":
        print(json.dumps(run_curves(args.search_id)))
        return 0
    result = run_perturbations(args.candidate, variant=args.variant, limit=args.limit or 40,
                               server=args.server, quantization=args.quantization,
                               overrides={"temperature": args.temperature, "think": args.think},
                               use_mlflow=not args.no_mlflow)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
