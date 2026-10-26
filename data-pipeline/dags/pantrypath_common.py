"""Shared pieces of the Pantry Path DAGs (this file defines no DAG itself).

Airflow and the pipeline use separate Python environments: DAG files run in Airflow's,
and every task calls the pipeline's own virtualenv (PIPELINE_PYTHON). So this module
uses only the standard library and PyYAML, which Airflow already has, and it never
imports the pipeline package.
"""

import json
import logging
import os
import re
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

PIPELINE_HOME = Path(os.environ.get("PANTRYPATH_HOME") or Path(__file__).resolve().parents[1])
PIPELINE_PYTHON = os.environ.get("PIPELINE_PYTHON", "/opt/pipeline-venv/bin/python")

# Same values as pantrypath_core.diet_rules (a test keeps them in sync)
ALLERGENS = ["milk", "egg", "fish", "shellfish", "tree_nuts", "peanuts", "wheat", "soy",
             "sesame", "gluten"]
DIETS = ["none", "vegetarian", "vegan"]

DISH_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Per-step time limits (minutes): generous, so a stuck step fails and alerts instead of
# hanging the run. Keys are the CLI step names (python -m pantrypath_pipeline <step>).
STEP_TIMEOUT_MIN = {
    "download-off": 90,
    "filter-off": 45,
    "validate-off": 30,
    "download-foodcom": 30,
    "clean-recipes": 30,
    "group-dishes": 20,
    "select-dishes": 15,
    "pull-llm-cache": 15,
    "refine-dish": 10,
    "assemble-catalog": 15,
    "validate-catalog": 15,
    "eval-catalog": 10,
    "load-sqlite": 15,
    "compute-stats": 20,
    "check-drift": 10,
    "bias-report": 45,
    "dvc-version": 45,
    "notify": 10,
}

# Request DAG: params arrive as environment variables (REQ_*), never pasted into the
# shell text, so a dish name can't run commands. `set -f` stops the allergen list,
# split into words on purpose, from being glob-expanded. The request module's own
# output goes to the log (stderr); the last stdout line, the result path, is the XCom.
REQUEST_COMMAND = """\
set -euo pipefail
set -f
out="results/result_${REQ_RUN_ID//[^A-Za-z0-9_.-]/_}.json"
"$PIPELINE_PYTHON" -m pantrypath_pipeline.request "$REQ_DISH" --zip "$REQ_ZIP" \
  ${REQ_AVOID:+--avoid $REQ_AVOID} --diet "$REQ_DIET" --out "$out" >&2
echo "$PWD/$out"
"""


def load_settings() -> dict[str, Any]:
    """The pipeline's config/pipeline.yaml (read when the DAG file is parsed)."""
    return yaml.safe_load((PIPELINE_HOME / "config" / "pipeline.yaml").read_text())


def step_command(step: str) -> str:
    """Shell command for one pipeline step. The DAG's failure callback sends the alert,
    so the step itself doesn't (PANTRYPATH_ALERT_ON_FAILURE=0)."""
    return f"PANTRYPATH_ALERT_ON_FAILURE=0 {PIPELINE_PYTHON} -m pantrypath_pipeline {step}"


def read_pool_ids(path: Path) -> list[str]:
    """Dish ids from the candidate pool written by select-dishes, in pool order."""
    data = json.loads(Path(path).read_text())
    dishes = data["dishes"] if isinstance(data, dict) else data
    ids = list(dict.fromkeys(d["dish_id"] for d in dishes))
    bad = [i for i in ids if not isinstance(i, str) or not DISH_ID.match(i)]
    if bad:
        raise ValueError(f"{path}: invalid dish ids {bad[:5]}; check the select-dishes step")
    if not ids:
        raise ValueError(f"{path} lists no dishes; check the select-dishes step")
    return ids


def alert_on_failure(context: dict[str, Any]) -> None:
    """Failure callback: alert (log + email when configured) through the pipeline's own
    alerts module, run with the pipeline's python so Airflow needs nothing extra."""
    ti = context["ti"]
    map_index = getattr(ti, "map_index", -1)
    subject = f"{ti.dag_id}.{ti.task_id}{f'[{map_index}]' if map_index >= 0 else ''} failed"
    body = (
        f"Run: {context.get('run_id')}\n"
        f"Try: {getattr(ti, 'try_number', '?')}\n"
        f"Error: {context.get('exception')}\n"
        "Details: the task log in the Airflow UI, or data-pipeline/logs/."
    )
    code = (
        "import sys\n"
        "from pantrypath_pipeline.alerts import send_alert\n"
        "send_alert(sys.argv[1], sys.argv[2], level='error')\n"
    )
    try:
        subprocess.run([PIPELINE_PYTHON, "-c", code, subject, body], timeout=120, check=False)
    except Exception:  # noqa: BLE001 - a broken alert must not hide the task's own error
        log.exception("Could not send the failure alert for %s", subject)


def default_args(**overrides: Any) -> dict[str, Any]:
    """Retries with exponential backoff (1 min, then 2 min) and an alert on final failure."""
    args = {
        "owner": "pantrypath",
        "retries": 2,
        "retry_delay": timedelta(minutes=1),
        "retry_exponential_backoff": 2.0,
        "max_retry_delay": timedelta(minutes=10),
        "on_failure_callback": alert_on_failure,
    }
    args.update(overrides)
    return args
