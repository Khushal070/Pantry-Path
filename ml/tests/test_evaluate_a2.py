"""A2 evaluation sets and runner, with a fake client that returns fixed refine replies."""

import json

import pytest
from conftest import dish_name, refine_reply

from pantrypath_ml import config, evalsets, evaluate_a2
from pantrypath_ml.evalsets import HeldOutError, a2_cases, popularity_tiers
from pantrypath_pipeline.errors import PipelineError


def test_golden_dev_cases_use_the_pipeline_mapping_and_slices(pcfg):
    cases = a2_cases(pcfg, config.load("evalsets"), "dev")
    assert [c["name"] for c in cases] == ["Beef Lasagna", "Pad Thai"]
    lasagna = cases[0]
    assert (lasagna["dish_id"], lasagna["cuisine"], lasagna["popularity"]) == (
        "lasagna", "european", "high")
    assert lasagna["case_id"] == "beef-lasagna"


def test_test_split_is_never_read_unless_asked(pcfg, monkeypatch):
    read = []
    monkeypatch.setattr(evalsets, "read_recipes", lambda p: read.append(p) or [])
    a2_cases(pcfg, config.load("evalsets"), "dev")
    with pytest.raises(HeldOutError):
        a2_cases(pcfg, config.load("evalsets"), "test")
    assert all("golden_test" not in str(p) for p in read)
    a2_cases(pcfg, config.load("evalsets"), "test", allow_test=True)
    assert "golden_test" in str(read[-1])


def test_leave_one_out_uses_core_as_required(pcfg):
    cases = a2_cases(pcfg, config.load("evalsets"), "loo")
    lasagna = next(c for c in cases if c["case_id"] == "lasagna")
    assert [i["optional"] for i in lasagna["reference"]["ingredients"]][0] is False
    assert all(i["optional"] for i in lasagna["reference"]["ingredients"][1:])


def test_leave_one_out_never_asks_a_golden_test_dish(pcfg):
    from conftest import CATALOG

    cases = a2_cases(pcfg, config.load("evalsets"), "loo")
    held_out = evalsets.golden_test_dish_ids(pcfg, CATALOG)
    assert held_out == {"hummus"}  # the fake golden test set: one recipe, Hummus
    assert len(cases) == len(CATALOG) - 1
    assert not {c["dish_id"] for c in cases} & held_out
    assert not {c["name"].lower() for c in cases} & {"hummus"}


def test_full_leave_one_out_asks_every_dish_and_is_held_out(pcfg):
    from conftest import CATALOG

    with pytest.raises(HeldOutError):
        a2_cases(pcfg, config.load("evalsets"), "loo_full")
    cases = a2_cases(pcfg, config.load("evalsets"), "loo_full", allow_test=True)
    assert len(cases) == len(CATALOG) and "hummus" in {c["dish_id"] for c in cases}


def test_popularity_tiers_are_even():
    dishes = [{"dish_id": str(i), "review_count": i} for i in range(7)]
    tiers = popularity_tiers(dishes, ["high", "mid", "low"])
    assert [tiers[str(i)] for i in range(6, -1, -1)] == [
        "high", "high", "high", "mid", "mid", "low", "low"]


ANSWERS = {
    "beef lasagna": refine_reply("beef lasagna", ["lasagna noodles", "ground beef", "mozzarella"]),
    "pad thai": refine_reply("pad thai", ["rice noodles", "peanuts", "fish sauce", "tofu"]),
}


def answer(prompt):
    return ANSWERS[dish_name(prompt.split("Your previous answer")[0])]


def test_metrics_from_fixed_replies(pcfg, fake_server):
    fake_server(answer)
    summary = evaluate_a2.run("qwen3-4b", "dev", use_mlflow=False, run_id="r1")
    # lasagna: 2 of 3 required found (salt skipped, basil optional); pad thai: 4 of 4
    assert summary["recall"] == round(6 / 7, 4)
    # core: lasagna noodles (in reference), rice noodles (in reference)
    assert summary["core_precision"] == 1.0
    assert summary["valid_profile_rate"] == 1.0 and summary["false_refusal_rate"] == 0
    assert summary["llm_calls"] == 2 and summary["input_tokens"] == 200
    assert summary["slices"]["cuisine"]["east_se_asian"]["recall"] == 1.0
    folder = config.REPORTS / "a2" / "r1"
    rows = [json.loads(line) for line in (folder / "dishes.jsonl").read_text().splitlines()]
    assert rows[0]["score"]["found"] == 2 and rows[0]["latency_s"] >= 0
    assert json.loads((folder / "summary.json").read_text())["params"]["split"] == "dev"


def unknown_reply(name, ingredient_names):
    """A refine_dish_unknown reply: every ingredient in ingredients, from_raw and added empty."""
    return {"keep": True, "merge_into": None, "name": name, "aliases": [], "cuisine": "american",
            "ingredients": [{"name": n, "from_raw": [], "role": "core" if i == 0 else "common",
                             "category": "produce"} for i, n in enumerate(ingredient_names)],
            "added": []}


def test_refine_dish_unknown_variant(pcfg, fake_server):
    def answer_unknown(prompt):
        assert prompt.startswith("prompt_version: refine_dish_unknown/1")
        name = dish_name(prompt)
        return unknown_reply(name, ["lasagna noodles", "ground beef", "ricotta cheese"]
                             if name == "beef lasagna" else ["rice noodles", "tofu", "peanuts"])
    fake_server(answer_unknown)
    summary = evaluate_a2.run("qwen3-4b", "dev", variant="refine_dish_unknown",
                              use_mlflow=False, run_id="r4")
    assert summary["params"]["prompt_version"] == "refine_dish_unknown/1"
    assert summary["params"]["prompt_variant"] == "refine_dish_unknown"
    assert summary["valid_profile_rate"] == 1.0 and summary["recall"] == round(6 / 7, 4)
    rows = (config.REPORTS / "a2" / "r4" / "dishes.jsonl").read_text().splitlines()
    profile = json.loads(rows[0])["profile"]
    assert profile["source"] == "llm" and all(i["added_by_llm"] for i in profile["ingredients"])


def test_default_variant_is_refine_dish_unknown(pcfg, fake_server):
    fake_server(lambda prompt: unknown_reply(dish_name(prompt), ["rice noodles", "tofu", "egg"]))
    summary = evaluate_a2.run("qwen3-4b", "dev", use_mlflow=False, run_id="r5")
    assert summary["params"]["prompt_variant"] == "refine_dish_unknown"
    fake_server(answer)
    summary = evaluate_a2.run("qwen3-4b", "dev", variant="refine_dish", use_mlflow=False,
                              run_id="r6")
    assert summary["params"]["prompt_version"] == "refine_dish/1"
    with pytest.raises(PipelineError, match="Unknown A2 prompt variant"):
        evaluate_a2.run("qwen3-4b", "dev", variant="nope", use_mlflow=False)


def test_refusals_and_errors_are_counted(pcfg, fake_server):
    def refuse_or_fail(prompt):
        name = dish_name(prompt.split("Your previous answer")[0])
        if name == "beef lasagna":
            return refine_reply("beef lasagna", [], keep=False)
        return "The LLM reply is not a JSON object"
    scripted = fake_server(refuse_or_fail)
    summary = evaluate_a2.run("qwen3-4b", "dev", use_mlflow=False, run_id="r2")
    assert summary["false_refusal_rate"] == 0.5 and summary["error_rate"] == 0.5
    assert summary["valid_profile_rate"] == 0 and summary["recall"] is None
    assert len(scripted.prompts) == 3  # the unusable reply was retried once


def test_cli_default_split_is_dev(pcfg, fake_server, monkeypatch):
    seen = {}
    monkeypatch.setattr(evaluate_a2, "run", lambda c, split, **k: seen.update(split=split) or {})
    assert evaluate_a2.main(["--candidate", "qwen3-4b", "--no-mlflow"]) == 0
    assert seen["split"] == "dev"


def test_run_logs_to_mlflow_with_tags(pcfg, fake_server):
    import mlflow

    fake_server(answer)
    summary = evaluate_a2.run("qwen3-4b", "dev", run_id="r3", tags={"grid": "local"})
    run = mlflow.get_run(summary["mlflow_run_id"])
    assert run.data.tags["eval_split"] == "dev" and run.data.tags["grid"] == "local"
    assert run.data.metrics["recall"] == summary["recall"]
    assert run.data.params["think"] == "False"


def test_rescore_uses_the_current_matcher_without_the_model(pcfg, fake_server):
    fake_server(answer)
    before = evaluate_a2.run("qwen3-4b", "dev", variant="refine_dish", use_mlflow=False,
                             run_id="rs")
    folder = config.REPORTS / "a2" / "rs"
    fake_server(lambda prompt: pytest.fail("rescoring must not call the model"))
    pcfg.data["catalog_eval"]["synonyms"].append(["ricotta cheese", "mozzarella"])
    after = evaluate_a2.rescore(folder, pcfg)
    assert after["recall"] > before["recall"] and after["rescored"]["synonym_groups"] >= 1
    assert after["params"] == before["params"] and after["run_id"] == "rs"
    saved = json.loads((folder / "summary.before-rescore.json").read_text())
    assert saved["recall"] == before["recall"]
    assert json.loads((folder / "summary.json").read_text())["recall"] == after["recall"]
