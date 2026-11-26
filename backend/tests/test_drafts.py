from app.drafts import build_draft, confirmed_request
from app.engines.base import Issue, ParseOutcome

PROFILE = {"zip": "45202", "avoid": ["milk", "gluten"], "diet": "vegetarian", "servings": 4,
           "needs": ["kosher"]}


def outcome(**kw) -> ParseOutcome:
    return ParseOutcome(**{"status": "ok", "query": "quiche lorraine", **kw})


def test_profile_and_chat_combine_cautiously():
    draft = build_draft(outcome(avoid=["egg", "milk"], diet="none", zip=None,
                                extras={"servings": None, "unsupported": ["halal"]}), PROFILE)
    assert draft["state"] == "ready"
    assert draft["request"] == {"query": "quiche lorraine", "zip": "45202",
                                "avoid": ["milk", "egg", "gluten"], "diet": "vegetarian"}
    assert draft["sources"]["avoid"] == {"milk": "both", "egg": "chat", "gluten": "profile"}
    assert draft["sources"]["zip"] == "profile"
    assert draft["sources"]["diet"] == "profile"
    assert draft["locked"] == ["milk", "gluten"]
    assert draft["servings"] == 4
    assert draft["needs"] == ["halal", "kosher"]
    assert draft["extras"]["unsupported"] == ["halal", "kosher"]


def test_the_chats_zip_and_servings_win_and_a_stricter_chat_diet_wins():
    draft = build_draft(outcome(zip="10001", diet="vegan", extras={"servings": 2}), PROFILE)
    assert draft["request"]["zip"] == "10001"
    assert draft["sources"]["zip"] == "chat"
    assert draft["request"]["diet"] == "vegan"
    assert draft["sources"]["diet"] == "chat"
    assert draft["servings"] == 2


def test_no_zip_anywhere_asks_for_one():
    assert build_draft(outcome(), {"avoid": [], "diet": "none"})["state"] == "need_zip"


def test_no_dish_and_unreadable_chats():
    assert build_draft(outcome(query=None), PROFILE)["state"] == "need_dish"
    draft = build_draft(ParseOutcome(status="invalid", error="bad json"), PROFILE)
    assert draft == {"state": "invalid", "error": "bad json"}


def test_doubts_the_profile_already_covers_are_dropped():
    issues = [Issue("missed_allergen", allergen="milk"),        # profile avoids milk
              Issue("unmentioned", allergen="gluten"),          # profile avoids gluten
              Issue("diet", value="vegetarian"),                # profile is vegetarian
              Issue("clarify", allergen="peanuts"),             # not covered: kept
              Issue("other", value="something new")]            # never dropped
    draft = build_draft(outcome(avoid=["tree_nuts", "peanuts"], issues=issues), PROFILE)
    assert [i["kind"] for i in draft["issues"]] == ["clarify", "other"]
    assert draft["state"] == "check"


def test_a_profile_allergen_the_chat_calls_fine_is_kept_with_a_note():
    draft = build_draft(outcome(avoid=[], mentioned=["milk"], chat_avoid=[]), PROFILE)
    assert "milk" in draft["request"]["avoid"]
    assert draft["notes"] == ["Your preferences avoid milk, so I kept it. Change that in "
                              "Preferences."]


def test_a_confirmed_card_cant_drop_profile_restrictions():
    draft = build_draft(outcome(avoid=["tree_nuts", "peanuts"], clarify=["peanuts"],
                                extras={"servings": None}), PROFILE)
    body = {"query": "quiche", "zip": "10001", "avoid": ["tree_nuts"], "diet": "none",
            "servings": 6}
    request, extras, clarify = confirmed_request(draft, body, PROFILE)
    assert request == {"query": "quiche", "zip": "10001",
                       "avoid": ["milk", "tree_nuts", "gluten"], "diet": "vegetarian"}
    assert extras["servings"] == 6
    assert clarify == ["peanuts"]    # peanuts unticked: the answer asks about them


def test_the_nuts_question_is_answered_once_peanuts_are_avoided():
    draft = build_draft(outcome(avoid=["tree_nuts", "peanuts"], clarify=["peanuts"]), PROFILE)
    body = {**draft["request"], "servings": None}
    assert confirmed_request(draft, body, PROFILE)[2] == []
