"""runtime.load: the champion's settings reach both clients and the A1 prompt."""

import json

import pytest
from conftest import ScriptedClient

from pantrypath_ml import local_llm, runtime
from pantrypath_pipeline.errors import PipelineError

CHAMPION = {"candidate": "qwen3-4b", "quantization": "4bit", "server": "ollama",
            "a1": {"prompt_variant": "fewshot", "temperature": 0.0, "think": False},
            "a2": {"temperature": 0.2, "think": False}}
REPLY = {"query": "pad thai", "zip": "02115", "avoid": ["peanuts"], "diet": "none",
         "extras": {"servings": None, "tweaks": [], "unsupported": []}}


@pytest.fixture
def clients(monkeypatch):
    """Every client make_client builds, with the settings it was given."""
    built = []

    def make(cfg, candidate, settings=None):
        scripted = ScriptedClient(lambda prompt: REPLY)
        built.append({"candidate": candidate, "settings": settings, "client": scripted})
        return scripted
    monkeypatch.setattr(local_llm, "make_client", make)
    return built


def test_load_configures_a1_and_a2_from_the_champion(pcfg, clients, tmp_path):
    path = tmp_path / "champion.json"
    path.write_text(json.dumps({"champion": CHAMPION}))
    models = runtime.load(path)
    assert models.variant == "fewshot" and models.versions["a1"] == "parse_request/1+fewshot"
    a1, a2, a3 = clients
    assert a1["candidate"].model == "qwen3:4b-q4_K_M"
    assert a1["settings"]["temperature"] == 0.0 and a2["settings"]["temperature"] == 0.2
    assert a3["settings"]["temperature"] == 0.0  # answer.yaml default: no a3 in the record
    assert a1["settings"]["max_output_tokens"] == 256  # per-task caps, thinking off
    assert a3["settings"]["max_output_tokens"] == 2048
    assert not hasattr(models.a3_client, "cache")  # A3 is never cached (Kroger data)

    result = models.parse([{"role": "user", "content": "pad thai near 02115, no peanuts"}])
    assert result.status == "ok" and result.request["avoid"] == ["peanuts"]
    assert "Examples:" in a1["client"].prompts[0]  # the champion's few-shot variant


def test_an_a3_that_is_not_promoted_gets_no_client(pcfg, clients, tmp_path):
    path = tmp_path / "champion.json"
    record = {**CHAMPION, "a3": {"status": "evaluated_not_promoted", "dev_run": "a3-dev"}}
    path.write_text(json.dumps({"champion": record}))
    models = runtime.load(path)
    assert len(clients) == 2 and not models.a3_available
    with pytest.raises(PipelineError, match="not promoted"):
        models.answer({"ingredients": []})


def test_no_champion_is_an_error(pcfg, clients, tmp_path):
    with pytest.raises(PipelineError, match="no champion"):
        runtime.load(tmp_path / "missing.json")
    (tmp_path / "empty.json").write_text(json.dumps({"champion": None}))
    with pytest.raises(PipelineError, match="no champion"):
        runtime.load(tmp_path / "empty.json")


def test_build_result_gets_the_champions_a2_prompt(pcfg, clients, tmp_path):
    from pantrypath_pipeline import refine

    path = tmp_path / "champion.json"
    record = {**CHAMPION, "a2": {**CHAMPION["a2"], "prompt_variant": "refine_dish_unknown"}}
    path.write_text(json.dumps({"champion": record}))
    models = runtime.load(path)
    assert models.versions["a2"] == "refine_dish_unknown/1"
    assert models.a2_template[1] == "refine_dish_unknown/1"

    def reply(prompt):
        assert prompt.startswith("prompt_version: refine_dish_unknown/1")
        return {"keep": True, "merge_into": None, "name": "mystery stew", "aliases": [],
                "cuisine": "american", "added": [],
                "ingredients": [{"name": n, "from_raw": [], "role": "core" if i == 0
                                 else "common", "category": "produce"}
                                for i, n in enumerate(["beans", "onion", "carrot"])]}
    # what build_result(llm_client=..., llm_template=models.a2_template) runs for a miss
    profile = refine.profile_for_unknown_dish(pcfg, "mystery stew", ScriptedClient(reply),
                                              template=models.a2_template)
    assert profile.source == "llm" and len(profile.ingredients) == 3
