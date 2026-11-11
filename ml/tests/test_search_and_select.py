"""Search grid, bias gaps, aggregation, gates, selection and the champion record."""

import json

import pytest
from conftest import dish_name, refine_reply

from pantrypath_ml import aggregate, bias, config, search, select
from pantrypath_ml.evalsets import write_a1

# ---------------------------------------------------------------- grid


def test_local_grid_is_two_prompts_two_temperatures_think_on_off():
    tasks = search.tasks(config.load("search"), "local")
    a1 = [t for t in tasks if t["task"] == "a1"]
    a2 = [t for t in tasks if t["task"] == "a2"]
    assert len(a1) == 2 * 2 * 2 and len(a2) == 2 * 2
    assert {t["think"] for t in tasks} == {False, True}
    assert [t["index"] for t in tasks] == list(range(len(tasks)))
    assert len({search.task_name(t) for t in tasks}) == len(tasks)


def test_explorer_grid_and_server_override(cfg):
    tasks = search.tasks(config.load("search"), "explorer")
    # A1: 2 quantizations x 3 temperatures x 1 think x 3 prompts; A2: x 2 splits
    assert len(tasks) == 2 * 3 * 3 + 2 * 3 * 2
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


def test_gate_checks_every_target_and_the_gap():
    gates = config.load("gates")
    good = {"valid_profile_rate": 1.0, "recall": 0.8, "core_precision": 0.9}
    assert select.gate("a2", good, 0.05, gates)["passed"]
    verdict = select.gate("a2", {**good, "recall": 0.7}, 0.2, gates)
    assert not verdict["passed"] and len(verdict["failures"]) == 2
    assert select.gate("a2", good, None, gates)["passed"]  # no group big enough: not gated


def row(task, params_b, quant="4bit", passed=True, **metrics):
    return {"task": task, "candidate": f"m{params_b}", "quantization": quant, "server": "ollama",
            "params_b": params_b, "prompt_variant": "base" if task == "a1" else None,
            "temperature": 0.0, "think": False, "passed": passed, "run_id": f"{task}-{params_b}",
            "latency_mean_s": 1.0, **metrics}


def test_choose_prefers_the_smallest_passing_model():
    gates = config.load("gates")
    table = [row("a1", 8, allergen_exact=1.0), row("a2", 8, recall=1.0),
             row("a1", 4, allergen_exact=0.8), row("a2", 4, recall=0.8),
             row("a1", 2, allergen_exact=1.0), row("a2", 2, passed=False, recall=1.0)]
    chosen = select.choose(table, gates)
    assert chosen["candidate"] == "m4"  # m2 fails A2; m4 is smaller than m8
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
    assert select.promote(final(), "full", gates, **paths)["promoted"]
    assert not prev.exists()
    worse = select.promote(final(a2=0.78, quant="8bit"), "full", gates, **paths)
    assert not worse["promoted"] and "worse" in worse["reason"]
    assert select.promote(final(a2=0.9, quant="8bit"), "full", gates, **paths)["promoted"]
    assert json.loads(prev.read_text())["champion"]["quantization"] == "4bit"
    assert not select.promote(final(passed=False, a2=1.0), "full", gates, **paths)["promoted"]


def test_tie_goes_to_the_smaller_model(tmp_path):
    gates = config.load("gates")
    paths = {"champion_path": tmp_path / "c.json", "previous_path": tmp_path / "p.json"}
    select.promote(final(params_b=8.0), "full", gates, **paths)
    assert select.promote(final(params_b=4.0, quant="8bit"), "full", gates, **paths)["promoted"]


def test_never_replaced_without_comparable_metrics(tmp_path):
    gates = config.load("gates")
    paths = {"champion_path": tmp_path / "c.json", "previous_path": tmp_path / "p.json"}
    select.promote(final(kind="full"), "full", gates, **paths)
    other = select.promote(final(kind="ci", quant="8bit", a2=1.0), "ci", gates, **paths)
    assert not other["promoted"]
    same = select.promote(final(kind="ci"), "ci", gates, **paths)
    assert not same["promoted"] and "baseline" in same["reason"]
    record = json.loads((tmp_path / "c.json").read_text())["champion"]
    assert set(record["metrics"]) == {"full", "ci"}


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
    scfg.data["grids"]["local"]["limit"] = {"a1": 3, "a2": None}
    monkeypatch.setattr(config, "load", lambda name, path=None, _load=config.load:
                        scfg if name == "search" else _load(name, path))
    for t in search.tasks(scfg, "local")[:1] + search.tasks(scfg, "local")[-1:]:
        search.run_task(t, "s1", use_mlflow=False)
    out = aggregate.run("s1")
    assert out["settings"] == 2
    table = json.loads((config.REPORTS / "search" / "s1" / "comparison.json").read_text())["rows"]
    assert {r["task"] for r in table} == {"a1", "a2"}
    a1 = next(r for r in table if r["task"] == "a1")
    # the fake model never lists allergens, but the cross-check flags them all
    assert a1["allergens_captured"] == 1.0 and a1["allergen_exact"] < 1.0
    assert not a1["passed"] and any("dish_resolution" in f for f in a1["failures"])
    assert (config.REPORTS / "search" / "s1" / "comparison.md").is_file()
    assert select.choose(table, config.load("gates")) is None
