"""Catalog evaluation: name matching, scoring, and the written report."""

import json

import pytest

from pantrypath_pipeline import evaluate
from pantrypath_pipeline.errors import DataUnavailableError

SYNONYMS = [["lemon", "lemon juice"], ["egg", "egg yolk"]]


@pytest.mark.parametrize(("a", "b", "same"), [
    ("all-purpose flour", "flour", True),
    ("eggs", "egg", True),
    ("boneless skinless chicken breast", "chicken breasts", True),
    ("semisweet chocolate chips", "chocolate chips", True),
    ("tomatoes", "tomato", True),
    ("green onions", "onion", False),        # a different vegetable, not a modifier
    ("chicken broth", "chicken", False),     # different last word
    ("parmesan cheese", "mozzarella cheese", False),
    ("garlic", "garlic powder", False),
    ("lemon", "lemon juice", True),          # synonym group
    ("egg yolk", "eggs", True),
    ("", "flour", False),
])
def test_matcher(a, b, same):
    assert evaluate.Matcher(SYNONYMS).same(a, b) is same


def test_synonyms_are_needed_for_lemon_juice():
    assert not evaluate.Matcher().same("lemon", "lemon juice")


def ing(name, optional=False):
    return {"raw": name, "name": name, "category": "other", "optional": optional}


def profile(dish_id, *ingredients, aliases=()):
    """ingredients: (name, role, added_by_llm)"""
    return {"dish_id": dish_id, "name": dish_id.replace("-", " "), "aliases": list(aliases),
            "cuisine": "american",
            "ingredients": [{"name": n, "role": r, "added_by_llm": llm}
                            for n, r, llm in ingredients]}


CATALOG = [
    profile("quiche", ("eggs", "core", False), ("heavy cream", "core", False),
            ("pie crust", "core", True), ("swiss cheese", "common", False),
            ("cayenne pepper", "core", False), aliases=["quiche lorraine"]),
    profile("beef-stew", ("beef stew meat", "core", False), ("carrots", "core", False)),
]


def test_find_dish_by_alias_and_override():
    assert evaluate.find_dish("Quiche Lorraine", CATALOG, {})["dish_id"] == "quiche"
    assert evaluate.find_dish("Beef Stew with Red Wine", CATALOG, {}) is None
    found = evaluate.find_dish("Beef Stew with Red Wine", CATALOG,
                               {"beef stew with red wine": "beef-stew"})
    assert found["dish_id"] == "beef-stew"


def test_score_dish_counts_required_llm_and_core():
    recipe = {"dish": "Quiche Lorraine", "ingredients": [
        ing("pie crust"), ing("eggs"), ing("heavy cream"), ing("gruyere cheese"),
        ing("salt"), ing("nutmeg", optional=True)]}
    d = evaluate.score_dish(recipe, CATALOG[0], evaluate.Matcher(), skip={"salt"})
    # salt is skipped and nutmeg optional: 4 required, gruyere missed
    assert (d["required"], d["found"], d["missed"]) == (4, 3, ["gruyere cheese"])
    # pie crust came from the LLM, so without it one more is missed
    assert d["found_without_llm"] == 2 and d["missed_without_llm"] == ["pie crust",
                                                                       "gruyere cheese"]
    # 4 core ingredients, cayenne isn't in the reference
    assert (d["core"], d["core_in_reference"], d["core_not_in_reference"]) == (
        4, 3, ["cayenne pepper"])


def write_inputs(cfg, dev, test, catalog=CATALOG):
    golden = cfg.home / "golden"
    golden.mkdir()
    (golden / "dev.json").write_text(json.dumps(dev))
    (golden / "test.json").write_text(json.dumps(test))
    cfg.data["catalog_eval"]["splits"] = {"dev": "golden/dev.json", "test": "golden/test.json"}
    cfg.path(cfg["catalog"]["profiles_output"]).write_text(json.dumps({"dishes": catalog}))


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(evaluate, "send_alert",
                        lambda subject, body, level="warning", cfg=None:
                        sent.append((level, subject, body)))
    return sent


def test_run_writes_reports_per_split(cfg, alerts):
    dev = [{"dish": "Quiche Lorraine", "ingredients": [ing("eggs"), ing("pie crust")]}]
    test = [{"dish": "Beef Stew with Red Wine",
             "ingredients": [ing("beef chuck"), ing("carrots"), ing("water")]}]
    write_inputs(cfg, dev, test)
    summary = evaluate.run_catalog(cfg)

    report = json.loads(cfg.path(cfg["catalog_eval"]["output_json"]).read_text())
    assert summary["status"] == "ok" and alerts == []
    assert summary["dev_recall"] == 1.0
    # real config: the dish_ids override, the beef chuck synonym, water skipped
    assert report["splits"]["test"]["recall"] == 1.0
    assert report["splits"]["test"]["totals"]["required"] == 2
    assert report["splits"]["dev"]["recall_without_llm"] == 0.5
    markdown = cfg.path(cfg["catalog_eval"]["output_md"]).read_text()
    assert "| test | 1 | 100.0% | 100.0% |" in markdown
    assert "## test: per dish" in markdown


def test_low_held_out_recall_and_missing_dishes_alert(cfg, alerts):
    test = [{"dish": "Quiche Lorraine", "ingredients": [ing("eggs"), ing("bacon"),
                                                        ing("gruyere cheese")]},
            {"dish": "Pad Thai", "ingredients": [ing("rice noodles")]}]
    write_inputs(cfg, [], test)
    summary = evaluate.run_catalog(cfg)

    assert summary["status"] == "warning" and summary["test_recall"] == pytest.approx(0.3333)
    assert summary["dev_recall"] is None  # no dev recipes
    (level, _, body), = alerts
    assert level == "warning"
    assert "held-out recall 33.3% is below 75.0%" in body
    assert "test: not in the catalog: Pad Thai" in body


def test_missing_catalog_is_reported(cfg):
    with pytest.raises(DataUnavailableError, match="assemble-catalog"):
        evaluate.run_catalog(cfg)
