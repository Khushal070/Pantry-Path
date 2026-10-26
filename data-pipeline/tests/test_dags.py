"""The DAG files and scripts/run_all.sh: the DAGs import cleanly, the task graph
matches the design, run_all.sh follows the same order, and the request DAG passes user
input to the shell safely.

Airflow isn't in the pipeline venv, so the graph tests run inside the Airflow
container (locally they skip):

    docker compose exec airflow-scheduler bash -c "cd /opt/pantrypath/data-pipeline \
      && PYTHONPATH=src:../shared python -m pytest -q tests/test_dags.py"
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

PIPELINE = Path(__file__).resolve().parents[1]
DAGS = PIPELINE / "dags"
for path in (DAGS, PIPELINE / "src", PIPELINE.parent / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

common = importlib.import_module("pantrypath_common")

PARALLEL_TASKS = {
    "download_off", "filter_off", "validate_off",
    "download_foodcom", "clean_recipes", "group_dishes", "select_dishes", "dish_ids",
    "pull_llm_cache",
    "refine_dish", "assemble_catalog", "validate_catalog", "eval_catalog",
    "load_sqlite", "compute_stats", "check_drift", "bias_report", "dvc_version", "notify",
}
# The serial DAG's chain, and scripts/run_all.sh's order (with refine-all for refine-dish)
SERIAL_ORDER = list(common.STEP_TIMEOUT_MIN)


# --- No Airflow needed ---------------------------------------------------------

def test_every_dag_step_is_a_cli_step():
    from pantrypath_pipeline.cli import STEPS

    assert set(common.STEP_TIMEOUT_MIN) <= set(STEPS)


def test_allergens_and_diets_match_the_shared_rules():
    from pantrypath_core.diet_rules import Allergen, Diet

    assert common.ALLERGENS == [a.value for a in Allergen]
    assert set(common.DIETS) == {d.value for d in Diet}


def test_step_command_disables_the_steps_own_alert():
    assert common.step_command("filter-off") == (
        f"PANTRYPATH_ALERT_ON_FAILURE=0 {common.PIPELINE_PYTHON} -m pantrypath_pipeline filter-off")


def test_read_pool_ids(tmp_path):
    pool = tmp_path / "pool.json"
    pool.write_text('{"dishes": [{"dish_id": "lasagna"}, {"dish_id": "pad-thai"},'
                    ' {"dish_id": "lasagna"}]}')
    assert common.read_pool_ids(pool) == ["lasagna", "pad-thai"]


@pytest.mark.parametrize("content", [
    '{"dishes": []}',
    '{"dishes": [{"dish_id": "pad thai; rm -rf ~"}]}',
    '{"dishes": [{"dish_id": "Lasagna"}]}',
])
def test_read_pool_ids_rejects_bad_pools(tmp_path, content):
    pool = tmp_path / "pool.json"
    pool.write_text(content)
    with pytest.raises(ValueError):
        common.read_pool_ids(pool)


def run_request_command(tmp_path, **env):
    """Run the request DAG's shell command with a fake python that records its argv."""
    fake = tmp_path / "fake_python"
    argv_file = tmp_path / "argv.txt"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_file}"\necho "noise for the log"\n')
    fake.chmod(0o755)
    (tmp_path / "results").mkdir(exist_ok=True)
    full_env = {"PATH": os.environ["PATH"], "PIPELINE_PYTHON": str(fake), "REQ_ZIP": "45202",
                "REQ_DIET": "vegan", "REQ_RUN_ID": "manual__2026-10-25T01:02:03+00:00", **env}
    proc = subprocess.run(["bash", "-c", common.REQUEST_COMMAND], cwd=tmp_path, env=full_env,
                          capture_output=True, text=True, check=True)
    return argv_file.read_text().splitlines(), proc.stdout.strip().splitlines()


def test_request_command_passes_input_as_single_arguments(tmp_path):
    nasty = 'quiche"; touch PWNED; echo "$(touch PWNED2)'
    argv, stdout = run_request_command(tmp_path, REQ_DISH=nasty, REQ_AVOID="milk gluten")

    out = "results/result_manual__2026-10-25T01_02_03_00_00.json"
    assert argv == ["-m", "pantrypath_pipeline.request", nasty, "--zip", "45202",
                    "--avoid", "milk", "gluten", "--diet", "vegan", "--out", out]
    assert not (tmp_path / "PWNED").exists() and not (tmp_path / "PWNED2").exists()
    assert stdout == [f"{tmp_path}/{out}"]  # only the path: the XCom


def test_request_command_without_allergens_omits_the_flag(tmp_path):
    argv, _ = run_request_command(tmp_path, REQ_DISH="tacos", REQ_AVOID="")
    assert "--avoid" not in argv


def run_all(tmp_path, *args, fail_step=""):
    """Run scripts/run_all.sh with a fake python that records each step it is given."""
    record = tmp_path / "steps.txt"
    fake = tmp_path / "fake_python"
    fake.write_text(f'#!/bin/sh\necho "$3" >> "{record}"\n'
                    f'[ "$3" = "{fail_step}" ] && exit 1\nexit 0\n')
    fake.chmod(0o755)
    proc = subprocess.run(["bash", str(PIPELINE / "scripts" / "run_all.sh"), *args],
                          env={**os.environ, "PYTHON": str(fake)}, capture_output=True, text=True)
    return proc.returncode, record.read_text().split() if record.exists() else []


def test_run_all_runs_every_step_in_dag_order(tmp_path):
    code, steps = run_all(tmp_path)
    assert code == 0
    assert steps == [s.replace("refine-dish", "refine-all") for s in SERIAL_ORDER]


def test_run_all_stops_at_a_failure_and_can_resume(tmp_path):
    code, steps = run_all(tmp_path, fail_step="validate-catalog")
    assert code == 1 and steps[-1] == "validate-catalog"

    (tmp_path / "steps.txt").unlink()
    code, steps = run_all(tmp_path, "load-sqlite")
    assert code == 0 and steps == SERIAL_ORDER[SERIAL_ORDER.index("load-sqlite"):]

    assert run_all(tmp_path, "no-such-step")[0] == 2


# --- Inside the Airflow container -----------------------------------------------

@pytest.fixture(scope="module")
def dagbag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    return DagBag(dag_folder=str(DAGS))


def test_dags_import_without_errors(dagbag):
    assert dagbag.import_errors == {}
    assert set(dagbag.dag_ids) == {
        "build_reference_data", "build_reference_data_serial", "serve_request"}


def test_parallel_dag_graph(dagbag):
    dag = dagbag.get_dag("build_reference_data")
    up = {t.task_id: t.upstream_task_ids for t in dag.tasks}

    assert set(up) == PARALLEL_TASKS
    assert dag.schedule == "@weekly" and dag.catchup is False
    # Two independent branches...
    assert up["download_off"] == set() and up["download_foodcom"] == set()
    assert up["filter_off"] == {"download_off"} and up["validate_off"] == {"filter_off"}
    assert up["dish_ids"] == {"select_dishes"} and up["pull_llm_cache"] == set()
    assert up["refine_dish"] == {"dish_ids", "pull_llm_cache"}  # saved replies first
    assert up["assemble_catalog"] == {"refine_dish"}
    assert up["eval_catalog"] == {"validate_catalog"}
    # ...that join before loading
    assert up["load_sqlite"] == {"validate_off", "eval_catalog"}
    tail = ["load_sqlite", "compute_stats", "check_drift", "bias_report", "dvc_version",
            "notify"]
    for before, after in zip(tail, tail[1:], strict=False):
        assert up[after] == {before}


def test_refine_is_mapped_and_capped(dagbag):
    settings = common.load_settings()
    refine = dagbag.get_dag("build_reference_data").get_task("refine_dish")
    assert refine.is_mapped
    assert refine.max_active_tis_per_dagrun == settings["llm"]["concurrency"]
    assert refine.partial_kwargs["bash_command"].endswith('refine-dish --dish-id "$DISH_ID"')
    assemble = dagbag.get_dag("build_reference_data").get_task("assemble_catalog")
    assert assemble.trigger_rule == "all_done_min_one_success"

    serial = dagbag.get_dag("build_reference_data_serial").get_task("refine_dish")
    assert serial.max_active_tis_per_dagrun == 1


def test_serial_dag_is_one_chain(dagbag):
    dag = dagbag.get_dag("build_reference_data_serial")
    assert {t.task_id for t in dag.tasks} == PARALLEL_TASKS
    assert dag.schedule is None
    assert all(len(t.downstream_task_ids) <= 1 for t in dag.tasks)
    order = [t.task_id for t in dag.tasks if not t.upstream_task_ids]
    assert order == ["download_off"]
    while downstream := dag.get_task(order[-1]).downstream_task_ids:
        order.extend(downstream)
    assert [t for t in order if t != "dish_ids"] == [s.replace("-", "_") for s in SERIAL_ORDER]


def test_steps_retry_alert_and_time_out(dagbag):
    for dag_id in ("build_reference_data", "build_reference_data_serial"):
        for t in dagbag.get_dag(dag_id).tasks:
            if t.task_id == "dish_ids":
                continue
            assert t.retries == 2 and t.retry_exponential_backoff == 2.0, t.task_id
            assert t.on_failure_callback, t.task_id
            if not t.is_mapped:
                assert t.execution_timeout is not None, t.task_id
                assert t.bash_command == common.step_command(t.task_id.replace("_", "-"))


def test_request_dag_takes_validated_params(dagbag):
    dag = dagbag.get_dag("serve_request")
    assert set(dag.params) == {"dish", "zip", "avoid", "diet"}
    task = dag.get_task("build_result")
    assert task.bash_command == common.REQUEST_COMMAND
    assert "params" not in task.bash_command  # input never becomes shell text
    assert task.env["REQ_DISH"] == "{{ params.dish }}"
    assert task.append_env is True
