"""Weekly batch DAG: build the reference data every Pantry Path request needs.

Two branches run in parallel, then join:

    Open Food Facts:  download_off -> filter_off -> validate_off
    Food.com:         download_foodcom -> clean_recipes -> group_dishes -> select_dishes
                      -> dish_ids -> refine_dish (one mapped task per dish; LLM)
    Saved replies:    pull_llm_cache -> refine_dish (the LLM replies saved in DVC, so a
                      run replays them instead of calling the LLM again)
                      -> assemble_catalog -> validate_catalog -> eval_catalog
    then:            load_sqlite -> compute_stats -> check_drift -> bias_report
                      -> dvc_version -> notify

Each task runs one pipeline step in the pipeline's own virtualenv
(`python -m pantrypath_pipeline <step>`), the same commands scripts/run_all.sh runs.
Failed tasks retry twice with exponential backoff, then alert.

A dish that still fails after its retries doesn't stop the run: assemble_catalog runs
once every refine_dish task has finished (at least one successfully) and fails only if
too few dishes survive (catalog.min_dishes).

This file also defines build_reference_data_serial: the same tasks in one chain,
refining one dish at a time. It is the "before" in the Gantt-chart comparison, so it
only runs when triggered by hand. Don't run both DAGs at once: they share files.
"""

from datetime import UTC, datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG, chain, task
from pantrypath_common import (
    PIPELINE_HOME,
    STEP_TIMEOUT_MIN,
    default_args,
    load_settings,
    read_pool_ids,
    step_command,
)

SETTINGS = load_settings()


def step(name: str, **kwargs) -> BashOperator:
    """One pipeline step as a task (task id = step name with underscores)."""
    return BashOperator(
        task_id=name.replace("-", "_"),
        bash_command=step_command(name),
        cwd=str(PIPELINE_HOME),
        execution_timeout=timedelta(minutes=STEP_TIMEOUT_MIN[name]),
        **kwargs,
    )


def build_dag(dag_id: str, *, serial: bool) -> DAG:
    with DAG(
        dag_id=dag_id,
        description=("Baseline: every step in one chain, one dish at a time" if serial
                     else "Dish catalog + allergen snapshot, validated and versioned"),
        doc_md=__doc__,
        schedule=None if serial else "@weekly",
        start_date=datetime(2026, 10, 19, tzinfo=UTC),
        catchup=False,
        max_active_runs=1,
        dagrun_timeout=timedelta(hours=8),
        default_args=default_args(),
        tags=["pantrypath", "batch"] + (["baseline"] if serial else []),
    ) as dag:
        off = [step(s) for s in ("download-off", "filter-off", "validate-off")]
        foodcom = [step(s) for s in
                   ("download-foodcom", "clean-recipes", "group-dishes", "select-dishes")]

        @task(execution_timeout=timedelta(minutes=2))
        def dish_ids() -> list[dict[str, str]]:
            """One env per candidate dish, for the mapped refine_dish tasks."""
            pool = PIPELINE_HOME / SETTINGS["foodcom"]["pool_output"]
            return [{"DISH_ID": dish_id} for dish_id in read_pool_ids(pool)]

        ids = dish_ids()
        # DISH_ID reaches the command as an environment variable, never as shell text
        refine = BashOperator.partial(
            task_id="refine_dish",
            bash_command=step_command("refine-dish") + ' --dish-id "$DISH_ID"',
            append_env=True,
            cwd=str(PIPELINE_HOME),
            execution_timeout=timedelta(minutes=STEP_TIMEOUT_MIN["refine-dish"]),
            max_active_tis_per_dagrun=1 if serial else int(SETTINGS["llm"]["concurrency"]),
            map_index_template="{{ task.env['DISH_ID'] }}",
        ).expand(env=ids)

        catalog = [step("assemble-catalog", trigger_rule="all_done_min_one_success"),
                   step("validate-catalog"), step("eval-catalog")]
        tail = [step(s) for s in ("load-sqlite", "compute-stats", "check-drift",
                                  "bias-report", "dvc-version", "notify")]

        pull = step("pull-llm-cache")

        if serial:
            chain(*off, *foodcom, pull, ids, refine, *catalog, *tail)
        else:
            chain(*off, tail[0])
            chain(*foodcom, ids, refine, *catalog, tail[0])
            chain(pull, refine)
            chain(*tail)
    return dag


build_reference_data = build_dag("build_reference_data", serial=False)
build_reference_data_serial = build_dag("build_reference_data_serial", serial=True)
