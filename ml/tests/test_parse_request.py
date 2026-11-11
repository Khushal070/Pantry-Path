"""A1 parser: schema, cross-check (synonyms, negations, unsupported needs) and parse flow."""

import pytest
from conftest import ScriptedClient

from pantrypath_ml import config
from pantrypath_ml.parse_request import (
    PARSE_SCHEMA,
    CrossCheck,
    parse_request,
    prompt_version,
    render_prompt,
)


@pytest.fixture
def prcfg():
    return config.load("parse_request")


@pytest.fixture
def check(prcfg):
    return CrossCheck(prcfg["cross_check"])


def user(*texts):
    return [{"role": "user", "content": t} for t in texts]


# ---------------------------------------------------------------- schema and prompt


def test_schema_is_closed_and_built_from_request_info():
    assert set(PARSE_SCHEMA["properties"]) == {"query", "zip", "avoid", "diet", "extras"}
    assert PARSE_SCHEMA["required"] == list(PARSE_SCHEMA["properties"])
    assert PARSE_SCHEMA["additionalProperties"] is False
    assert PARSE_SCHEMA["properties"]["avoid"]["items"]["enum"][:2] == ["milk", "egg"]
    assert PARSE_SCHEMA["properties"]["extras"]["additionalProperties"] is False
    assert "$ref" not in str(PARSE_SCHEMA) and "pattern" not in str(PARSE_SCHEMA)
    assert PARSE_SCHEMA["properties"]["avoid"]["maxItems"] == 10


def test_prompt_variants(prcfg):
    chat = user("pad thai near 02115")
    base = render_prompt(prcfg, "base", chat)
    fewshot = render_prompt(prcfg, "fewshot_rules", chat)
    assert "Shopper: pad thai near 02115" in base and "Examples:" not in base
    assert fewshot.count("Answer:\n") == 3 and "Extra rules:" in fewshot
    assert prompt_version(prcfg, "fewshot") == "parse_request/1+fewshot"


def test_rules_v2_variant(prcfg):
    chat = user("pad thai near 02115")
    v2 = render_prompt(prcfg, "rules_v2", chat)
    assert v2.count("Answer:\n") == 5 and "Extra rules:" in v2
    assert "When you are unsure" not in v2 and "I can eat eggs" in v2
    assert "cod, salmon, tuna" in v2 and "tahini, sesame seeds -> sesame" in v2
    # the older variants are unchanged
    assert "When you are unsure" in render_prompt(prcfg, "rules", chat)
    assert render_prompt(prcfg, "fewshot", chat).count("Answer:\n") == 3


# ---------------------------------------------------------------- cross-check


@pytest.mark.parametrize(("text", "avoid"), [
    ("No dairy please", ["milk"]),
    ("I'm allergic to eggs and shrimp.", ["egg", "shellfish"]),
    ("I have celiac disease.", ["gluten"]),
    ("nut-free please", ["tree_nuts"]),
    ("no nuts", ["tree_nuts"]),
    ("allergic to peanuts", ["peanuts"]),
    ("peanut butter cookies please. I can't have soya", ["soy"]),
    ("Please leave out anything with tahini or almonds.", ["tree_nuts", "sesame"]),
    ("no onions, and I want shrimp scampi", []),
    ("I can't have milk, but eggs are fine", ["milk"]),
    ("No dairy, eggs are fine.", ["milk"]),
    ("I'm not allergic to eggs.", []),
    ("I want egg drop soup", []),
])
def test_allergen_scan(check, text, avoid):
    assert check.scan(user(text)).avoid == avoid


def test_later_messages_override_earlier_ones(check):
    chat = [{"role": "user", "content": "pad thai, no peanuts or soy"},
            {"role": "assistant", "content": "no fish? Got it."},
            {"role": "user", "content": "Sorry, actually peanuts are fine. Oh, and no fish."}]
    assert check.scan(chat).avoid == ["fish", "soy"]


@pytest.mark.parametrize(("text", "diet"), [
    ("I'm vegan.", "vegan"), ("No meat please.", "vegetarian"), ("I'm not vegan", "none"),
    ("I'm vegetarian, actually plant-based", "vegan"), ("chili for 4", "none"),
])
def test_diet_scan(check, text, diet):
    assert check.scan(user(text)).diet == diet


def test_unsupported_needs(check):
    scan = check.scan(user("It needs to be halal and low-sodium. I'm also allergic to mustard.",
                           "keto too"))
    assert scan.unsupported == ["halal", "keto", "low_sodium", "mustard_allergy"]
    assert check.scan(user("hot dogs with mustard")).unsupported == []  # not in an avoid phrase


def test_model_labels_are_mapped(check, prcfg):
    labels = prcfg["unsupported_labels"]
    assert check.unsupported_labels(["Halal", "low sodium diet", "allergic to celery",
                                     "something odd"], labels) == [
        "celery_allergy", "halal", "low_sodium", "something_odd"]


# ---------------------------------------------------------------- parse


def answer(**fields):
    base = {"query": "pad thai", "zip": "02115", "avoid": [], "diet": "none",
            "extras": {"servings": None, "tweaks": [], "unsupported": []}}
    return {**base, **fields}


def test_ok_when_the_model_agrees_with_the_scan(prcfg):
    client = ScriptedClient(lambda p: answer(avoid=["peanuts"]))
    result = parse_request(prcfg, user("pad thai near 02115, no peanuts"), client)
    assert result.status == "ok" and not result.conflicts
    assert result.request == {"query": "pad thai", "zip": "02115", "avoid": ["peanuts"],
                              "diet": "none"}


def test_a_missed_allergen_or_diet_needs_confirmation(prcfg):
    client = ScriptedClient(lambda p: answer(avoid=["peanuts"]))
    result = parse_request(prcfg, user("pad thai 02115, no peanuts or dairy. I'm vegan."),
                           client)
    assert result.status == "needs_confirmation"
    assert len(result.conflicts) == 2
    assert result.suggested["avoid"] == ["milk", "peanuts"] and result.suggested["diet"] == "vegan"


def test_extra_caution_from_the_model_is_kept(prcfg):
    client = ScriptedClient(lambda p: answer(avoid=["peanuts"], diet="vegetarian"))
    result = parse_request(prcfg, user("pad thai 02115, no peanuts"), client)
    assert result.status == "ok" and result.request["diet"] == "vegetarian"


def test_an_allergen_the_chat_never_mentions_is_asked_never_dropped(prcfg):
    client = ScriptedClient(lambda p: answer(avoid=["peanuts", "soy", "soy"]))
    result = parse_request(prcfg, user("pad thai 02115, no peanuts"), client)
    assert result.status == "needs_confirmation" and result.unmentioned == ["soy"]
    assert result.request["avoid"] == ["peanuts", "soy"]  # repeats dropped, soy kept
    assert result.suggested["avoid"] == ["peanuts", "soy"]
    assert result.conflicts == ["the model put soy in avoid, which the chat never mentions"]


@pytest.mark.parametrize("text", [
    "pad thai 02115, no peanuts. Actually peanuts are fine.",  # named, then retracted
    "pad thai 02115. I have celiac disease.",  # implied by a condition
])
def test_mentioned_allergens_are_not_flagged(prcfg, text):
    avoid = ["peanuts"] if "peanuts" in text else ["gluten"]
    result = parse_request(prcfg, user(text), ScriptedClient(lambda p: answer(avoid=avoid)))
    assert result.unmentioned == []


def test_unsupported_needs_always_reach_extras(prcfg):
    client = ScriptedClient(lambda p: answer())
    result = parse_request(prcfg, user("pad thai 02115. It must be kosher."), client)
    assert result.extras["unsupported"] == ["kosher"] and result.model_unsupported == []


def test_missing_zip_is_incomplete(prcfg):
    result = parse_request(prcfg, user("pad thai"), ScriptedClient(lambda p: answer(zip=None)))
    assert result.status == "incomplete" and result.missing == ["zip"]
    assert result.request is None and result.fields["query"] == "pad thai"


def test_an_invalid_zip_is_retried_once(prcfg):
    replies = iter([answer(zip="02115-1234"), answer()])
    client = ScriptedClient(lambda p: next(replies))
    result = parse_request(prcfg, user("pad thai 02115-1234"), client)
    assert result.status == "ok" and result.attempts == 2
    assert "could not be used" in client.prompts[1]


def test_two_unusable_replies_are_invalid(prcfg):
    client = ScriptedClient(lambda p: "The LLM reply is not a JSON object")
    result = parse_request(prcfg, user("pad thai 02115"), client)
    assert result.status == "invalid" and result.attempts == 2 and "JSON" in result.error
