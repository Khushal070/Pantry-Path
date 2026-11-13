"""What every evaluation run shares: run ids, report folders, latency stats, MLflow logging
and the command-line options that pick a model and its settings.
"""

import argparse
import json
import logging
import math
import random
import secrets
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pantrypath_ml import config
from pantrypath_pipeline.llm import write_json_atomic

log = logging.getLogger(__name__)


def new_run_id(prefix: str) -> str:
    """Sortable and unique: '20261110T150405Z-a2-3f9a1c'."""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{prefix}-{secrets.token_hex(3)}"


def run_dir(task: str, run_id: str) -> Path:
    path = config.REPORTS / task / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_json(path: Path, obj: Any) -> None:
    write_json_atomic(path, obj)


def sample(items: list[dict[str, Any]], limit: int | None, key: Callable[[dict], Any],
           seed: int = 0) -> list[dict[str, Any]]:
    """Up to `limit` items spread over the values key(item) gives (seeded), in their
    original order. Taking the first N would take them all from the first few dishes.

    key may return one value or a tuple of facets (each a value or a list of values, e.g.
    style, diet and allergens): each pick is the item whose values are least represented
    so far, ties going to a seeded random order.
    """
    if not limit or limit >= len(items):
        return list(items)
    facets = {i: _facets(key(item)) for i, item in enumerate(items)}
    order = list(range(len(items)))
    random.Random(seed).shuffle(order)
    counts: dict[tuple[int, str], int] = {}
    picked: list[int] = []
    for _ in range(limit):
        best = max((i for i in order if i not in picked),
                   key=lambda i: sum(1 / (1 + counts.get(f, 0)) for f in facets[i]))
        picked.append(best)
        for f in facets[best]:
            counts[f] = counts.get(f, 0) + 1
    return [items[i] for i in sorted(picked)]


def _facets(value: Any) -> list[tuple[int, str]]:
    parts = value if isinstance(value, tuple) else (value,)
    out = []
    for n, part in enumerate(parts):
        values = part if isinstance(part, list) else [part]
        out += [(n, str(v)) for v in values or ["none"]]
    return out


def ratio(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (numpy's default), None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo), 3)


def usage_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Latency (mean, p95), tokens and cache hits over per-item records."""
    latencies = [r["latency_s"] for r in records if r.get("calls")]
    return {
        "latency_mean_s": round(sum(latencies) / len(latencies), 3) if latencies else None,
        "latency_p95_s": percentile(latencies, 95),
        "llm_calls": sum(r.get("calls", 0) for r in records),
        "cached_calls": sum(r.get("cached_calls", 0) for r in records),
        "input_tokens": sum(r.get("input_tokens", 0) for r in records),
        "output_tokens": sum(r.get("output_tokens", 0) for r in records),
        # None for runs recorded before the count existed
        "cut_off_calls": sum(r.get("cut_off_calls", 0) for r in records)
        if any("cut_off_calls" in r for r in records) else None,
    }


def numeric_metrics(summary: dict[str, Any], prefix: str = "") -> dict[str, float]:
    """Flatten a summary's numbers into MLflow metric names ('slices.cuisine.american.recall')."""
    out: dict[str, float] = {}
    for key, value in summary.items():
        name = f"{prefix}{key}"
        if isinstance(value, bool | int | float):
            out[name] = float(value)
        elif isinstance(value, dict):
            out.update(numeric_metrics(value, f"{name}."))
    return {_metric_name(k): v for k, v in out.items()}


def _metric_name(name: str) -> str:
    """MLflow metric names allow letters, digits, _ - . / and spaces only."""
    return "".join(c if c.isalnum() or c in "_-./ " else "_" for c in name)


def log_run(*, task: str, candidate: Any, prompt_version: str, split: str, run_id: str,
            params: dict[str, Any], summary: dict[str, Any], folder: Path,
            tags: dict[str, str] | None = None) -> str:
    """Log one finished run (params, numeric summary, the report folder) to MLflow."""
    import mlflow

    from pantrypath_ml.registry import start_run

    with start_run(task=task, candidate=candidate, prompt_version=prompt_version, split=split,
                   run_name=run_id) as run:
        mlflow.set_tags({"run_id": run_id, **(tags or {})})
        mlflow.log_params({k: str(v) for k, v in params.items()})
        mlflow.log_metrics(numeric_metrics(summary))
        mlflow.log_artifacts(str(folder))
        return run.info.run_id


def add_model_args(parser: argparse.ArgumentParser) -> None:
    """--candidate, --server, --quantization, --temperature, --think/--no-think."""
    parser.add_argument("--candidate", required=True, help="a name in configs/candidates.yaml")
    parser.add_argument("--server", help="ollama | vllm (default: default_server)")
    parser.add_argument("--quantization", help="e.g. 4bit, 8bit (default: the candidate's)")
    parser.add_argument("--temperature", type=float, help="overrides client.temperature")
    parser.add_argument("--think", action=argparse.BooleanOptionalAction, default=None,
                        help="overrides client.think")
    parser.add_argument("--limit", type=int, help="evaluate only the first N items")
    parser.add_argument("--no-mlflow", action="store_true", help="write reports only")
    parser.add_argument("--run-id", help="report folder name (default: a new id)")
