"""A3: substitute screen, grounding check and repair, the answer flow, the synthetic
evaluation sets (validated against the real result.json contract) and the evaluator."""

import copy
import json
import random

import pytest
from conftest import CATALOG, ScriptedClient

from pantrypath_ml import config, evaluate_a3
from pantrypath_ml.answer import Facts, SubstituteCheck, answer, check, repair
from pantrypath_ml.evalsets import HeldOutError, generate_a3, held_out_dish_ids, load_a3, write_a3
from pantrypath_pipeline.errors import PipelineError


def product(store, name, verdict, source="kroger"):
    return {"store_id": store, "barcode": None, "name": name, "brand": "B", "size": "1 ct",
            "in_stock": True, "aisle": None, "verdict": verdict, "reason": "r",
            "evidence_source": source}


RESULT = {
    "request": {"query": "quiche", "zip": "45202", "avoid": ["milk", "gluten"],
                "diet": "vegetarian"},
    "dish": {"name": "quiche lorraine", "cuisine": "european", "source": "catalog"},
    "stores": [{"store_id": "S1", "chain": "KROGER", "name": "Testmart Vine", "address": "a",
                "lat": 39.1, "lon": -84.5, "distance_miles": 0.8}],
    "ingredients": [
        {"name": "eggs", "role": "core", "category": "egg",
         "products": [product("S1", "Acme Grade A Large Eggs", "safe")]},
        {"name": "heavy cream", "role": "core", "category": "dairy",
         "products": [product("S1", "Acme Heavy Whipping Cream", "unsafe")]},
        {"name": "pie crust", "role": "core", "category": "bakery",
         "products": [product("S1", "Bramble Pie Crust", "unknown", "none")]},
        {"name": "bacon", "role": "common", "category": "meat",
         "products": [product("S1", "Fenwick Smoked Bacon", "unsafe")]},
        {"name": "swiss cheese", "role": "common", "category": "dairy",
         "products": [product("S1", "Acme Swiss", "unsafe"),
                      product("S1", "Greenleaf Swiss Style Slices", "safe", "off")]},
    ],
    "summary": {"ingredients_total": 5, "with_safe_product": 2,
                "without_safe_product": ["heavy cream", "pie crust", "bacon"]},
    "meta": {},
}


def item(name, status, product=None, store_id=None, substitute=None, quantity=None,
         unit=None, note=None):
    return {"name": name, "status": status, "product": product, "store_id": store_id,
            "substitute": substitute, "quantity": quantity, "unit": unit, "note": note}


GOOD = {"summary": "A dairy-free vegetarian quiche.", "servings": 4,
        "steps": ["Whisk the eggs with coconut milk.", "Bake for 35 minutes."],
        "ingredients": [
            item("eggs", "buy", "Acme Grade A Large Eggs", "S1", quantity=4, unit="large"),
            item("heavy cream", "substitute", substitute="coconut milk", quantity=1,
                 unit="cup"),
            item("pie crust", "check_label", note="Read the label for wheat."),
            item("bacon", "skip"),
            item("swiss cheese", "buy", "Greenleaf Swiss Style Slices", "S1", quantity=4,
                 unit="slices")]}


@pytest.fixture
def acfg():
    return config.load("answer")


@pytest.fixture
def screen(acfg):
    return SubstituteCheck(acfg, config.load("parse_request"))


@pytest.mark.parametrize(("text", "avoid", "diet", "problems"), [
    ("coconut milk", ["milk"], "none", []),
    ("almond milk", ["milk", "tree_nuts"], "none", ["tree_nuts"]),
    ("oat milk", ["milk", "gluten"], "none", ["gluten"]),
    ("butter", ["milk"], "none", ["milk"]),
    ("flax egg", ["egg"], "vegan", []),
    ("rice flour", ["wheat", "gluten"], "none", []),
    ("all-purpose flour", ["gluten"], "none", ["gluten"]),
    ("tamari (gluten-free soy sauce)", ["gluten"], "none", []),
    ("soy sauce", ["gluten"], "none", ["gluten"]),
    ("egg-free mayo", ["egg"], "vegan", []),
    ("chicken broth", [], "vegetarian", ["vegetarian"]),
    ("vegetable broth", [], "vegetarian", []),
    ("honey", [], "vegan", ["vegan"]),
    ("tofu", ["soy"], "none", ["soy"]),
    ("vegetarian sausage", [], "vegetarian", []),                 # a meat-free product
    ("plant-based ground meat", [], "vegan", []),
    ("chicken sausage", [], "vegetarian", ["vegetarian"]),
    ("Gluten-Free All-Purpose Flour", ["gluten"], "none", []),    # descriptors between
    ("all-purpose flour", ["gluten"], "none", ["gluten"]),
])
def test_substitute_screen(screen, text, avoid, diet, problems):
    assert screen.problems(text, avoid, diet) == problems


def test_a_grounded_reply_passes(screen):
    assert check(GOOD, Facts.of(RESULT), screen) == []


def test_every_rule_break_is_found_and_repaired(screen):
    facts = Facts.of(RESULT)
    bad = copy.deepcopy(GOOD)
    bad["ingredients"][0]["product"] = "Acme Heavy Whipping Cream"       # not safe to buy
    bad["ingredients"][1].update(status="buy", product="Acme Heavy Whipping Cream")
    bad["ingredients"][2].update(status="substitute", substitute="wheat flour crust")
    bad["ingredients"][3].update(status="substitute", substitute="turkey bacon")
    del bad["ingredients"][4]                                               # missing
    bad["ingredients"].append(item("gruyere", "buy", "Some Gruyere", "S1"))  # invented
    bad["summary"] = "Great news. Bramble Pie Crust is safe for you."
    kinds = {v.kind for v in check(bad, facts, screen)}
    assert kinds == {"product", "buy_without_safe", "substitute", "missing", "invented",
                     "text_product"}
    fixed = repair(bad, facts, screen)
    assert check(fixed, facts, screen) == []
    assert [i["name"] for i in fixed["ingredients"]] == list(facts.roles)
    assert "Bramble" not in fixed["summary"]
    assert fixed["ingredients"][0]["product"] == "Acme Grade A Large Eggs"
    assert fixed["ingredients"][1]["substitute"] == "coconut milk"  # a good part is kept


def test_a_safe_product_at_the_wrong_store_is_caught_and_repaired(screen):
    facts = Facts.of(RESULT)
    bad = copy.deepcopy(GOOD)
    bad["ingredients"][0]["store_id"] = "S9"  # not where result.json lists it
    assert {v.kind for v in check(bad, facts, screen)} == {"store"}
    assert repair(bad, facts, screen)["ingredients"][0]["store_id"] == "S1"


@pytest.mark.parametrize(("text", "kinds"), [
    ("Crumble sausage on top.", {"text_conflict"}),              # meat, vegetarian
    ("Add a knob of butter.", {"text_conflict"}),                # avoided milk
    ("Stir in the heavy cream.", {"text_conflict"}),             # substituted, milk-avoider
    ("Fry the bacon until crisp.", {"text_conflict"}),           # skipped meat, vegetarian
    ("Use coconut milk instead of heavy cream.", set()),         # substitute, avoid phrase
    ("Replace the heavy cream with coconut milk.", set()),
    ("Swap the butter for olive oil.", set()),                   # replaced, not used
    ("Replace the sour cream with coconut milk.", set()),        # not in result.json
    ("Swap out the butter, then bake without butter.", set()),
    ("Use olive oil, not butter, without any cheese.", {"text_conflict"}),  # "not" is no cue
    ("Top with the swiss cheese.", set()),                       # bought: a safe product
    ("A dairy-free quiche with no bacon.", set()),
    ("Make it without milk, cheese or bacon.", set()),           # a list after the cue
    # the over-flags fixed in the hybrid redesign
    ("Use smoked mushrooms as a substitute for ham.", set()),
    ("Avoids milk and gluten.", set()),
    ("Top with vegetarian sausage.", set()),
    ("Dust the pan with Gluten-Free All-Purpose Flour.", set()),
    # found on the hybrid dev sample: a list that ran into the next phrase, a passive
    # replace, a three-word food
    ("Use coconut milk instead of heavy cream and swiss cheese instead of bacon.", set()),
    ("Bake with coconut milk, with the heavy cream replaced by coconut milk.", set()),
    ("Use coconut milk instead of the heavy cream and sharp cheddar cheese.", set()),
    ("Use coconut milk instead of heavy cream and then add butter.", {"text_conflict"}),
    ("Use lentils instead of sausage and dairy-free cheese instead of swiss cheese.", set()),
    ("Top with chicken sausage.", {"text_conflict"}),            # still meat
    ("Dust the pan with all-purpose flour.", {"text_conflict"}),  # still gluten
])
def test_steps_are_screened_for_the_avoid_list_and_diet(screen, text, kinds):
    reply = copy.deepcopy(GOOD)
    reply["steps"] = [text, "Bake for 35 minutes."]
    facts = Facts.of(RESULT)
    assert {v.kind for v in check(reply, facts, screen)} == kinds
    fixed = repair(reply, facts, screen)
    assert check(fixed, facts, screen) == [] and "Bake for 35 minutes." in fixed["steps"]


def with_status(name, status, **fields):
    reply = copy.deepcopy(GOOD)
    for i in reply["ingredients"]:
        if i["name"] == name:
            i.update({"status": status, "product": None, "store_id": None,
                      "substitute": None, **fields})
    return reply


def test_an_ingredient_name_is_free_only_when_it_is_bought(screen):
    cream = copy.deepcopy(RESULT)   # heavy cream with a safe product, so it can be bought
    cream["ingredients"][1]["products"] = [product("S1", "Greenleaf Oat Cream", "safe")]
    bought = with_status("heavy cream", "buy")
    bought["ingredients"][1].update(product="Greenleaf Oat Cream", store_id="S1")
    bought["steps"] = ["Stir in the heavy cream.", "Bake."]
    assert check(bought, Facts.of(cream), screen) == []
    # the same step with heavy cream substituted, for a milk-avoider
    substituted = copy.deepcopy(GOOD)
    substituted["steps"] = ["Stir in the heavy cream.", "Bake."]
    assert {v.kind for v in check(substituted, Facts.of(RESULT), screen)} == {"text_conflict"}
    # bacon skipped or substituted, for a vegetarian
    for reply in (with_status("bacon", "skip"),
                  with_status("bacon", "substitute", substitute="smoked mushrooms")):
        reply["steps"] = ["Fry the bacon until crisp.", "Bake."]
        assert {v.kind for v in check(reply, Facts.of(RESULT), screen)} == {"text_conflict"}
    replaced = copy.deepcopy(GOOD)
    replaced["steps"] = ["Replace the heavy cream with coconut milk.", "Bake."]
    assert check(replaced, Facts.of(RESULT), screen) == []


def test_notes_are_screened_too(screen):
    reply = with_status("bacon", "skip", note="Bacon adds a smoky taste.")
    kinds = check(reply, Facts.of(RESULT), screen)
    assert [v.kind for v in kinds] == ["text_conflict"]
    fixed = repair(reply, Facts.of(RESULT), screen)
    assert check(fixed, Facts.of(RESULT), screen) == [] and fixed["ingredients"][3]["note"] is None
    # a note on what a label is read for, or what to leave out, is fine
    ok = with_status("bacon", "skip", note="Skip the bacon, or use smoked paprika.")
    assert check(ok, Facts.of(RESULT), screen) == []


def test_repair_always_ends_clean(screen):
    names = [i["name"] for i in RESULT["ingredients"]] + ["gruyere"]
    products = [p["name"] for i in RESULT["ingredients"] for p in i["products"]] + ["X"]
    texts = ["ok.", "Fenwick Smoked Bacon works.", "Stir in the heavy cream.", "Add butter.",
             "Fry the bacon. Then bake.", "Replace the heavy cream with coconut milk.", None]
    rng = random.Random(0)
    for _ in range(500):
        dish = rng.choice(["quiche lorraine", "bacon cheese quiche", "cream pie"])
        facts = Facts.of({**RESULT, "dish": {**RESULT["dish"], "name": dish}})
        reply = {"summary": rng.choice(texts[:-1]), "servings": None,
                 "steps": [rng.choice(texts[:-1]), "Use Acme Swiss.", "Bake."],
                 "ingredients": [item(rng.choice(names), rng.choice(
                     ["buy", "substitute", "check_label", "skip"]), rng.choice([*products, None]),
                     "S1", rng.choice(["butter", "coconut milk", None]), note=rng.choice(texts))
                     for _ in range(rng.randint(0, 7))]}
        assert check(repair(reply, facts, screen), facts, screen) == []


def test_the_words_of_the_dish_name_are_free(screen):
    from pantrypath_ml.answer import dish_words

    assert {"deviled", "egg", "eggs"} <= set(dish_words("deviled eggs"))
    assert "of" not in dish_words("cream of chicken soup")   # short words are left out
    result = {**copy.deepcopy(RESULT), "dish": {**RESULT["dish"], "name": "bacon cheese quiche"}}
    facts = Facts.of(result)
    reply = copy.deepcopy(GOOD)
    reply["summary"] = "Your bacon cheese quiche, made vegetarian."
    assert check(reply, facts, screen) == []
    reply["summary"] = "Add the bacon last."   # a word of the dish's name: not a mention
    assert check(reply, facts, screen) == []
    reply["summary"] = "Add the sausage last."  # not in the name: still screened
    assert {v.kind for v in check(reply, facts, screen)} == {"text_conflict"}
    fixed = repair(reply, facts, screen)   # the fallback summary names the dish
    assert fixed["summary"] == "Here is your shopping list for bacon cheese quiche."
    assert check(fixed, facts, screen) == []


@pytest.mark.parametrize(("dish", "avoid", "diet", "text"), [
    ("zucchini bread", ["wheat", "gluten"], "none", "Pour the batter into a greased bread pan."),
    ("zucchini bread", ["wheat", "gluten"], "none", "Let the bread cool before slicing."),
    ("deviled eggs", ["egg"], "vegan", "Divide the mixture into 6 egg-shaped molds."),
    ("cream cheese chicken", ["milk"], "vegetarian", "Bake the cream cheese chicken."),
])
def test_dish_words_found_on_the_dev_set_are_not_mentions(screen, dish, avoid, diet, text):
    result = {**copy.deepcopy(RESULT), "dish": {**RESULT["dish"], "name": dish},
              "request": {"avoid": avoid, "diet": diet}}
    reply = {**copy.deepcopy(GOOD), "steps": [text, "Serve."]}
    assert "text_conflict" not in {v.kind for v in check(reply, Facts.of(result), screen)}


def text_reply(steps=("Whisk the eggs with the coconut cream.", "Bake for 35 minutes."),
               summary="A dairy-free vegetarian quiche.", quantities=()):
    return {"summary": summary, "servings": 4, "steps": list(steps),
            "quantities": [{"name": n, "quantity": q, "unit": u} for n, q, u in quantities]}


@pytest.fixture
def table(acfg):
    from pantrypath_ml.answer import SubstituteTable, load_table

    return SubstituteTable(load_table(acfg))


def test_code_decides_status_product_store_and_substitute(screen, table):
    from pantrypath_ml.answer import decide

    items = {i["name"]: i for i in decide(RESULT, Facts.of(RESULT), screen, table)}
    assert (items["eggs"]["status"], items["eggs"]["product"], items["eggs"]["store_id"]) \
        == ("buy", "Acme Grade A Large Eggs", "S1")
    # cream is milk: a label can't fix it, so a vetted substitute that passes the screen
    # (oat cream is skipped: oats for a gluten-avoider)
    assert (items["heavy cream"]["status"], items["heavy cream"]["substitute"]) \
        == ("substitute", "coconut cream")
    assert items["pie crust"]["status"] == "check_label"      # unverified products
    assert "read the label for milk, gluten" in items["pie crust"]["note"]
    assert (items["bacon"]["status"], items["bacon"]["substitute"]) \
        == ("substitute", "smoked mushrooms")                  # meat, vegetarian
    assert items["swiss cheese"]["product"] == "Greenleaf Swiss Style Slices"


def test_the_nearest_store_wins_and_without_a_candidate_common_items_are_skipped(screen, table):
    from pantrypath_ml.answer import decide

    result = copy.deepcopy(RESULT)
    result["stores"].append({**result["stores"][0], "store_id": "S0", "distance_miles": 0.2})
    result["ingredients"][0]["products"].append(product("S0", "Acme Grade A Large Eggs", "safe"))
    result["ingredients"].append({"name": "saffron", "role": "common", "category": "spice",
                                  "products": [product("S1", "Acme Saffron", "unsafe")]})
    result["ingredients"].append({"name": "lardons", "role": "core", "category": "meat",
                                  "products": []})
    items = {i["name"]: i for i in decide(result, Facts.of(result), screen, table)}
    assert items["eggs"]["store_id"] == "S0"
    assert items["saffron"]["status"] == "skip"                   # no candidate, common
    assert items["lardons"]["status"] == "substitute" and items["lardons"]["substitute"] is None
    assert "choose a substitute without milk, gluten" in items["lardons"]["note"]


def test_an_ingredient_that_is_itself_avoided_gets_a_substitute_not_a_label_check(screen, table):
    from pantrypath_ml.answer import decide

    result = copy.deepcopy(RESULT)
    result["ingredients"].append({"name": "butter", "role": "core", "category": "dairy",
                                  "products": [product("S1", "Acme Butter", "unknown", "none")]})
    items = {i["name"]: i for i in decide(result, Facts.of(result), screen, table)}
    assert (items["butter"]["status"], items["butter"]["substitute"]) == ("substitute",
                                                                          "vegan butter")


@pytest.mark.parametrize(("name", "avoid", "diet", "expected"), [
    ("cheddar cheese", ["milk"], "none", "dairy-free cheese"),     # the key ending the name
    ("chicken broth", [], "vegetarian", "vegetable broth"),          # broth, not chicken
    ("peanut butter", ["peanuts"], "none", "sunflower seed butter"),  # the longest key
    ("hamburger buns", ["gluten"], "none", "gluten-free buns"),      # plurals
    ("milk", ["milk", "gluten", "soy"], "none", "rice milk"),         # oat, soy screened out
    ("corn tortillas", ["gluten"], "none", "gluten-free tortillas"),  # not itself
    ("tortilla chips", ["gluten"], "none", None),                    # a key inside: no match
    ("tahini", ["sesame", "milk"], "none", "sunflower seed butter"),
    ("bacon", [], "vegan", "smoked mushrooms"),
    ("breakfast sausage", [], "vegetarian", "vegetarian sausage"),
    ("all-purpose flour", ["gluten"], "none", "gluten-free all-purpose flour"),
    ("all-purpose flour", ["wheat", "milk"], "vegan", "gluten-free all-purpose flour"),
])
def test_substitution_table(screen, table, name, avoid, diet, expected):
    facts = Facts.of({**RESULT, "request": {"avoid": avoid, "diet": diet}})
    assert table.pick(name, facts, screen) == expected


def test_every_decided_list_passes_the_check(screen, table):
    from pantrypath_ml.answer import assemble, decide

    ecfg = config.load("evalsets")
    cases = [c for split in generate_a3(CATALOG, ecfg["a3"], ecfg["a1"], screen).values()
             for c in split]
    rng = random.Random(0)
    for case in cases * 20:
        result = {**case["result"], "request": {
            **case["result"]["request"], "diet": rng.choice(["none", "vegetarian", "vegan"]),
            "avoid": rng.sample(["milk", "egg", "gluten", "wheat", "soy", "peanuts",
                                 "tree_nuts", "sesame", "fish", "shellfish"], rng.randint(0, 4))}}
        facts = Facts.of(result)
        reply, _ = assemble(decide(result, facts, screen, table),
                            text_reply(steps=["Cook it.", "Serve."], summary="Here it is."))
        assert check(reply, facts, screen) == []


def test_a_good_text_needs_one_call_and_its_quantities_are_used(acfg, screen):
    client = ScriptedClient(lambda prompt: text_reply(quantities=[
        ("eggs", 4, "large"), ("heavy cream", 1, "cup"), ("bacon", 4, "slices"),
        ("gruyere", 1, "cup")]))
    out = answer(acfg, RESULT, client, screen=screen)
    assert out.status == "ok" and out.attempts == 1 and out.violations == []
    items = {i["name"]: i for i in out.answer["ingredients"]}
    assert (items["eggs"]["quantity"], items["eggs"]["unit"]) == (4, "large")
    assert items["heavy cream"]["substitute"] == "coconut cream"   # decided, not written
    assert out.history[0]["ignored"] == ["gruyere"]                # not on the list
    assert [d["name"] for d in out.decisions] == list(Facts.of(RESULT).roles)


def test_answer_retries_then_repairs_and_adds_warnings(acfg, screen):
    client = ScriptedClient(lambda prompt: text_reply(
        steps=["Stir in the heavy cream.", "Bake for 35 minutes."],
        quantities=[("heavy cream", 1, "cup")]))
    out = answer(acfg, RESULT, client, extras={"servings": 4, "unsupported": ["halal"]},
                 clarify=["peanuts"], screen=screen)
    assert out.status == "repaired" and out.attempts == 2
    assert "could not be used" in client.prompts[1] and "heavy cream" in client.prompts[1]
    assert check(out.answer, Facts.of(RESULT), screen) == []
    assert out.answer["steps"] == ["Bake for 35 minutes."]
    assert out.raw["steps"][0] == "Stir in the heavy cream."  # the model's own text is kept
    text = " ".join(out.warnings)
    assert "couldn't verify" in text and "halal" in text and "Open Food Facts" in text
    assert "peanuts" in text and "estimates for 4 servings" in text
    assert "Substitutes are suggestions" in text


def test_every_attempt_is_kept(acfg, screen):
    bad = text_reply(steps=["Fry the bacon until crisp.", "Bake."])
    replies = iter(["The LLM reply is not a JSON object", bad])  # a string: an unusable reply
    out = answer(acfg, RESULT, ScriptedClient(lambda prompt: next(replies)), screen=screen)
    first, retry = out.history
    assert first["reply"] is None and first["text"] is None
    assert "not a JSON object" in first["error"]
    assert retry["text"] == bad and retry["reply"]["steps"] == bad["steps"]
    assert [v["kind"] for v in retry["violations"]] == ["text_conflict"]
    assert out.status == "repaired" and out.raw == retry["reply"]
    assert "Fry the bacon until crisp." not in out.answer["steps"]  # the repair


def test_prompt_shows_the_decided_list_only(acfg, screen, table):
    from pantrypath_ml.answer import decide, render_prompt

    items = decide(RESULT, Facts.of(RESULT), screen, table)
    prompt = render_prompt(acfg, RESULT, {"servings": 6}, items)
    assert '"use": "buy", "product": "Acme Grade A Large Eggs"' in prompt
    assert '"use": "substitute", "substitute": "coconut cream"' in prompt
    assert "Acme Heavy Whipping Cream" not in prompt and "Bramble" not in prompt
    assert "Acme Swiss" not in prompt.replace("Acme Swiss Style", "")  # only the safe one
    assert "Servings: 6" in prompt and "avoids: milk, gluten" in prompt


# ---------------------------------------------------------------- evaluation sets


def test_generated_results_follow_the_contract(pcfg, screen):
    ecfg = config.load("evalsets")
    splits = generate_a3(CATALOG, ecfg["a3"], ecfg["a1"], screen)  # validates each Result
    assert splits == generate_a3(CATALOG, ecfg["a3"], ecfg["a1"], screen)  # seeded
    cases = splits["dev"] + splits["test"]
    assert len(cases) == len(CATALOG) and splits["test"]
    for case in cases:
        result = case["result"]
        summary = result["summary"]
        assert summary["ingredients_total"] == len(result["ingredients"])
        assert not any(p["brand"].lower() == "kroger" for i in result["ingredients"]
                       for p in i["products"])  # synthetic products only
    a1 = ecfg["a1"]  # the same held-out dishes as A1
    assert {c["dish_id"] for c in splits["test"]} == held_out_dish_ids(CATALOG, a1["seed"],
                                                                  a1["test_fraction"])


def test_a3_test_split_is_held_out(pcfg):
    write_a3(pcfg, config.load("evalsets"))
    with pytest.raises(HeldOutError):
        load_a3(config.load("evalsets"), "test")
    assert load_a3(config.load("evalsets"), "dev")


def test_evaluator_scores_rules_before_repair(pcfg, fake_server):
    write_a3(pcfg, config.load("evalsets"))
    fake_server(lambda prompt: text_reply(steps=["Cook it.", "Serve."], summary="Here."))
    summary = evaluate_a3.run("qwen3-4b", "dev", use_mlflow=False, run_id="a3run")
    # the list is code's, so verdicts, coverage and substitutes hold by construction
    assert summary["valid_json_rate"] == 1.0 and summary["final_clean_rate"] == 1.0
    assert summary["verdict_safety_rate"] == summary["coverage_rate"] == 1.0
    assert summary["substitute_safety_rate"] == 1.0 and summary["repaired_rate"] == 0.0
    saved = json.loads((config.REPORTS / "a3" / "a3run" / "summary.json").read_text())
    assert saved["params"]["prompt_version"] == "answer/2"
    rows = [json.loads(line) for line in
            (config.REPORTS / "a3" / "a3run" / "cases.jsonl").read_text().splitlines()]
    assert all(r["result"]["decisions"] for r in rows)


def test_rescore_checks_the_saved_replies_again_without_the_model(pcfg, fake_server):
    write_a3(pcfg, config.load("evalsets"))
    fake_server(lambda prompt: text_reply(steps=["Cook it.", "Serve."], summary="Here."))
    before = evaluate_a3.run("qwen3-4b", "dev", limit=3, use_mlflow=False, run_id="a3re")
    folder = config.REPORTS / "a3" / "a3re"
    rows = [json.loads(line) for line in (folder / "cases.jsonl").read_text().splitlines()]
    rows[0]["result"].update(status="repaired", violations=[{"kind": "x"}])  # a stale check
    (folder / "cases.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    fake_server(lambda prompt: pytest.fail("rescore called the model"))
    after = evaluate_a3.rescore(folder)
    assert (folder / "summary.before-rescore.json").is_file() and "rescored" in after
    assert after["grounded_rate"] == before["grounded_rate"] == 1.0
    assert after["repaired_rate"] == before["repaired_rate"] == 0.0
    saved = [json.loads(line) for line in (folder / "cases.jsonl").read_text().splitlines()]
    assert saved[0]["result"]["status"] == "ok" and saved[0]["result"]["violations"] == []
    assert after["params"] == before["params"]
    assert after["rescored"]["history"] and after["rescored"]["retried_cases"] == 0


def test_rescore_prefers_a_first_reply_that_now_passes(acfg, screen):
    case = {"result": RESULT, "extras": {}}
    bad = copy.deepcopy(GOOD)
    bad["steps"] = ["Fry the bacon until crisp.", "Bake."]
    out = {"status": "repaired", "raw": bad, "attempts": 2, "violations": [{}],
           "history": [{"attempt": 1, "reply": GOOD, "error": "an old false alarm",
                        "violations": [{}]},
                       {"attempt": 2, "reply": bad, "error": "x", "violations": [{}]}]}
    again = evaluate_a3.recheck(acfg, case, out, screen)
    assert again["status"] == "ok" and again["raw"] == GOOD and again["answer"] == GOOD
    # saved before the history: only the last reply can be checked
    old = evaluate_a3.recheck(acfg, case, {k: v for k, v in out.items() if k != "history"},
                              screen)
    assert old["status"] == "repaired" and old["raw"] == bad


# ---------------------------------------------------------------- substitution table


def test_every_substitute_lists_what_it_contains():
    import yaml

    from pantrypath_ml.answer import DIET_TAGS
    from pantrypath_ml.parse_request import ALLERGEN_ORDER

    raw = yaml.safe_load((config.ML_HOME / "configs" / "substitutes.yaml").read_text())
    allowed = {*ALLERGEN_ORDER, *DIET_TAGS}
    missing = [(key, entry) for key, entries in raw["substitutes"].items() for entry in entries
               if not isinstance(entry, dict) or not isinstance(entry.get("contains"), list)]
    assert missing == [], f"entries without a contains list: {missing}"
    unknown = {(e["name"], c) for es in raw["substitutes"].values() for e in es
               for c in e["contains"] if c not in allowed}
    assert unknown == set()


def test_a_table_without_contains_is_refused(tmp_path):
    from pantrypath_ml.answer import SubstituteTable

    path = tmp_path / "substitutes.yaml"
    path.write_text("substitutes:\n  milk:\n    - {name: oat milk}\n")
    with pytest.raises(PipelineError, match="contains"):
        SubstituteTable(config.load("substitutes", path))
    path.write_text("substitutes:\n  milk:\n    - {name: oat milk, contains: [oats]}\n")
    with pytest.raises(PipelineError, match="unknown contains"):
        SubstituteTable(config.load("substitutes", path))


def test_tamari_is_kept_from_a_soy_avoider_by_its_contains_list(screen, table):
    from pantrypath_ml.answer import contains_conflicts

    tamari = next(c for c in table.candidates("soy sauce") if c["name"] == "tamari")
    assert set(tamari["contains"]) == {"soy", "wheat", "gluten"}   # some tamari has wheat
    assert not screen.problems("tamari", ["soy"], "none")   # the name screen alone misses it
    assert contains_conflicts(tamari["contains"], ["soy"], "none") == ["soy"]
    for avoid, expected in (([], "tamari"), (["soy"], "coconut aminos"),
                            (["soy", "gluten"], "coconut aminos")):
        facts = Facts.of({**RESULT, "request": {"avoid": avoid, "diet": "none"}})
        assert table.pick("soy sauce", facts, screen) == expected


def test_tamari_is_kept_from_gluten_and_wheat_avoiders(screen, table):
    for avoid in (["gluten"], ["wheat"]):
        facts = Facts.of({**RESULT, "request": {"avoid": avoid, "diet": "none"}})
        assert table.pick("soy sauce", facts, screen) == "coconut aminos"


def test_sunflower_seeds_are_kept_from_peanut_avoiders(screen, table):
    from pantrypath_ml.answer import contains_conflicts

    for key in ("peanuts", "walnuts", "pecans"):
        seeds = [c for c in table.candidates(key) if "sunflower" in c["name"]]
        assert seeds and all(contains_conflicts(c["contains"], ["peanuts"], "none")
                             for c in seeds)
    facts = Facts.of({**RESULT, "request": {"avoid": ["peanuts"], "diet": "none"}})
    assert table.pick("peanuts", facts, screen) == "roasted chickpeas"
    assert table.pick("walnuts", facts, screen) == "pumpkin seeds"
    facts = Facts.of({**RESULT, "request": {"avoid": ["tree_nuts"], "diet": "none"}})
    assert table.pick("walnuts", facts, screen) == "sunflower seeds"   # fine without peanuts


def test_seitan_is_kept_from_wheat_and_gluten_avoiders_by_its_contains_list(screen, table):
    from pantrypath_ml.answer import contains_conflicts

    seitan = next(c for c in table.candidates("chicken") if c["name"] == "seitan")
    assert set(seitan["contains"]) == {"wheat", "gluten", "soy"}
    assert contains_conflicts(seitan["contains"], ["wheat"], "vegan") == ["wheat"]
    assert contains_conflicts(seitan["contains"], ["gluten"], "none") == ["gluten"]
    for avoid, expected in ((["milk"], "extra-firm tofu"), (["soy"], "chickpeas"),
                            (["soy", "wheat"], "chickpeas"), (["soy", "gluten"], "chickpeas")):
        facts = Facts.of({**RESULT, "request": {"avoid": avoid, "diet": "vegetarian"}})
        assert table.pick("chicken", facts, screen) == expected


def test_contains_diet_tags():
    from pantrypath_ml.answer import contains_conflicts

    assert contains_conflicts(["meat"], [], "vegetarian") == ["vegetarian"]
    assert contains_conflicts(["fish"], [], "vegetarian") == ["vegetarian"]
    assert contains_conflicts(["egg"], [], "vegetarian") == []
    assert contains_conflicts(["egg"], [], "vegan") == ["vegan"]
    assert contains_conflicts(["animal"], [], "vegan") == ["vegan"]
    assert contains_conflicts(["soy", "milk"], ["milk"], "vegan") == ["milk", "vegan"]


# ---------------------------------------------------------------- variants


def test_the_first_version_is_still_selectable(acfg, screen):
    from pantrypath_ml.answer import load_prompt, variant_of_version

    assert load_prompt(acfg, "model_list")[1] == "answer/1"
    assert load_prompt(acfg, "hybrid")[1] == load_prompt(acfg)[1] == "answer/2"
    assert variant_of_version(acfg, "answer/1") == "model_list"
    client = ScriptedClient(lambda prompt: GOOD)   # the model writes the whole answer
    out = answer(acfg, RESULT, client, screen=screen, variant="model_list")
    assert out.status == "ok" and out.answer == GOOD and out.decisions == []
    assert "SAFE" in client.prompts[0] and "Acme Grade A Large Eggs" in client.prompts[0]
    with pytest.raises(PipelineError, match="Unknown A3 prompt variant"):
        answer(acfg, RESULT, client, screen=screen, variant="nope")


def test_every_a3_report_records_its_variant(pcfg, fake_server):
    write_a3(pcfg, config.load("evalsets"))
    fake_server(lambda prompt: GOOD if "SAFE" in prompt else
                text_reply(steps=["Cook it.", "Serve."], summary="Here."))
    for variant, version in (("hybrid", "answer/2"), ("model_list", "answer/1")):
        s = evaluate_a3.run("qwen3-4b", "dev", limit=2, use_mlflow=False,
                            run_id=f"a3-{variant}", variant=variant)
        assert (s["params"]["prompt_variant"], s["params"]["prompt_version"]) == (variant,
                                                                                  version)
    # a run saved before the variants gets its variant from the prompt version
    from pantrypath_ml.aggregate import prompt_variant

    assert prompt_variant({"task": "a3", "params": {"prompt_version": "answer/1"}}) \
        == "model_list"


def test_the_swaps_variant_lists_every_swap(acfg, screen, table):
    from pantrypath_ml.answer import decide, load_prompt, render_prompt

    template, version = load_prompt(acfg, "hybrid_swaps")
    assert version == "answer/3" and load_prompt(acfg, "hybrid")[1] == "answer/2"
    result = copy.deepcopy(RESULT)
    result["ingredients"].append({"name": "saffron", "role": "common", "category": "spice",
                                  "products": [product("S1", "Acme Saffron", "unsafe")]})
    items = decide(result, Facts.of(result), screen, table)
    prompt = render_prompt(acfg, result, {}, items, template)
    assert "- use coconut cream instead of heavy cream" in prompt
    assert "- use smoked mushrooms instead of bacon" in prompt
    assert "- leave out saffron" in prompt and "instead of eggs" not in prompt  # eggs bought
    assert "$swaps" not in render_prompt(acfg, result, {}, items)  # answer/2 ignores it
    client = ScriptedClient(lambda p: text_reply())
    out = answer(acfg, result, client, screen=screen, variant="hybrid_swaps")
    assert out.status == "ok" and "Swaps (follow every one)" in client.prompts[0]
