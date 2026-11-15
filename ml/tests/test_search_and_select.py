"""Search grid, bias gaps, aggregation, gates, selection and the champion record."""

import json

import pytest
from conftest import dish_name, refine_reply

from pantrypath_ml import aggregate, bias, config, search, select
from pantrypath_ml.evalsets import write_a1
from pantrypath_pipeline.errors import PipelineError

# ---------------------------------------------------------------- grid


def test_local_grids_are_the_staged_search_that_was_run():
    scfg = config.load("search")
    stages = ["local_prompts", "local_temperature", "local_think", "local_loo"]
    tasks = [t for g in stages for t in search.tasks(scfg, g)]
    names = {search.task_name(t)[4:] for t in tasks}  # without the index
    assert names == {
        "a1-dev-qwen3-4b-4bit-base-t0-nothink", "a1-dev-qwen3-4b-4bit-rules_v2-t0-nothink",
        "a2-dev-qwen3-4b-4bit-refine_dish-t0-nothink",
        "a2-dev-qwen3-4b-4bit-refine_dish_unknown-t0-nothink",
        "a1-dev-qwen3-4b-4bit-rules_v2-t0.2-nothink",
        "a2-dev-qwen3-4b-4bit-refine_dish_unknown-t0.2-nothink",
        "a1-dev-qwen3-4b-4bit-rules_v2-t0-think",
        "a2-dev-qwen3-4b-4bit-refine_dish_unknown-t0-think",
        "a2-loo-qwen3-4b-4bit-refine_dish_unknown-t0-nothink"}
    assert len(names) == len(tasks)
    for grid in stages:
        part = search.tasks(scfg, grid)
        assert [t["index"] for t in part] == list(range(len(part)))


def test_explorer_grid_and_server_override(cfg):
    tasks = search.tasks(config.load("search"), "explorer")
    # A1: 2 quantizations x 3 temperatures x 1 think x 3 prompts; A2: 2 prompts x 2 splits
    assert len(tasks) == 2 * 3 * 3 + 2 * 3 * 2 * 2
    assert {t["server"] for t in tasks} == {"vllm"}
    with pytest.raises(Exception, match="Unknown grid"):
        search.tasks(config.load("search"), "cloud")


# ---------------------------------------------------------------- bias


def a2_record(cuisine, found, required=4, status="valid"):
    return {"cuisine": cuisine, "popularity": "high", "status": status,
            "score": {"required": required, "found": found, "core": 1, "core_in_reference": 1}}


def test_bias_gap_ignores_small_groups():
    records = [a2_record("american", 4)] * 10 + [a2_record("european", 3)] * 10 \
        + [a2_record("south_asian", 0)] * 2
    result = bias.analyze("a2", records, min_group_size=10)
    recall = result["metrics"]["recall"]["cuisine"]
    assert recall["groups"]["south_asian"] == {"value": 0.0, "n": 2}
    assert recall["too_small"] == ["south_asian"]
    assert recall["gap"] == 0.25 and recall["gap_between"] == ["european", "american"]
    assert result["largest_gap"]["recall"]["slice"] == "cuisine"


def test_bias_report_files(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    rows = [a2_record("american", 4)] * 3 + [a2_record("european", 2, status="error")]
    (folder / "dishes.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    report = bias.run(None, folder, out=tmp_path / "bias", min_group_size=1)
    assert report["a2"]["metrics"]["valid_profile_rate"]["cuisine"]["gap"] == 1.0
    assert (tmp_path / "bias" / "bias.md").read_text().startswith("# Bias report")
    assert list((tmp_path / "bias").glob("*.png"))


# ---------------------------------------------------------------- gates and selection


def test_gate_checks_the_hard_gates_and_the_gap_and_reports_the_goals():
    gates = config.load("gates")
    good = {"valid_profile_rate": 1.0, "recall": 0.8, "core_precision": 0.9}
    assert select.gate("a2", good, 0.05, gates)["passed"]
    below_goal = select.gate("a2", {**good, "recall": 0.7}, 0.05, gates)
    assert below_goal["passed"] and not below_goal["goals"]["recall"]["met"]
    verdict = select.gate("a2", {**good, "valid_profile_rate": 0.98}, 0.05, gates)
    assert not verdict["passed"] and len(verdict["failures"]) == 1
    assert select.gate("a2", good, None, gates)["passed"]  # no group big enough: not gated
    a1 = {"allergens_captured": 1.0, "diet_captured": 1.0, "unsupported_recall": 1.0,
          "valid_json_rate": 0.99, "dish_resolution": 0.5}
    assert select.gate("a1", a1, 0.0, gates)["passed"]
    assert not select.gate("a1", {**a1, "allergens_captured": 0.99}, 0.0, gates)["passed"]
    assert not select.gate("a1", {**a1, "diet_captured": 0.99}, 0.0, gates)["passed"]


def test_an_unmeasurable_bias_gap_is_reported_never_a_silent_pass():
    gates = config.load("gates")
    a1 = {"allergens_captured": 1.0, "diet_captured": 1.0, "unsupported_recall": 1.0,
          "valid_json_rate": 1.0}
    failed = select.gate("a1", a1, None, gates, on_insufficient="fail")  # A1 in final
    assert not failed["passed"] and failed["bias"]["status"] == "insufficient"
    assert "insufficient data" in failed["failures"][0]
    warned = select.gate("a1", a1, None, gates)  # search and CI
    assert warned["passed"] and warned["bias"]["status"] == "insufficient"
    assert not warned["bias"]["passed"] and "insufficient data" in warned["warnings"][0]
    # A2's bias gate doesn't block: insufficient data is a warning even in final
    a2 = select.gate("a2", {"valid_profile_rate": 1.0}, None, gates, on_insufficient="fail")
    assert a2["passed"] and a2["bias"]["status"] == "insufficient" and a2["warnings"]
    a3 = {"final_clean_rate": 1.0, "valid_json_rate": 1.0, "repaired_rate": 0.2}
    skipped = select.gate("a3", a3, None, gates, on_insufficient="skip")
    assert skipped["passed"] and skipped["bias"]["status"] == "not_gated"
    assert gates["bias"]["on_insufficient"]["final"] == "fail"


def test_a3_hard_gates_and_goals():
    gates = config.load("gates")
    good = {"final_clean_rate": 1.0, "valid_json_rate": 0.96, "repaired_rate": 0.5,
            "verdict_safety_rate": 0.5}
    verdict = select.gate("a3", good, None, gates, on_insufficient="skip")
    assert verdict["passed"] and not verdict["goals"]["verdict_safety_rate"]["met"]
    assert not select.gate("a3", {**good, "final_clean_rate": 0.99}, None, gates,
                           on_insufficient="skip")["passed"]
    assert not select.gate("a3", {**good, "valid_json_rate": 0.9}, None, gates,
                           on_insufficient="skip")["passed"]
    failed = select.gate("a3", {**good, "repaired_rate": 0.51}, None, gates,
                         on_insufficient="skip")  # a ceiling, not a floor
    assert not failed["passed"] and failed["failures"] == ["repaired_rate 0.51 > 0.5"]
    assert failed["hard"]["repaired_rate"] == {"value": 0.51, "max": 0.5, "passed": False}
    assert not select.gate("a3", {k: v for k, v in good.items() if k != "repaired_rate"},
                           None, gates, on_insufficient="skip")["passed"]  # missing fails
    assert ("a3", "verdict_safety_rate") in select.regression_metrics(gates)


def test_choose_takes_a3_from_its_dev_run_or_the_defaults():
    gates = config.load("gates")
    base = [row("a1", 4), row("a2", 4)]
    assert select.choose(base, gates)["a3"] == {"temperature": 0.0, "think": False}
    with_a3 = select.choose(base + [{**row("a3", 4), "prompt_variant": "hybrid"}], gates)
    assert with_a3["a3"] == {"prompt_variant": "hybrid", "temperature": 0.0, "think": False,
                             "dev_run": "a3-4"}
    assert select.choose(base + [row("a3", 4, passed=False)], gates) is None


def test_choose_can_leave_a_failing_a3_out_of_the_proposal():
    gates = config.load("gates")
    failing = {**row("a3", 4, passed=False, repaired_rate=0.93, final_clean_rate=1.0),
               "failures": ["repaired_rate 0.93 > 0.5"], "rescored_from": "last reply"}
    chosen = select.choose([row("a1", 4), row("a2", 4), failing], gates, exclude_a3=True)
    a3 = chosen["a3"]
    assert a3["status"] == "evaluated_not_promoted" and a3["dev_run"] == "a3-4"
    assert a3["failures"] == ["repaired_rate 0.93 > 0.5"] and not a3["passed"]
    assert a3["dev_results"]["repaired_rate"] == 0.93 and a3["rescored_from"] == "last reply"
    assert not select.a3_promoted(chosen) and select.a3_setting(chosen) == a3
    assert select.a3_promoted({"a3": {"temperature": 0.0}}) and select.a3_promoted({})


def test_final_skips_an_a3_that_is_not_promoted(monkeypatch, tmp_path):
    from pantrypath_ml import bias, evaluate_a1, evaluate_a2, evaluate_a3

    monkeypatch.setattr(config, "REPORTS", tmp_path)
    calls = []

    def fake_run(task):
        def run(candidate, split, **kw):
            calls.append((task, split))
            m = {**metrics()["a1"], "unsupported_recall": 1.0} if task == "a1" \
                else metrics()["a2"]
            return {"run_id": kw["run_id"], "params": {"prompt_version": f"{task}/1"},
                    "cases": 1, **m}
        return run
    monkeypatch.setattr(evaluate_a1, "run", fake_run("a1"))
    monkeypatch.setattr(evaluate_a2, "run", fake_run("a2"))
    monkeypatch.setattr(evaluate_a3, "run", lambda *a, **k: pytest.fail("A3 was run"))
    monkeypatch.setattr(select, "_bias_run", lambda proposed: tmp_path / "a2" / "full")
    gap = {"largest_gap": {"allergens_captured": {"gap": 0.0}, "recall": {"gap": 0.05}}}
    monkeypatch.setattr(bias, "run", lambda *a, **k: {"a1": gap, "a2": gap})
    proposed = {"candidate": "qwen3-4b", "quantization": "4bit", "server": "ollama",
                "a1": {"prompt_variant": "rules_v2", "temperature": 0.0, "think": False},
                "a2": {"prompt_variant": "refine_dish_unknown", "temperature": 0.0,
                       "think": False},
                "a3": {"status": "evaluated_not_promoted", "dev_run": "a3-dev",
                       "failures": ["repaired_rate 0.93 > 0.5"],
                       "dev_results": {"repaired_rate": 0.93}}}
    result = select.final(proposed, use_mlflow=False)
    assert sorted(calls) == [("a1", "realistic"), ("a1", "test"), ("a2", "test")]
    assert result["passed"] and "a3" not in result["gates"] and "a3" not in result["metrics"]
    assert result["splits"]["a3"] is None and set(result["prompt_versions"]) == {"a1", "a2"}
    assert result["setting"]["a3"]["status"] == "evaluated_not_promoted"
    report = (tmp_path / "final" / result["final_id"] / "final.md").read_text()
    assert "A3: evaluated, not promoted" in report and "repaired_rate 0.93 > 0.5" in report
    assert select.latest_full_final(proposed, tmp_path / "final") is not None


def test_bias_gap_blocks_a1_and_only_warns_for_a2():
    gates = config.load("gates")
    a1 = {"allergens_captured": 1.0, "diet_captured": 1.0, "unsupported_recall": 1.0,
          "valid_json_rate": 1.0}
    blocked = select.gate("a1", a1, 0.2, gates)
    assert not blocked["passed"] and blocked["failures"] == ["bias gap 0.2 > 0.1"]
    assert blocked["warnings"] == []
    warned = select.gate("a2", {"valid_profile_rate": 1.0}, 0.2, gates)
    assert warned["passed"] and not warned["bias"]["passed"] and warned["failures"] == []
    assert "catalog entries" in warned["warnings"][0]


def test_choose_records_the_bias_warning():
    gates = config.load("gates")
    table = [row("a1", 4), row("a2", 4, warnings=["bias gap 0.2 > 0.1. small groups"])]
    chosen = select.choose(table, gates)
    assert chosen["warnings"] == {"a2": ["bias gap 0.2 > 0.1. small groups"]}


def test_no_regression_covers_every_metric_without_a_hard_gate():
    metrics_ = select.regression_metrics(config.load("gates"))
    assert ("a1", "dish_resolution") in metrics_ and ("a1", "allergen_exact") in metrics_
    assert ("a2", "recall") in metrics_ and ("a2", "core_precision") in metrics_
    assert ("a1", "allergens_captured") not in metrics_  # hard: an absolute minimum instead
    assert ("a1", "diet_captured") not in metrics_
    assert ("a2", "valid_profile_rate") not in metrics_


def row(task, params_b, quant="4bit", passed=True, **metrics):
    return {"task": task, "candidate": f"m{params_b}", "quantization": quant, "server": "ollama",
            "params_b": params_b,
            "prompt_variant": "rules_v2" if task == "a1" else "refine_dish_unknown",
            "temperature": 0.0, "think": False, "passed": passed, "run_id": f"{task}-{params_b}",
            "latency_mean_s": 1.0, **metrics}


def test_choose_prefers_the_smallest_passing_model():
    gates = config.load("gates")
    table = [row("a1", 8, allergen_exact=1.0), row("a2", 8, recall=1.0),
             row("a1", 4, allergen_exact=0.8), row("a2", 4, recall=0.8),
             row("a1", 2, allergen_exact=1.0), row("a2", 2, passed=False, recall=1.0)]
    chosen = select.choose(table, gates)
    assert chosen["candidate"] == "m4"  # m2 fails A2; m4 is smaller than m8
    assert chosen["a2"]["prompt_variant"] == "refine_dish_unknown"
    assert select.choose([row("a1", 4)], gates) is None  # no passing A2 setting


def metrics(a1=1.0, a2=0.8):
    return {"a1": {"allergens_captured": 1.0, "diet_captured": 1.0, "unsupported_recall": 1.0,
                   "dish_resolution": 0.96, "valid_json_rate": 1.0, "allergen_exact": a1,
                   "diet_accuracy": 1.0, "zip_accuracy": 1.0},
            "a2": {"valid_profile_rate": 1.0, "recall": a2, "core_precision": 0.9}}


def final(kind="full", a2=0.8, passed=True, quant="4bit", params_b=4.0):
    return {"final_id": "f", "kind": kind, "passed": passed, "params_b": params_b,
            "setting": {"candidate": "qwen3-4b", "quantization": quant,
                        "a1": {"prompt_variant": "base", "temperature": 0.0, "think": False},
                        "a2": {"temperature": 0.0, "think": False}},
            "models": {"ollama": "qwen3:4b-q4_K_M"}, "prompt_versions": {},
            "metrics": metrics(a2=a2), "git_commit": "abc"}


def test_champion_is_replaced_only_by_a_better_setting(tmp_path):
    gates = config.load("gates")
    champ, prev = tmp_path / "champion.json", tmp_path / "previous.json"
    paths = {"champion_path": champ, "previous_path": prev}
    assert select.promote(final(), gates, **paths)["promoted"]
    assert not prev.exists()
    worse = select.promote(final(a2=0.78, quant="8bit"), gates, **paths)
    assert not worse["promoted"] and "worse" in worse["reason"]
    assert select.promote(final(a2=0.9, quant="8bit"), gates, **paths)["promoted"]
    assert json.loads(prev.read_text())["champion"]["quantization"] == "4bit"
    assert not select.promote(final(passed=False, a2=1.0), gates, **paths)["promoted"]


def test_a_regression_on_a_goal_metric_blocks_promotion(tmp_path):
    gates = config.load("gates")
    paths = {"champion_path": tmp_path / "c.json", "previous_path": tmp_path / "p.json"}
    select.promote(final(), gates, **paths)
    new = final(a2=0.9, quant="8bit")
    new["metrics"]["a1"]["dish_resolution"] = 0.955  # better score, dish resolution dropped
    decision = select.promote(new, gates, **paths)
    assert not decision["promoted"] and "a1.dish_resolution" in decision["reason"]


def test_tie_goes_to_the_smaller_model(tmp_path):
    gates = config.load("gates")
    paths = {"champion_path": tmp_path / "c.json", "previous_path": tmp_path / "p.json"}
    select.promote(final(params_b=8.0), gates, **paths)
    assert select.promote(final(params_b=4.0, quant="8bit"), gates, **paths)["promoted"]


def test_a_ci_result_is_never_promoted(tmp_path):
    gates = config.load("gates")
    paths = {"champion_path": tmp_path / "c.json", "previous_path": tmp_path / "p.json"}
    decision = select.promote(final(kind="ci", a2=1.0), gates, **paths)
    assert not decision["promoted"] and "never promoted" in decision["reason"]
    assert not (tmp_path / "c.json").exists()  # no champion written, even without one
    select.promote(final(), gates, **paths)
    assert not select.promote(final(kind="ci", quant="8bit", a2=1.0), gates,
                              **paths)["promoted"]


def test_promote_needs_a_committed_final_report(tmp_path, monkeypatch, capsys):
    report = tmp_path / "final.json"
    report.write_text(json.dumps(final()))
    monkeypatch.setattr(select, "is_committed", lambda path: False)
    assert select.main(["promote", "--final", str(report)]) == 0
    assert "not committed" in json.loads(capsys.readouterr().out.splitlines()[-1])["reason"]
    assert select.is_committed(report) is False  # a file Git doesn't track


def test_a_task_missing_from_the_old_champion_counts_as_zero(tmp_path):
    gates = config.load("gates")
    paths = {"champion_path": tmp_path / "c.json", "previous_path": tmp_path / "p.json"}
    select.promote(final(), gates, **paths)  # no a3 or a1_realistic metrics
    newer = final(a2=0.9, quant="8bit")
    newer["metrics"]["a3"] = {"verdict_safety_rate": 1.0, "coverage_rate": 1.0}
    assert select.promote(newer, gates, **paths)["promoted"]


def test_latest_full_final_matches_the_proposed_setting(tmp_path):
    report = final()
    report["setting"]["a3"] = select.a3_setting(report["setting"])
    for name, body in (("20261101T000000Z-final", report),
                       ("20261102T000000Z-final", {**report, "passed": False}),
                       ("20261103T000000Z-ci", {**report, "kind": "ci"})):
        (tmp_path / name).mkdir()
        (tmp_path / name / "final.json").write_text(json.dumps(body))
    found = select.latest_full_final(report["setting"], tmp_path)
    assert found is not None and found.parent.name == "20261101T000000Z-final"
    assert select.latest_full_final({**report["setting"], "quantization": "8bit"},
                                    tmp_path) is None


# ---------------------------------------------------------------- aggregate (fake model)


def test_search_aggregate_and_choose_end_to_end(pcfg, fake_server, monkeypatch):
    write_a1(pcfg, config.load("evalsets"))

    def model(prompt):
        if "dish catalog" in prompt:  # refine prompt (A2)
            return refine_reply(dish_name(prompt), ["lasagna noodles", "ground beef",
                                                    "ricotta cheese", "rice noodles", "peanuts",
                                                    "fish sauce", "tofu"])
        return {"query": "pad thai", "zip": "00000", "avoid": [], "diet": "none",
                "extras": {"servings": None, "tweaks": [], "unsupported": []}}
    fake_server(model)
    scfg = config.load("search")
    scfg.data["grids"]["local_prompts"]["limit"] = {"a1": None, "a2": None}
    monkeypatch.setattr(config, "load", lambda name, path=None, _load=config.load:
                        scfg if name == "search" else _load(name, path))
    tasks = search.tasks(scfg, "local_prompts")
    a1_task = next(t for t in tasks if t["task"] == "a1")
    a2_task = next(t for t in tasks if t["prompt_variant"] == "refine_dish")
    for t in (a1_task, a2_task):
        search.run_task(t, "s1", use_mlflow=False)
    out = aggregate.run("s1")
    assert out["settings"] == 2
    table = json.loads((config.REPORTS / "search" / "s1" / "comparison.json").read_text())["rows"]
    assert {r["task"] for r in table} == {"a1", "a2"}
    a1 = next(r for r in table if r["task"] == "a1")
    # the fake model never lists allergens, but the cross-check flags them all
    assert a1["allergens_captured"] == 1.0 and a1["allergen_exact"] < 1.0
    assert (config.REPORTS / "search" / "s1" / "comparison.md").is_file()
    chosen = select.choose(table, config.load("gates"))
    assert chosen["a2"]["prompt_variant"] == "refine_dish"

    # the same choice from the run folders, without the search
    folders = [str(config.REPORTS / r["task"] / r["run_id"]) for r in table]
    assert select.main(["choose", "--runs", *folders]) == 0
    picked = sorted((config.REPORTS / "selection").glob("*-runs-*/chosen.json"))
    assert {k: v for k, v in json.loads(picked[-1].read_text()).items() if k != "source"} \
        == chosen


def test_choose_from_runs_refuses_test_runs_and_non_run_folders(tmp_path):
    gates = config.load("gates")
    run = tmp_path / "r"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({"params": {"split": "test"}}))
    with pytest.raises(PipelineError, match="held-out run"):
        select.run_rows([run], gates)
    with pytest.raises(PipelineError, match="not a run folder"):
        select.run_rows([tmp_path], gates)


def a2_summary(run_id, split, **params):
    return {"task": "a2", "run_id": run_id,
            "params": {"candidate": "qwen3-4b", "quantization": "4bit", "split": split,
                       "prompt_variant": "refine_dish_unknown", "temperature": 0.0,
                       "think": False, **params}}


def test_a_full_leave_one_out_run_is_recorded_for_final_not_chosen_from(tmp_path):
    gates = config.load("gates")
    folders = []
    for s in (a2_summary("a2-old", "loo"),                      # before loo left them out
              a2_summary("a2-full", "loo_full"),
              a2_summary("a2-other", "loo_full", temperature=0.2)):
        folders.append(tmp_path / s["run_id"])
        folders[-1].mkdir()
        (folders[-1] / "summary.json").write_text(json.dumps(s))
    table, full = select.run_rows(folders, gates)
    assert table == [] and [s["run_id"] for s in full] == ["a2-old", "a2-full", "a2-other"]
    assert not select.is_full_loo(a2_summary("x", "loo", held_out_excluded=True)["params"])
    chosen = {"candidate": "qwen3-4b", "quantization": "4bit",
              "a2": {"prompt_variant": "refine_dish_unknown", "temperature": 0.0,
                     "think": False}}
    select.attach_final_bias_run(chosen, full)
    assert chosen["a2"]["final_bias_run"] == "a2-old"   # newest matching run id


def test_final_takes_the_full_leave_one_out_run_for_the_a2_bias(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REPORTS", tmp_path)
    proposed = {"candidate": "qwen3-4b", "quantization": "4bit",
                "a2": {"prompt_variant": "refine_dish_unknown", "temperature": 0.0,
                       "think": False, "bias_run": "a2-search"}}
    with pytest.raises(PipelineError, match="no a2.final_bias_run"):
        select._bias_run(proposed)
    for s in (a2_summary("a2-search", "loo", held_out_excluded=True),
              a2_summary("a2-full", "loo_full"),
              a2_summary("a2-hot", "loo_full", temperature=0.7)):
        (tmp_path / "a2" / s["run_id"]).mkdir(parents=True)
        (tmp_path / "a2" / s["run_id"] / "summary.json").write_text(json.dumps(s))
    for run_id, message in (("a2-search", "not a full leave-one-out"),
                            ("a2-hot", "not a leave-one-out run of the proposed")):
        proposed["a2"]["final_bias_run"] = run_id
        with pytest.raises(PipelineError, match=message):
            select._bias_run(proposed)
    proposed["a2"]["final_bias_run"] = "a2-full"
    assert select._bias_run(proposed) == tmp_path / "a2" / "a2-full"


def fake_final_runs(monkeypatch, tmp_path, a3_calls):
    """Reports for an earlier final f0 (A1, A1 realistic, A2, A3 runs); evaluators that fail
    for A1/A2 and record A3 calls; no-op bias."""
    from pantrypath_ml import bias, evaluate_a1, evaluate_a2, evaluate_a3

    monkeypatch.setattr(config, "REPORTS", tmp_path)
    a1 = {**metrics()["a1"], "cases": 1}
    a3 = {"cases": 1, "final_clean_rate": 1.0, "valid_json_rate": 1.0, "repaired_rate": 0.1}
    for task, run_id, m in (("a1", "f0-a1", a1), ("a1", "f0-a1-realistic", a1),
                            ("a2", "f0-a2", {**metrics()["a2"], "cases": 1}),
                            ("a3", "f0-a3", a3)):
        (tmp_path / task / run_id).mkdir(parents=True)
        (tmp_path / task / run_id / "summary.json").write_text(json.dumps(
            {"run_id": run_id, "params": {"prompt_version": f"{task}/1"}, **m}))
    monkeypatch.setattr(evaluate_a1, "run", lambda *a, **k: pytest.fail("A1 was rerun"))
    monkeypatch.setattr(evaluate_a2, "run", lambda *a, **k: pytest.fail("A2 was rerun"))

    def a3_run(candidate, split, **kw):
        a3_calls.append((split, kw["variant"]))
        return {"run_id": kw["run_id"], "params": {"prompt_version": "answer/2"}, **a3}
    monkeypatch.setattr(evaluate_a3, "run", a3_run)
    monkeypatch.setattr(select, "_bias_run", lambda proposed: tmp_path / "a2" / "full")
    gap = {"largest_gap": {"allergens_captured": {"gap": 0.0}, "recall": {"gap": 0.05}}}
    monkeypatch.setattr(bias, "run", lambda *a, **k: {"a1": gap, "a2": gap})


A1A2 = {"candidate": "qwen3-4b", "quantization": "4bit",
        "a1": {"prompt_variant": "rules_v2", "temperature": 0.0, "think": False},
        "a2": {"prompt_variant": "refine_dish_unknown", "temperature": 0.0,
               "think": False, "final_bias_run": "full"}}
HYBRID = {"prompt_variant": "hybrid", "temperature": 0.0, "think": False}


def test_a_combined_final_reuses_a1_and_a2_and_runs_only_a3(monkeypatch, tmp_path):
    a3_calls = []
    fake_final_runs(monkeypatch, tmp_path, a3_calls)
    proposed = {**A1A2, "server": "ollama", "a3": HYBRID}
    versions = select.current_prompt_versions(proposed)
    old = {"final_id": "f0", "kind": "full", "git_commit": "abc",
           "setting": {**A1A2, "a3": {"status": "evaluated_not_promoted"}},
           "prompt_versions": {t: versions[t] for t in ("a1", "a2")},
           "runs": {"a1": "f0-a1", "a1_realistic": "f0-a1-realistic", "a2": "f0-a2"}}
    result = select.final(proposed, use_mlflow=False, reuse=old)
    assert a3_calls == [("test", "hybrid")] and result["passed"]
    assert result["runs"]["a1"] == "f0-a1" and result["runs"]["a3"].endswith("-a3")
    assert result["reused"] == {"from_final": "f0", "tasks": ["a1", "a1_realistic", "a2"],
                                "git_commit": "abc"}
    assert set(result["gates"]) == {"a1", "a1_realistic", "a2", "a3"}
    report = (tmp_path / "final" / result["final_id"] / "final.md").read_text()
    assert "Combined final" in report and "`f0`" in report and "A3 ran now" in report
    # a different A1 or A2 setting can't be combined
    changed = {**proposed, "a1": {**proposed["a1"], "temperature": 0.2}}
    with pytest.raises(PipelineError, match="a1.temperature"):
        select.final(changed, use_mlflow=False, reuse=old)


def test_restamping_a_combined_final_reuses_every_run_and_runs_no_model(monkeypatch, tmp_path):
    a3_calls = []
    fake_final_runs(monkeypatch, tmp_path, a3_calls)
    monkeypatch.setattr("pantrypath_ml.registry.git_commit", lambda: "merged")
    proposed = {**A1A2, "server": "ollama", "a3": HYBRID}
    combined = {"final_id": "f1", "kind": "full", "git_commit": "branch",
                "setting": {**A1A2, "a3": HYBRID},
                "prompt_versions": select.current_prompt_versions(proposed),
                "runs": {"a1": "f0-a1", "a1_realistic": "f0-a1-realistic", "a2": "f0-a2",
                         "a3": "f0-a3"}}
    result = select.final(proposed, use_mlflow=False, reuse=combined)
    assert a3_calls == [] and result["passed"] and result["git_commit"] == "merged"
    assert result["runs"]["a3"] == "f0-a3" and result["reused"]["tasks"] == [
        "a1", "a1_realistic", "a2", "a3"]
    report = (tmp_path / "final" / result["final_id"] / "final.md").read_text()
    assert "no model ran" in report and "`merged`" in report
    # the same refusal rules for A3: variant, temperature and thinking
    for key, value in (("prompt_variant", "model_list"), ("temperature", 0.2),
                       ("think", True)):
        changed = {**proposed, "a3": {**HYBRID, key: value}}
        with pytest.raises(PipelineError, match=f"a3.{key}"):
            select.final(changed, use_mlflow=False, reuse=combined)
    # a prompt changed since the reused final: rerun
    stale = {**combined, "prompt_versions": {**combined["prompt_versions"], "a3": "answer/1"}}
    with pytest.raises(PipelineError, match="a3 prompt version answer/1"):
        select.final(proposed, use_mlflow=False, reuse=stale)
    # an A3 run can't stand for an A3 that is not promoted
    excluded = {**proposed, "a3": {"status": "evaluated_not_promoted"}}
    with pytest.raises(PipelineError, match="not promoted"):
        select.final(excluded, use_mlflow=False, reuse=combined)


def test_a_combined_final_can_rerun_a1_and_reuse_a2_and_a3(monkeypatch, tmp_path):
    from pantrypath_ml import evaluate_a1

    a3_calls = []
    fake_final_runs(monkeypatch, tmp_path, a3_calls)
    a1_calls = []

    def a1_run(candidate, split, **kw):
        a1_calls.append(split)
        return {"run_id": kw["run_id"], "params": {"prompt_version": "a1/2"}, "cases": 1,
                **metrics()["a1"]}
    monkeypatch.setattr(evaluate_a1, "run", a1_run)
    proposed = {**A1A2, "server": "ollama", "a3": HYBRID}
    combined = {"final_id": "f1", "kind": "full", "git_commit": "abc",
                "setting": {**A1A2, "a1": {**A1A2["a1"], "temperature": 0.5}, "a3": HYBRID},
                "prompt_versions": {**select.current_prompt_versions(proposed), "a1": "old"},
                "runs": {"a1": "f0-a1", "a1_realistic": "f0-a1-realistic", "a2": "f0-a2",
                         "a3": "f0-a3"}}
    with pytest.raises(PipelineError, match="a1.temperature"):   # A1 differs: not reusable
        select.final(proposed, use_mlflow=False, reuse=combined)
    result = select.final(proposed, use_mlflow=False, reuse=combined, rerun=("a1",))
    assert sorted(a1_calls) == ["realistic", "test"] and a3_calls == []
    assert result["reused"]["tasks"] == ["a2", "a3"] and result["runs"]["a2"] == "f0-a2"
    assert result["runs"]["a1"].endswith("-a1") and result["passed"]
