"""Gates, selection and the champion record.

    python -m pantrypath_ml.select choose --search-id ID [--write-proposed]
    python -m pantrypath_ml.select choose --runs reports/a1/<run> reports/a2/<run> ...
        settings that pass the hard and bias gates on dev; per model and quantization the
        best A1, A2 and A3 setting; the smallest model wins, then the best score. --runs
        takes existing dev (and A2 loo, A3 dev) run folders instead of a search. A3 is not
        searched: without an A3 dev run it gets configs/answer.yaml defaults, with one its
        hard gates must pass. A full leave-one-out run among --runs (loo_full, every
        catalog dish) is not used to choose; it is recorded as the chosen A2 setting's
        a2.final_bias_run. --exclude-a3 proposes A1 and A2 only: A3 is recorded as
        "evaluated, not promoted" with its dev results and failing gates, and select final
        and the champion leave it out (the app shows result.json without an A3 answer).
    python -m pantrypath_ml.select final [--proposed registry/proposed.json] [--ci]
        runs the proposed setting once on the held-out sets (logged): A1 test and
        realistic, A2 test and A3 test (not A3 when it is not promoted). The A2 bias gap
        comes from the proposed setting's
        full leave-one-out run (a2.final_bias_run, all 100 catalog dishes, recorded by
        choose); search and choose use the loo run without the golden test dishes
        (a2.bias_run). Applies the gates and writes
        reports/final/<id>/. --ci runs a small seeded sample of the DEV splits instead
        (gates.yaml ci) and never reads the held-out sets. Exit 1 on a failed gate.
    python -m pantrypath_ml.select promote --final reports/final/<id>/final.json
    python -m pantrypath_ml.select promote --latest-full [--proposed ...]
        replaces registry/champion.json only from a full final report committed to Git,
        and only if the new setting is no worse on any metric without a hard gate (goals
        and score) and better on the score (a tie goes to the smaller model); the old
        record is kept as registry/champion.previous.json for rollback. --latest-full
        uses the newest passing full final report for the proposed setting. A CI result
        (a dev sample) is never promoted.
"""

import argparse
import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pantrypath_ml import config
from pantrypath_ml.reporting import new_run_id, write_json
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import PipelineError
from pantrypath_pipeline.logging_setup import setup_logging

log = logging.getLogger(__name__)

REGISTRY = config.ML_HOME / "registry"
PROPOSED = REGISTRY / "proposed.json"
CHAMPION = REGISTRY / "champion.json"
PREVIOUS = REGISTRY / "champion.previous.json"
SETTING_KEYS = ("candidate", "quantization", "a1", "a2", "a3")


# ---------------------------------------------------------------- gates and score


def gate(task: str, summary: dict[str, Any], bias_gap: float | None, gates: Config, *,
         on_insufficient: str = "warn") -> dict[str, Any]:
    """Hard gates and the bias gap for one task's summary; the goals are reported only.

    Bias status: passed | failed | insufficient (no two groups reach min_group_size) |
    not_gated (on_insufficient "skip": the set is not sliced). A gap over the limit, or
    one that can't be measured with on_insufficient "fail", fails the setting where the
    bias gate blocks (bias.blocking); otherwise it is a warning with its reason.
    """
    checks, failures, warnings = {}, [], []
    for metric, rule in gates["hard"][task].items():
        value = summary.get(metric)
        # {min: x} is a floor, {max: x} a ceiling (A3's repaired_rate)
        if "max" in rule:
            ok = value is not None and value <= float(rule["max"])
            checks[metric] = {"value": value, "max": rule["max"], "passed": ok}
            problem = f"{metric} {value} > {rule['max']}"
        else:
            ok = value is not None and value >= float(rule["min"])
            checks[metric] = {"value": value, "min": rule["min"], "passed": ok}
            problem = f"{metric} {value} < {rule['min']}"
        if not ok:
            failures.append(problem)
    bcfg = gates["bias"]
    max_gap = float(bcfg["max_gap"])
    blocking = bool(bcfg["blocking"].get(task, False))
    reason = (bcfg.get("warning_reason") or {}).get(task)
    problem = None
    if bias_gap is not None:
        status = "passed" if bias_gap <= max_gap else "failed"
        if status == "failed":
            problem = f"bias gap {bias_gap} > {max_gap}"
    elif on_insufficient == "skip":
        status = "not_gated"
    else:
        status = "insufficient"
        problem = ("bias gap not measurable (insufficient data): no two groups reach "
                   f"{bcfg['min_group_size']} items")
    if problem:
        fails = blocking and (status == "failed" or on_insufficient == "fail")
        if fails:
            failures.append(problem)
        else:
            warnings.append(f"{problem}. {reason}" if reason else problem)
    goals = {m: {"value": summary.get(m), "target": target,
                 "met": summary.get(m) is not None and summary[m] >= float(target)}
             for m, target in (gates["goals"].get(task) or {}).items()}
    return {"passed": not failures, "hard": checks,
            "bias": {"gap": bias_gap, "max": max_gap, "status": status,
                     "passed": status in ("passed", "not_gated"), "blocking": blocking},
            "goals": goals, "failures": failures, "warnings": warnings}


def score(task: str, metrics: dict[str, Any], gates: Config) -> float:
    return round(sum(float(w) * float(metrics.get(m) or 0)
                     for m, w in (gates["score"].get(task) or {}).items()), 4)


A3_NOT_PROMOTED = "evaluated_not_promoted"


def a3_promoted(setting: dict[str, Any]) -> bool:
    """False when choose --exclude-a3 recorded A3 as evaluated, not promoted."""
    return (setting.get("a3") or {}).get("status") != A3_NOT_PROMOTED


def a3_setting(proposed: dict[str, Any]) -> dict[str, Any]:
    """A3's setting: the proposed one, filled in from configs/answer.yaml defaults (A3 is
    not in the search grid this milestone); an A3 that is not promoted stays its record."""
    given = proposed.get("a3") or {}
    if not a3_promoted(proposed):
        return dict(given)
    defaults = config.load("answer")["defaults"]
    return {**given, **{k: given.get(k, v) for k, v in defaults.items()}}


def a3_not_promoted(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The A3 record for a proposal without A3: its newest dev run's results and failures."""
    from pantrypath_ml.aggregate import A3_COLUMNS

    record: dict[str, Any] = {"status": A3_NOT_PROMOTED,
                              "reason": "excluded from the champion (choose --exclude-a3)"}
    if not rows:
        return {**record, "dev_run": None}
    last = max(rows, key=lambda r: r["run_id"])
    return {**record, "dev_run": last["run_id"], "prompt_variant": last.get("prompt_variant"),
            "prompt_version": last.get("prompt_version"), "temperature": last["temperature"],
            "think": last["think"], "passed": last["passed"], "failures": last["failures"],
            "dev_results": {c: last.get(c) for c in A3_COLUMNS},
            "rescored_from": last.get("rescored_from")}


# ---------------------------------------------------------------- choose


def choose(table: list[dict[str, Any]], gates: Config, *,
           exclude_a3: bool = False) -> dict[str, Any] | None:
    """Best passing A1, A2 (and A3, if it has dev runs) setting per (candidate,
    quantization); smallest model first. With exclude_a3, A3 neither blocks nor is
    proposed: it is recorded as evaluated, not promoted."""
    options = []
    groups = {(r["candidate"], r["quantization"]) for r in table}
    for candidate, quant in sorted(groups):
        rows = [r for r in table if (r["candidate"], r["quantization"]) == (candidate, quant)]
        best = {}
        for task in ("a1", "a2") if exclude_a3 else ("a1", "a2", "a3"):
            passing = [r for r in rows if r["task"] == task and r["passed"]]
            if passing:
                best[task] = max(passing, key=lambda r, t=task: (score(t, r, gates),
                                                                 -r["latency_mean_s"]))
        has_a3 = not exclude_a3 and any(r["task"] == "a3" for r in rows)
        if "a1" in best and "a2" in best and (not has_a3 or "a3" in best):
            options.append((best["a1"]["params_b"],
                            -(score("a1", best["a1"], gates) + score("a2", best["a2"], gates)),
                            best))
    if not options:
        return None
    _, _, best = min(options, key=lambda o: (o[0], o[1]))
    a1, a2 = best["a1"], best["a2"]
    chosen = {"candidate": a1["candidate"], "quantization": a1["quantization"],
              "server": a1["server"],
              "a1": {"prompt_variant": a1["prompt_variant"], "temperature": a1["temperature"],
                     "think": a1["think"], "dev_run": a1["run_id"]},
              "a2": {"prompt_variant": a2["prompt_variant"], "temperature": a2["temperature"],
                     "think": a2["think"], "dev_run": a2["run_id"]}}
    if a2.get("bias_split") == "loo":
        chosen["a2"]["bias_run"] = a2["bias_run"]
    if exclude_a3:
        chosen["a3"] = a3_not_promoted(
            [r for r in table if r["task"] == "a3" and r["candidate"] == a1["candidate"]
             and r["quantization"] == a1["quantization"]])
    elif "a3" in best:
        a3 = best["a3"]
        chosen["a3"] = {"prompt_variant": a3.get("prompt_variant"),
                        "temperature": a3["temperature"], "think": a3["think"],
                        "dev_run": a3["run_id"]}
    else:
        chosen["a3"] = dict(config.load("answer")["defaults"])
    chosen["warnings"] = {t: best[t]["warnings"] for t in best if best[t].get("warnings")}
    return chosen


def is_full_loo(params: dict[str, Any]) -> bool:
    """A leave-one-out run over every catalog dish: loo_full, or a loo run from before the
    golden test dishes were left out of loo (no held_out_excluded)."""
    return params["split"] == "loo_full" or (params["split"] == "loo"
                                             and not params.get("held_out_excluded"))


def run_rows(folders: list[Path], gates: Config) -> tuple[list[dict[str, Any]],
                                                          list[dict[str, Any]]]:
    """Comparison rows (as in a search's comparison.json) from existing run folders, and
    the summaries of the full leave-one-out runs among them (for the final bias check,
    never for choosing)."""
    from pantrypath_ml.aggregate import rows

    runs, full = [], []
    for folder in folders:
        path = folder / "summary.json"
        if not path.is_file():
            raise PipelineError(f"{folder} is not a run folder (no summary.json)")
        summary = json.loads(path.read_text())
        if summary.get("task") == "a2" and is_full_loo(summary["params"]):
            full.append(summary)
            continue
        if summary["params"]["split"] in ("test", "realistic"):
            raise PipelineError(f"{folder} is a held-out run: choose from dev runs only")
        runs.append((folder, summary))
    return rows(runs, gates), full


def same_a2_setting(params: dict[str, Any], proposed: dict[str, Any]) -> bool:
    a2 = proposed["a2"]
    return (params["candidate"] == proposed["candidate"]
            and params["quantization"] == proposed["quantization"]
            and params.get("prompt_variant") == a2.get("prompt_variant")
            and params["temperature"] == a2["temperature"] and params["think"] == a2["think"])


def attach_final_bias_run(chosen: dict[str, Any], full: list[dict[str, Any]]) -> None:
    """Record the newest full leave-one-out run of the chosen A2 setting, if one was given."""
    matching = sorted(s["run_id"] for s in full if same_a2_setting(s["params"], chosen))
    if matching:
        chosen["a2"]["final_bias_run"] = matching[-1]
    elif full:
        log.warning("No full leave-one-out run among --runs matches the chosen A2 setting")


# ---------------------------------------------------------------- champion


def read_record(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text()).get("champion") if path.is_file() else None


def regression_metrics(gates: Config) -> list[tuple[str, str]]:
    """Every goal and score metric without a hard gate: the champion must not beat these."""
    out = []
    for task in dict.fromkeys([*gates["goals"], *gates["score"]]):
        hard = gates["hard"].get(task) or {}
        for metric in dict.fromkeys([*(gates["goals"].get(task) or {}),
                                     *(gates["score"].get(task) or {})]):
            if metric not in hard:
                out.append((task, metric))
    return out


def better(new: dict[str, Any], old: dict[str, Any] | None, kind: str,
           gates: Config) -> tuple[bool, str]:
    """Is `new` (metrics of `kind`: full | ci) good enough to replace the champion?"""
    if old is None:
        return True, "no champion yet"
    old_metrics = (old.get("metrics") or {}).get(kind)
    if not old_metrics:  # nothing comparable: never replace blind
        return False, f"the champion has no {kind} metrics to compare with"
    new_metrics = new["metrics"][kind]

    def value(metrics: dict[str, Any], task: str, metric: str) -> float:
        # a task one record lacks (an older champion without A3) counts as 0
        return float((metrics.get(task) or {}).get(metric) or 0)

    worse = [f"{t}.{m}" for t, m in regression_metrics(gates)
             if value(new_metrics, t, m) < value(old_metrics, t, m)]
    if worse:
        return False, "worse than the champion on " + ", ".join(worse)
    new_score = score("a1", new_metrics["a1"], gates) + score("a2", new_metrics["a2"], gates)
    old_score = score("a1", old_metrics["a1"], gates) + score("a2", old_metrics["a2"], gates)
    if new_score > old_score:
        return True, f"score {new_score:.4f} > {old_score:.4f}"
    if new_score == old_score and new["params_b"] < old["params_b"]:
        return True, "same score, smaller model"
    return False, f"score {new_score:.4f} is not above the champion's {old_score:.4f}"


def promote(final: dict[str, Any], gates: Config, *, champion_path: Path = CHAMPION,
            previous_path: Path = PREVIOUS) -> dict[str, Any]:
    """Replace the champion with a full final report's setting if it is better."""
    kind = final.get("kind")
    if kind != "full":
        return {"promoted": False, "reason": f"a {kind} result (a dev sample) is never "
                                             "promoted: run select final for a full report"}
    if not final["passed"]:
        return {"promoted": False, "reason": "the final run failed a gate"}
    old = read_record(champion_path)
    record = {**final["setting"], "params_b": final["params_b"], "models": final["models"],
              "prompt_versions": final["prompt_versions"],
              "metrics": {kind: final["metrics"]},
              "promoted_at": datetime.now(UTC).isoformat(timespec="seconds"),
              "final_id": final["final_id"], "git_commit": final["git_commit"]}
    ok, reason = better(record, old, kind, gates)
    if old is not None and not (old.get("metrics") or {}).get(kind) and _same_setting(old, record):
        old.setdefault("metrics", {})[kind] = final["metrics"]
        write_json(champion_path, {"champion": old})
        return {"promoted": False, "reason": f"same setting: recorded its {kind} metrics as "
                                             "the champion's baseline"}
    if ok:
        if old is not None:
            write_json(previous_path, {"champion": old})
        write_json(champion_path, {"champion": record})
    log.info("Champion %s: %s", "replaced" if ok else "kept", reason)
    return {"promoted": ok, "reason": reason}


def _same_setting(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in SETTING_KEYS)


def is_committed(path: Path) -> bool:
    """The file is tracked by Git and unchanged since the last commit."""
    def git(*args: str) -> int:
        return subprocess.run(["git", *args], cwd=path.parent, capture_output=True).returncode

    return git("ls-files", "--error-unmatch", path.name) == 0 \
        and git("diff", "--quiet", "HEAD", "--", path.name) == 0


def latest_full_final(proposed: dict[str, Any], folder: Path | None = None) -> Path | None:
    """The newest full final report that passed for this setting."""
    folder = folder or config.REPORTS / "final"
    want = {**{k: proposed.get(k) for k in SETTING_KEYS[:4]}, "a3": a3_setting(proposed)}
    for path in sorted(folder.glob("*-final/final.json"), reverse=True):
        report = json.loads(path.read_text())
        if report.get("kind") == "full" and report.get("passed") and all(
                report["setting"].get(k) == v for k, v in want.items()):
            return path
    return None


# ---------------------------------------------------------------- final confirmation


def _bias_run(proposed: dict[str, Any]) -> Path:
    """The proposed A2 setting's full leave-one-out run (every catalog dish), checked to be
    that setting's. An older loo run not rescored with the current matcher gets a warning
    (evaluate_a2 --rescore)."""
    run_id = proposed["a2"].get("final_bias_run")
    if not run_id:
        raise PipelineError("registry/proposed.json has no a2.final_bias_run (a full A2 "
                            "leave-one-out run, --split loo_full): rerun select choose with "
                            "one in --runs")
    folder = config.REPORTS / "a2" / run_id
    path = folder / "summary.json"
    if not path.is_file():
        raise PipelineError(f"A2 bias run {folder} not found")
    summary = json.loads(path.read_text())
    p = summary["params"]
    if not is_full_loo(p):
        raise PipelineError(f"A2 bias run {run_id} is not a full leave-one-out run (it leaves "
                            "out the golden test dishes): run --split loo_full")
    if not same_a2_setting(p, proposed):
        raise PipelineError(f"A2 bias run {run_id} is not a leave-one-out run of the proposed "
                            "A2 setting")
    if not summary.get("rescored") and p["split"] == "loo":
        log.warning("A2 bias run %s was scored with an older matcher: rescore it first", run_id)
    return folder


TASK_KEYS = ("prompt_variant", "temperature", "think")


def current_prompt_versions(proposed: dict[str, Any]) -> dict[str, str]:
    """The prompt version each proposed task would run with the code as it is now."""
    from pantrypath_ml import evaluate_a2
    from pantrypath_ml.answer import load_prompt as load_answer_prompt
    from pantrypath_ml.parse_request import prompt_version

    out = {"a1": prompt_version(config.load("parse_request"), proposed["a1"]["prompt_variant"]),
           "a2": evaluate_a2.load_variant(
               config.pipeline_config(), proposed["a2"].get("prompt_variant")
               or config.load("refine_dish")["default"])[1]}
    if a3_promoted(proposed):
        out["a3"] = load_answer_prompt(config.load("answer"),
                                       a3_setting(proposed).get("prompt_variant"))[1]
    return out


def reusable_runs(reuse: dict[str, Any], proposed: dict[str, Any], *,
                  rerun: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    """The run summaries of an earlier full final that can stand for the proposed setting:
    A1, A1 realistic and A2 always, A3 too when that final has an A3 run. Refused
    (PipelineError) unless the final ran exactly the proposed setting for every task it
    reuses: model, quantization, and per task the prompt variant, temperature and
    thinking, the A2 bias run, and the prompt versions the code has now (a changed prompt
    file means a rerun). An A3 run is never reused for an A3 that is not promoted. Tasks in
    `rerun` ("a1": A1 test and realistic; "a2"; "a3") are neither checked nor reused."""
    if reuse.get("kind") != "full":
        raise PipelineError("only a full final's runs can be reused")
    old = reuse["setting"]
    reuse_a3 = "a3" in (reuse.get("runs") or {})
    if reuse_a3 and not a3_promoted(proposed):
        raise PipelineError(f"final {reuse['final_id']} has an A3 run, but the proposed A3 is "
                            "evaluated, not promoted: rerun select final")
    tasks = tuple(t for t in (("a1", "a2", "a3") if reuse_a3 else ("a1", "a2"))
                  if t not in rerun)
    want = {"a1": proposed["a1"], "a2": proposed["a2"], "a3": a3_setting(proposed)}
    problems = [k for k in ("candidate", "quantization") if old.get(k) != proposed.get(k)]
    problems += [f"{t}.{k}" for t in tasks for k in TASK_KEYS
                 if (old.get(t) or {}).get(k) != want[t].get(k)]
    if "a2" in tasks and old["a2"].get("final_bias_run") != proposed["a2"].get(
            "final_bias_run"):
        problems.append("a2.final_bias_run")
    now = current_prompt_versions(proposed)
    problems += [f"{t} prompt version {reuse['prompt_versions'].get(t)} (now {now[t]})"
                 for t in tasks if (reuse.get("prompt_versions") or {}).get(t) != now[t]]
    if problems:
        raise PipelineError(f"final {reuse['final_id']} ran a different setting "
                            f"({', '.join(problems)}): rerun select final in full")
    runs = {}
    for task in ("a1", "a1_realistic", "a2", "a3") if reuse_a3 else ("a1", "a1_realistic", "a2"):
        if (task[:2] if task.startswith("a1") else task) in rerun:
            continue
        folder = config.REPORTS / ("a1" if task.startswith("a1") else task) / reuse["runs"][task]
        runs[task] = json.loads((folder / "summary.json").read_text())
    return runs


def final(proposed: dict[str, Any], *, ci: bool = False, use_mlflow: bool = True,
          server: str | None = None, reuse: dict[str, Any] | None = None,
          rerun: tuple[str, ...] = ()) -> dict[str, Any]:
    """The proposed setting on the held-out sets (or, with ci, a dev sample), then the gates.

    Runs on the default server (Ollama, what is deployed) unless `server` is given, even
    when the setting was chosen on vLLM. With `reuse` (an earlier full final.json of the
    same setting), its A1, A1 realistic and A2 runs, and its A3 run if it has one, are taken
    as they are (reusable_runs); A3 runs only if it isn't reused. Every gate is applied
    again, the report is stamped with the current commit, and it says what was reused
    (restamping a final after a merge runs no model at all).
    """
    from pantrypath_ml import bias, evaluate_a1, evaluate_a2, evaluate_a3
    from pantrypath_ml.local_llm import get_candidate, load_candidates
    from pantrypath_ml.registry import git_commit

    gates = config.load("gates")
    kind = "ci" if ci else "full"
    final_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{'ci' if ci else 'final'}"
    split = gates["ci"]["split"] if ci else "test"
    limits = gates["ci"]["limit"] if ci else {}
    insufficient = gates["bias"]["on_insufficient"]["ci" if ci else "final"]
    ccfg = load_candidates()
    server = server or ccfg["default_server"]
    tags = {"final_id": final_id, "stage": kind}
    common = dict(server=server, quantization=proposed["quantization"],
                  use_mlflow=use_mlflow, tags=tags)
    a3 = a3_setting(proposed)
    with_a3 = a3_promoted(proposed)
    tasks = ("a1", "a2", "a3") if with_a3 else ("a1", "a2")
    settings = {t: {k: s[k] for k in ("temperature", "think")}
                for t, s in (("a1", proposed["a1"]), ("a2", proposed["a2"]), ("a3", a3))
                if t in tasks}
    a2_bias = None if ci else _bias_run(proposed)
    if reuse is not None and ci:
        raise PipelineError("--reuse-final is for a full final only")
    runs: dict[str, dict[str, Any]] = {}
    if reuse is not None:
        runs.update(reusable_runs(reuse, proposed, rerun=rerun))
        log.warning("Final confirmation %s: reusing the %s runs of %s", final_id,
                    ", ".join(runs), reuse["final_id"])
    if "a1" not in runs:
        if not ci:
            log.warning("Final confirmation %s: reading the held-out A1 TEST and REALISTIC "
                        "splits", final_id)
        runs["a1"] = evaluate_a1.run(proposed["candidate"], split,
                                     variant=proposed["a1"]["prompt_variant"],
                                     limit=limits.get("a1"), run_id=f"{final_id}-a1",
                                     overrides=settings["a1"], **common)
        if not ci:
            runs["a1_realistic"] = evaluate_a1.run(
                proposed["candidate"], "realistic", variant=proposed["a1"]["prompt_variant"],
                run_id=f"{final_id}-a1-realistic", overrides=settings["a1"], **common)
    if "a2" not in runs:
        runs["a2"] = evaluate_a2.run(proposed["candidate"], split, limit=limits.get("a2"),
                                     variant=proposed["a2"].get("prompt_variant"),
                                     run_id=f"{final_id}-a2", overrides=settings["a2"],
                                     **common)
    if with_a3:
        if "a3" not in runs:
            runs["a3"] = evaluate_a3.run(proposed["candidate"], split, limit=limits.get("a3"),
                                         run_id=f"{final_id}-a3", overrides=settings["a3"],
                                         variant=a3.get("prompt_variant"), **common)
    else:
        log.warning("A3 is evaluated, not promoted (%s): not run on its %s split",
                    a3.get("dev_run"), split)
    a2_bias = a2_bias or config.REPORTS / "a2" / runs["a2"]["run_id"]
    report = bias.run(config.REPORTS / "a1" / runs["a1"]["run_id"], a2_bias,
                      out=config.REPORTS / "final" / final_id / "bias")
    main = gates["bias"]["main_metric"]
    verdicts = {t: gate(t, runs[t], report[t]["largest_gap"][main[t]]["gap"], gates,
                        on_insufficient=insufficient) for t in ("a1", "a2")}
    for task in ("a1_realistic", "a3"):  # not sliced for the bias gate
        if task in runs:
            verdicts[task] = gate(task, runs[task], None, gates, on_insufficient="skip")
    candidate = get_candidate(ccfg, proposed["candidate"], server=server,
                              quantization=proposed["quantization"])
    keep = {t: list(dict.fromkeys(["cases", *gates["hard"][t], *(gates["goals"].get(t) or {}),
                                   *(gates["score"].get(t) or {}), "latency_mean_s",
                                   "latency_p95_s"]))
            for t in runs}
    result = {
        "final_id": final_id, "kind": kind,
        "splits": {"a1": split, "a1_realistic": None if ci else "realistic", "a2": split,
                   "a3": split if with_a3 else None, "a2_bias": "dev" if ci else "loo_full"},
        "setting": {**{k: proposed[k] for k in SETTING_KEYS[:4]}, "a3": a3},
        "params_b": candidate.params_b,
        "models": ccfg["candidates"][proposed["candidate"]]["quantizations"][
            proposed["quantization"]],
        "prompt_versions": {t: runs[t]["params"]["prompt_version"] for t in tasks},
        "metrics": {t: {k: s.get(k) for k in keep[t]} for t, s in runs.items()},
        "gates": verdicts, "passed": all(v["passed"] for v in verdicts.values()),
        "runs": {**{t: s["run_id"] for t, s in runs.items()}, "a2_bias": a2_bias.name},
        "git_commit": git_commit(),
    }
    if reuse is not None:
        result["reused"] = {"from_final": reuse["final_id"],
                            "tasks": [t for t in ("a1", "a1_realistic", "a2", "a3")
                                      if (reuse.get("runs") or {}).get(t) == runs[t]["run_id"]],
                            "git_commit": reuse.get("git_commit")}
    folder = config.REPORTS / "final" / final_id
    write_json(folder / "final.json", result)
    (folder / "final.md").write_text(render_final(result))
    log.info("Final %s: %s", final_id, "PASSED" if result["passed"] else "FAILED gates")
    return result


def render_final(result: dict[str, Any]) -> str:
    from pantrypath_ml.charts import markdown_table, pct

    lines = [f"# Final confirmation {result['final_id']} ({result['kind']})", "",
             f"Setting: `{json.dumps(result['setting'])}`", "",
             f"**{'Passed' if result['passed'] else 'Failed'}** the hard and bias gates.", ""]
    if result.get("reused"):
        r = result["reused"]
        fresh = [t for t in result["runs"] if t not in r["tasks"] and t != "a2_bias"]
        lines += [f"**Combined final.** {', '.join(t.upper() for t in r['tasks'])} are the "
                  f"runs of final `{r['from_final']}` (commit `{r.get('git_commit')}`; the "
                  "same setting and prompt versions), reused as they are; "
                  + (f"{', '.join(t.upper() for t in fresh)} ran now" if fresh
                     else "no model ran") + f". Every gate below was applied again at "
                  f"commit `{result['git_commit']}`.", ""]
    a3 = result["setting"].get("a3") or {}
    if a3.get("status") == A3_NOT_PROMOTED:
        dev = a3.get("dev_results") or {}
        lines += ["**A3: evaluated, not promoted.** Not run on its test split and not part of "
                  "this setting: the app shows result.json without an A3 answer. Dev run "
                  f"`{a3.get('dev_run')}`: " + (", ".join(a3.get("failures") or [])
                                                 or "no failing gate") + ".", ""]
        if dev:
            lines += [markdown_table(["A3 dev metric", "Value"],
                                     [[k, pct(v) if k.endswith("_rate") else str(v)]
                                      for k, v in dev.items()]), ""]
        if a3.get("rescored_from"):
            lines += [f"A3 dev was rescored from {a3['rescored_from']}.", ""]
    met = {"passed": "yes", "not_gated": "n/a", "insufficient": "insufficient data"}
    for task, verdict in result["gates"].items():
        rows = [[m, pct(c["value"]),
                 f"≤ {pct(float(c['max']))}" if "max" in c else f"≥ {pct(float(c['min']))}",
                 "yes" if c["passed"] else "NO"] for m, c in verdict["hard"].items()]
        b = verdict["bias"]
        if b["status"] != "not_gated":
            failed = "NO" if b["blocking"] else "no (warning)"
            rows.append(["bias gap (max)" + ("" if b["blocking"] else ", warning only"),
                         pct(b["gap"]), f"≤ {pct(b['max'])}", met.get(b["status"], failed)])
        goals = [[m, pct(g["value"]), pct(float(g["target"])), "yes" if g["met"] else "no"]
                 for m, g in verdict["goals"].items()]
        split = (result.get("splits") or {}).get(task, "test")
        lines += [f"## {task.upper()} ({split})", "", "Gates:", "",
                  markdown_table(["Metric", "Value", "Limit", "Passed"], rows), ""]
        if goals:
            lines += ["Goals (section 8 of the plan; reported, not gated):", "",
                      markdown_table(["Metric", "Value", "Target", "Met"], goals), ""]
        lines += [f"Warning: {w}" for w in verdict["warnings"]] + [""] * bool(verdict["warnings"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pantrypath_ml.select",
                                     description="Gates, selection and the champion record")
    parser.add_argument("command", choices=["choose", "final", "promote"])
    parser.add_argument("--search-id", help="choose: the search to choose from")
    parser.add_argument("--runs", type=Path, nargs="+",
                        help="choose: existing run folders (reports/a1/<run>, reports/a2/<run>,"
                             " reports/a3/<run>) instead of a search")
    parser.add_argument("--write-proposed", action="store_true",
                        help="choose: also write registry/proposed.json")
    parser.add_argument("--exclude-a3", action="store_true",
                        help="choose: propose A1 and A2 only; A3 is recorded as evaluated, "
                             "not promoted")
    parser.add_argument("--proposed", type=Path, default=PROPOSED)
    parser.add_argument("--ci", action="store_true", help="final: a seeded dev sample")
    parser.add_argument("--server", help="final: override the proposed server")
    parser.add_argument("--reuse-final", type=Path,
                        help="final: an earlier full final.json of the same setting; reuse "
                             "its A1/A2 runs (and its A3 run, if it has one) and run only "
                             "what is missing")
    parser.add_argument("--rerun", nargs="+", choices=["a1", "a2", "a3"],
                        help="final with --reuse-final: run these tasks again instead of "
                             "reusing them (a1 = A1 test and realistic)")
    parser.add_argument("--no-mlflow", action="store_true")
    parser.add_argument("--final", type=Path, help="promote: a final.json")
    parser.add_argument("--latest-full", action="store_true",
                        help="promote: the newest passing full final for --proposed")
    args = parser.parse_args(argv)
    setup_logging()
    gates = config.load("gates")
    try:
        if args.command == "choose":
            if bool(args.search_id) == bool(args.runs):
                parser.error("choose needs --search-id or --runs")
            if args.runs:
                selection_id = new_run_id("runs")
                table, full_loo = run_rows(args.runs, gates)
                source = "runs " + ", ".join(f.name for f in args.runs)
            else:
                selection_id = args.search_id
                path = config.REPORTS / "search" / args.search_id / "comparison.json"
                table, full_loo = json.loads(path.read_text())["rows"], []
                source = f"search {args.search_id}"
            folder = config.REPORTS / "selection" / selection_id
            if args.runs:
                write_json(folder / "table.json", {"source": source, "rows": table})
            chosen = choose(table, gates, exclude_a3=args.exclude_a3)
            if chosen is None:
                for r in table:
                    log.error("%s %s: %s", r["task"], r["run_id"],
                              "; ".join(r["failures"]) or "passes")
                log.error("No setting passes every gate (%s)", source)
                return 1
            attach_final_bias_run(chosen, full_loo)
            chosen["source"] = source
            write_json(folder / "chosen.json", chosen)
            if args.write_proposed:
                write_json(args.proposed, chosen)
            print(json.dumps(chosen))
            return 0
        if args.command == "final":
            reuse = json.loads(args.reuse_final.read_text()) if args.reuse_final else None
            if args.rerun and not reuse:
                parser.error("--rerun goes with --reuse-final")
            result = final(json.loads(args.proposed.read_text()), ci=args.ci,
                           use_mlflow=not args.no_mlflow, server=args.server, reuse=reuse,
                           rerun=tuple(args.rerun or ()))
            print(json.dumps({k: result[k] for k in ("final_id", "passed", "metrics")}))
            return 0 if result["passed"] else 1
        path = args.final
        if args.latest_full:
            path = latest_full_final(json.loads(args.proposed.read_text()))
            if path is None:
                print(json.dumps({"promoted": False, "reason": "no passing full final report "
                                  "for the proposed setting; run `select final` and commit "
                                  "reports/final/<id>/"}))
                return 0
        if not path:
            parser.error("promote needs --final or --latest-full")
        if not is_committed(path):
            print(json.dumps({"promoted": False, "reason": f"{path} is not committed to Git: "
                              "commit reports/final/<id>/ first"}))
            return 0
        decision = promote(json.loads(path.read_text()), gates)
        print(json.dumps(decision))
        return 0
    except PipelineError as exc:
        log.error("select %s failed: %s", args.command, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
