"""Dish refinement with a fake LLM: shares from the source, retries, failures, fallback."""

import copy
import json
import shutil
import string

import pytest

from pantrypath_pipeline import refine
from pantrypath_pipeline.errors import DataUnavailableError, LLMError
from pantrypath_pipeline.llm import LLMReply, LLMReplyError, Usage

SAMPLE_REPLIES = "llm_replies_sample.json"


class FakeLLM:
    """Answers refine prompts from canned data; a list of items is used in order."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, replies):
        self.replies = {k: v if isinstance(v, list) else [v] for k, v in replies.items()}
        self.prompts: dict[str, list[str]] = {}

    def complete_json(self, prompt, schema, *, purpose):
        dish_id = purpose.split(":", 1)[1]
        self.prompts.setdefault(dish_id, []).append(prompt)
        queue = self.replies[dish_id]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return LLMReply(data=copy.deepcopy(item), usage=Usage(1000, 300), model=self.model,
                        provider=self.provider)


@pytest.fixture
def pool_cfg(cfg, fixtures_dir):
    shutil.copy(fixtures_dir / "llm_pool_sample.json", cfg.path(cfg["foodcom"]["pool_output"]))
    return cfg


@pytest.fixture
def replies(fixtures_dir):
    return json.loads((fixtures_dir / SAMPLE_REPLIES).read_text())


def read_record(cfg, dish_id):
    return json.loads(refine.refined_path(cfg, dish_id).read_text())


def test_prompt_renders_every_placeholder(pool_cfg):
    template, version = refine.load_template(pool_cfg)
    pool = refine.load_pool(pool_cfg)
    lasagna = pool["dishes"][0]
    prompt = refine.render_prompt(pool_cfg, template, lasagna, [])
    assert version == "refine_dish/1"
    assert "$" not in prompt
    assert "- ricotta cheese: 0.81" in prompt and "(none)" in prompt
    assert "east_se_asian" in prompt and "spice_herb" in prompt


def test_merge_candidates_are_more_popular_look_alikes(pool_cfg):
    dishes = refine.load_pool(pool_cfg)["dishes"]
    by_id = {d["dish_id"]: d for d in dishes}
    found = refine.merge_candidates(by_id["classic-lasagna"], dishes, limit=5, min_score=50)
    assert [d["dish_id"] for d in found] == ["lasagna"]
    assert refine.merge_candidates(by_id["lasagna"], dishes, limit=5, min_score=50) == []


def test_run_all_writes_records_with_source_shares(pool_cfg, replies):
    summary = refine.run_all(pool_cfg, client=FakeLLM(replies))
    assert summary["dishes"] == 6 and summary["failed"] == 0
    assert (summary["kept"], summary["merged"], summary["dropped"]) == (4, 1, 1)

    lasagna = read_record(pool_cfg, "lasagna")["profile"]
    shares = {i["name"]: i for i in lasagna["ingredients"]}
    assert lasagna["name"] == "lasagna" and "lasagne" in lasagna["aliases"]
    assert shares["eggs"]["share"] == 0.5  # max of "egg" 0.5 and "eggs" 0.2, not the sum
    assert shares["lasagna noodles"]["added_by_llm"] is False  # in the source, if rarely
    assert [i["role"] for i in lasagna["ingredients"][:5]] == ["core"] * 5  # core first

    quiche = {i["name"]: i for i in read_record(pool_cfg, "quiche-lorraine")["profile"][
        "ingredients"]}
    assert quiche["pie crust"]["added_by_llm"] is True and quiche["pie crust"]["share"] == 0

    margarita = read_record(pool_cfg, "margarita")
    assert margarita["keep"] is False and margarita["profile"] is None
    assert read_record(pool_cfg, "classic-lasagna")["merge_into"] == "lasagna"

    report = json.loads(pool_cfg.path(pool_cfg["llm"]["usage_report"]).read_text())
    assert report["totals"]["calls_real"] == 6


def test_run_one_unknown_dish_fails_loudly(pool_cfg, replies):
    with pytest.raises(DataUnavailableError):
        refine.run_one(pool_cfg, "no-such-dish", client=FakeLLM(replies))


def test_bad_reply_gets_one_retry_with_the_error(pool_cfg, replies):
    client = FakeLLM({"quiche-lorraine": [{"keep": True}, replies["quiche-lorraine"]]})
    summary = refine.run_one(pool_cfg, "quiche-lorraine", client=client)
    assert summary["error"] is None and summary["keep"] is True
    first, second = client.prompts["quiche-lorraine"]
    assert "could not be used" not in first and "could not be used" in second
    assert len(read_record(pool_cfg, "quiche-lorraine")["calls"]) == 2


def test_two_bad_replies_are_recorded_not_raised(pool_cfg):
    bad = {"keep": True, "merge_into": None, "name": "quiche", "aliases": [],
           "cuisine": "european", "ingredients": [], "added": []}  # < 3 ingredients
    summary = refine.run_one(pool_cfg, "quiche-lorraine", client=FakeLLM({"quiche-lorraine": bad}))
    record = read_record(pool_cfg, "quiche-lorraine")
    assert summary["keep"] is False and "failed validation" in record["error"]
    assert record["profile"] is None and len(record["calls"]) == 2


def test_unusable_reply_is_retried_and_api_errors_are_not(pool_cfg, replies):
    cut_off = LLMReplyError("cut off", LLMReply({}, Usage(10, 16000), "fake-model", "fake"))
    client = FakeLLM({"lasagna": [cut_off, replies["lasagna"]]})
    assert refine.run_one(pool_cfg, "lasagna", client=client)["error"] is None
    assert read_record(pool_cfg, "lasagna")["usage"]["output_tokens"] == 16300

    client = FakeLLM({"lasagna": LLMError("LLM rate limit hit even after retries")})
    assert "rate limit" in refine.run_one(pool_cfg, "lasagna", client=client)["error"]
    assert len(client.prompts["lasagna"]) == 1


def test_merge_into_outside_the_candidates_is_ignored(pool_cfg, replies):
    reply = {**replies["beef-tacos"], "merge_into": "quiche-lorraine"}
    refine.run_one(pool_cfg, "beef-tacos", client=FakeLLM({"beef-tacos": reply}))
    record = read_record(pool_cfg, "beef-tacos")
    assert record["merge_into"] is None and record["profile"] is not None
    assert any("ignored merge_into" in n for n in record["notes"])


def test_duplicate_ingredients_are_merged(pool_cfg, replies):
    reply = copy.deepcopy(replies["beef-tacos"])
    reply["ingredients"].append({"name": "Ground  Beef", "from_raw": [], "role": "common",
                                 "category": "meat"})
    reply["added"].append({"name": "lettuce", "role": "core", "category": "produce", "why": "x"})
    refine.run_one(pool_cfg, "beef-tacos", client=FakeLLM({"beef-tacos": reply}))
    names = [i["name"] for i in read_record(pool_cfg, "beef-tacos")["profile"]["ingredients"]]
    assert names.count("ground beef") == 1 and names.count("lettuce") == 1


def test_profile_for_unknown_dish(cfg):
    reply = {"keep": True, "merge_into": None, "name": "shakshuka", "aliases": [],
             "cuisine": "middle_eastern_african", "added": [],
             "ingredients": [
                 {"name": "eggs", "from_raw": [], "role": "core", "category": "egg"},
                 {"name": "crushed tomatoes", "from_raw": [], "role": "core",
                  "category": "condiment"},
                 {"name": "bell pepper", "from_raw": [], "role": "common",
                  "category": "produce"}]}
    client = FakeLLM({"shakshuka": reply})
    profile = refine.profile_for_unknown_dish(cfg, "  Shakshuka ", client=client)
    assert profile.source == "llm" and profile.recipe_count == 0
    assert all(i.added_by_llm and i.share == 0 for i in profile.ingredients)
    assert "raw ingredients: (none)" in client.prompts["shakshuka"][0]

    not_a_dish = {**reply, "keep": False}
    with pytest.raises(LLMError, match="not a dish"):
        refine.profile_for_unknown_dish(cfg, "shakshuka", client=FakeLLM({"shakshuka": not_a_dish}))
    with pytest.raises(LLMError, match="empty"):
        refine.profile_for_unknown_dish(cfg, "   ", client=client)


def test_profile_for_unknown_dish_takes_a_template(cfg):
    reply = {"keep": True, "merge_into": None, "name": "shakshuka", "aliases": [],
             "cuisine": "middle_eastern_african", "added": [],
             "ingredients": [
                 {"name": "eggs", "from_raw": [], "role": "core", "category": "egg"},
                 {"name": "crushed tomatoes", "from_raw": [], "role": "core",
                  "category": "condiment"},
                 {"name": "bell pepper", "from_raw": [], "role": "common",
                  "category": "produce"}]}
    default, version = refine.load_template(cfg)
    other = string.Template("prompt_version: unknown_dish/1\n" + default.template)
    client = FakeLLM({"shakshuka": reply})
    profile = refine.profile_for_unknown_dish(cfg, "shakshuka", client=client,
                                              template=(other, "unknown_dish/1"))
    assert client.prompts["shakshuka"][0].startswith("prompt_version: unknown_dish/1")
    assert profile.source == "llm" and len(profile.ingredients) == 3
    # without a template, the pipeline's own prompt, as before
    plain = FakeLLM({"shakshuka": reply})
    refine.profile_for_unknown_dish(cfg, "shakshuka", client=plain)
    assert "unknown_dish/1" not in plain.prompts["shakshuka"][0]
    assert "raw ingredients: (none)" in plain.prompts["shakshuka"][0]
    assert version == "refine_dish/1"


def test_missing_pool_says_what_to_run(cfg):
    with pytest.raises(DataUnavailableError, match="select-dishes"):
        refine.load_pool(cfg)
