"""outcome_from: A1's ParseResult read into the chat's terms. Uses stand-ins with
ParseResult's fields, so it runs without the model packages."""

from types import SimpleNamespace

from app.engines.live import outcome_from


def parsed(status, **kw):
    base = {"status": status, "request": None, "fields": None, "extras": {}, "conflicts": [],
            "unmentioned": [], "suggested": None, "missing": [], "clarify": [], "notes": [],
            "error": None,
            "scan": {"avoid": [], "diet": "none", "mentioned": [], "clarify": [],
                     "unsupported": []}}
    return SimpleNamespace(**(base | kw))


FIELDS = {"query": "quiche lorraine", "zip": None, "avoid": ["milk"], "diet": "none"}


def test_ok_uses_the_resolved_request():
    p = parsed("ok", fields=FIELDS, request={**FIELDS, "zip": "45202"},
               extras={"servings": 4, "tweaks": [], "unsupported": []},
               scan={"avoid": ["milk"], "diet": "none", "mentioned": ["milk"]})
    o = outcome_from(p, in_catalog=True)
    assert (o.status, o.query, o.zip, o.avoid, o.diet) == ("ok", "quiche lorraine", "45202",
                                                          ["milk"], "none")
    assert o.issues == [] and o.in_catalog is True and o.extras["servings"] == 4


def test_incomplete_has_no_zip():
    o = outcome_from(parsed("incomplete", fields=FIELDS, missing=["zip"]), in_catalog=None)
    assert o.status == "ok" and o.zip is None and o.query == "quiche lorraine"


def test_needs_confirmation_takes_the_stricter_suggestion_and_explains_it():
    suggested = {"query": "quiche lorraine", "zip": "45202",
                 "avoid": ["milk", "egg", "tree_nuts", "peanuts"], "diet": "vegan"}
    p = parsed("needs_confirmation", fields={**FIELDS, "zip": "45203"}, suggested=suggested,
               clarify=["peanuts"], unmentioned=[],
               scan={"avoid": ["milk", "egg", "tree_nuts"], "diet": "vegan",
                     "mentioned": ["milk", "egg", "tree_nuts"]},
               conflicts=["the chat mentions egg, which the model did not put in avoid",
                          "the chat mentions tree_nuts, which the model did not put in avoid",
                          "the chat says vegan, the model chose none",
                          "the chat mentions nuts, which can mean tree nuts or peanuts: the "
                          "suggested fix avoids both",
                          "the chat gives ZIP 45202, the model chose 45203"])
    o = outcome_from(p, in_catalog=True)
    assert o.avoid == suggested["avoid"] and o.diet == "vegan" and o.zip == "45202"
    assert [(i.kind, i.allergen or i.value) for i in o.issues] == [
        ("missed_allergen", "egg"), ("missed_allergen", "tree_nuts"), ("diet", "vegan"),
        ("clarify", "peanuts"), ("zip", "45202")]
    assert o.clarify == ["peanuts"]


def test_a_conflict_it_cant_classify_is_kept():
    p = parsed("needs_confirmation", fields=FIELDS, suggested={**FIELDS, "zip": "45202"},
               conflicts=["a brand new kind of conflict"])
    o = outcome_from(p, in_catalog=None)
    assert [(i.kind, i.value) for i in o.issues] == [("other", "a brand new kind of conflict")]


def test_invalid():
    o = outcome_from(parsed("invalid", error="no usable reply"), in_catalog=None)
    assert (o.status, o.error) == ("invalid", "no usable reply")
