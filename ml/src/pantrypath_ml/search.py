"""Candidate search over configs/search.yaml: one task per setting, A1 and A2 on dev.

Locally every task runs in turn against one Ollama server and logs to MLflow as it goes.
On Explorer each Slurm array task runs one task (run-one --index) with --no-mlflow, since
many writers on one SQLite file over a network filesystem is unsafe; aggregate.py logs
them all to MLflow afterwards from one process.

    python -m pantrypath_ml.search list --grid local_prompts
    python -m pantrypath_ml.search run --grid local_prompts [--search-id ID]
    python -m pantrypath_ml.search run-one --grid explorer --index 3 --search-id ID --no-mlflow
    python -m pantrypath_ml.search model --grid explorer --index 3   # id to serve for a task
"""

import argparse
import itertools
import json
import logging
from datetime import UTC, datetime
from typing import Any

from pantrypath_ml import config, evaluate_a1, evaluate_a2
from pantrypath_ml.local_llm import get_candidate, load_candidates
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import PipelineError
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger(__name__)


def tasks(scfg: Config, grid: str) -> list[dict[str, Any]]:
    """Every task of a grid, in a fixed order (the Slurm array index is a position here)."""
    if grid not in scfg["grids"]:
        raise PipelineError(f"Unknown grid {grid!r}; use one of {sorted(scfg['grids'])}")
    g = scfg["grids"][grid]
    common = list(itertools.product(g["candidates"], g["quantizations"], g["temperature"],
                                    g["think"]))
    out: list[dict[str, Any]] = []
    for task in ("a1", "a2"):
        for candidate, quant, temperature, think in common:
            setting = {"candidate": candidate, "quantization": quant,
                       "temperature": float(temperature), "think": bool(think),
                       "limit": g["limit"][task]}
            if task == "a1":
                out += [{"task": task, "split": g["a1_split"], "prompt_variant": v, **setting}
                        for v in g["a1_prompt_variants"]]
            else:
                out += [{"task": task, "split": s, "prompt_variant": v, **setting}
                        for v in g["a2_prompt_variants"] for s in g["a2_splits"]]
    for i, t in enumerate(out):
        t.update(index=i, server=g["server"], grid=grid)
    return out


def task_name(t: dict[str, Any]) -> str:
    variant = f"-{t['prompt_variant']}" if t["prompt_variant"] else ""
    return (f"{t['index']:03d}-{t['task']}-{t['split']}-{t['candidate']}-{t['quantization']}"
            f"{variant}-t{t['temperature']:g}-{'think' if t['think'] else 'nothink'}")


def run_task(t: dict[str, Any], search_id: str, *, use_mlflow: bool) -> dict[str, Any]:
    run_id = f"{search_id}-{task_name(t)}"
    tags = {"search_id": search_id, "grid": t["grid"], "task_index": str(t["index"])}
    common = dict(limit=t["limit"], server=t["server"], quantization=t["quantization"],
                  overrides={"temperature": t["temperature"], "think": t["think"]},
                  run_id=run_id, use_mlflow=use_mlflow, tags=tags)
    if t["task"] == "a1":
        return evaluate_a1.run(t["candidate"], t["split"], variant=t["prompt_variant"], **common)
    return evaluate_a2.run(t["candidate"], t["split"], variant=t["prompt_variant"], **common)


def new_search_id(grid: str) -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{grid}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantrypath_ml.search",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["list", "count", "model", "run", "run-one"])
    parser.add_argument("--grid", default="local_prompts")
    parser.add_argument("--index", type=int, help="run-one: the task's position")
    parser.add_argument("--search-id", help="groups the runs of one search (default: new)")
    parser.add_argument("--server", help="override the grid's server (e.g. ollama fallback)")
    parser.add_argument("--no-mlflow", action="store_true")
    args = parser.parse_args(argv)
    setup_logging()
    all_tasks = tasks(config.load("search"), args.grid)
    if args.server:
        for t in all_tasks:
            t["server"] = args.server
    if args.command in ("model", "run-one") and args.index is None:
        parser.error(f"{args.command} needs --index")
    if args.command == "count":
        print(len(all_tasks))
        return 0
    if args.command == "list":
        for t in all_tasks:
            print(task_name(t))
        return 0
    if args.command == "model":
        t = all_tasks[args.index]
        print(get_candidate(load_candidates(), t["candidate"], server=t["server"],
                            quantization=t["quantization"]).model)
        return 0
    search_id = args.search_id or new_search_id(args.grid)
    chosen = all_tasks if args.command == "run" else [all_tasks[args.index]]
    failed = 0
    for t in chosen:
        try:
            run_task(t, search_id, use_mlflow=not args.no_mlflow)
        except PipelineError as exc:
            failed += 1
            log.error("Task %s failed: %s", task_name(t), exc)
    print(json.dumps({"search_id": search_id, "tasks": len(chosen), "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
