"""Run one pipeline step: python -m pantrypath_pipeline <step> [--dish-id ID]

Airflow tasks, scripts/run_all.sh and dvc.yaml all call these same steps, so the
pipeline also runs without Airflow. Each step is a function run(cfg, **kwargs) -> dict;
the dict is printed as JSON on the last stdout line (Airflow keeps it as the XCom).
Logs go to stderr and logs/pipeline.log.
"""

import argparse
import importlib
import json
import logging
import os
import sys
import time

from pantrypath_pipeline.alerts import send_alert
from pantrypath_pipeline.config import load_config
from pantrypath_pipeline.errors import PipelineError
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger("pantrypath_pipeline.cli")

# step name -> "module:function"
STEPS = {
    "download-off": "pantrypath_pipeline.acquire.off:run",
    "filter-off": "pantrypath_pipeline.preprocess.off_filter:run",
    "validate-off": "pantrypath_pipeline.validate.expectations:run_off",
    "download-foodcom": "pantrypath_pipeline.acquire.foodcom:run",
    "clean-recipes": "pantrypath_pipeline.preprocess.recipes:run",
    "group-dishes": "pantrypath_pipeline.preprocess.dishes:run_group",
    "select-dishes": "pantrypath_pipeline.preprocess.dishes:run_select",
    "pull-llm-cache": "pantrypath_pipeline.versioning:run_pull_llm_cache",
    "refine-dish": "pantrypath_pipeline.refine:run_one",
    "refine-all": "pantrypath_pipeline.refine:run_all",
    "assemble-catalog": "pantrypath_pipeline.catalog_build:run",
    "validate-catalog": "pantrypath_pipeline.validate.expectations:run_catalog",
    "eval-catalog": "pantrypath_pipeline.evaluate:run_catalog",
    "load-sqlite": "pantrypath_pipeline.store:run",
    "compute-stats": "pantrypath_pipeline.validate.stats:run",
    "check-drift": "pantrypath_pipeline.validate.drift:run",
    "bias-report": "pantrypath_pipeline.bias:run",
    "freeze-seen-products": "pantrypath_pipeline.bias:run_freeze_seen",  # by hand, not in the DAG
    "dvc-version": "pantrypath_pipeline.versioning:run",
    "notify": "pantrypath_pipeline.notify:run",
}


def resolve(step: str):
    module_name, func_name = STEPS[step].split(":")
    return getattr(importlib.import_module(module_name), func_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantrypath", description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=sorted(STEPS))
    parser.add_argument("--dish-id", help="for refine-dish: the dish to refine")
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg.path(cfg["paths"]["logs"]))
    kwargs = {"dish_id": args.dish_id} if args.dish_id else {}

    started = time.monotonic()
    log.info("step %s: start %s", args.step, kwargs or "")
    try:
        summary = resolve(args.step)(cfg, **kwargs) or {}
    except PipelineError as exc:
        log.error("step %s failed: %s", args.step, exc)
        # No duplicate alerts: under Airflow the DAG's failure callback alerts instead, and
        # some steps already sent a detailed alert before raising (exc.alerted).
        alert_here = os.environ.get("PANTRYPATH_ALERT_ON_FAILURE", "1") == "1"
        if alert_here and not getattr(exc, "alerted", False):
            send_alert(f"step {args.step} failed", str(exc), level="error", cfg=cfg)
        return 1
    log.info("step %s: done in %.1fs %s", args.step, time.monotonic() - started, summary)
    print(json.dumps(summary, default=str))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
