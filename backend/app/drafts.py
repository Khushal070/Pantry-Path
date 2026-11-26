"""The chat's reading plus the user's saved preferences (their "profile"): the request to
confirm before searching.

The rules, all deterministic (no model decides any of this):
- avoid: everything the chat asks to avoid, plus everything the profile avoids. A profile
  allergen can't be dropped from the chat ("milk is fine today"): the shopper edits the
  profile for that, and the card says so. Erring this way only adds caution.
- diet: the stricter of the chat's and the profile's.
- ZIP: the chat's when it gives one, else the profile's, else we ask.
- servings: the chat's, else the profile's household size.
- needs we can't check (halal, low sodium, ...): the chat's and the profile's; the answer
  warns about each.
- issues: the parser's doubts the shopper should look at. A doubt about an allergen or
  diet the profile already covers no longer matters and is dropped.
"""

from typing import Any

from app.engines.base import Issue, ParseOutcome
from app.vocab import (
    DIET_RANK,
    DIETS,
    NEEDS,
    allergen_label,
    ordered_allergens,
    stricter_diet,
)


def _moot(issue: Issue, profile: dict[str, Any]) -> bool:
    avoid = set(profile.get("avoid") or [])
    if issue.kind in ("missed_allergen", "clarify", "unmentioned"):
        return issue.allergen in avoid
    if issue.kind == "diet":
        return DIET_RANK[profile.get("diet") or "none"] >= DIET_RANK[issue.value or "none"]
    if issue.kind == "zip":
        return bool(issue.value) and issue.value == profile.get("zip")
    return False


def issue_text(issue: Issue) -> str:
    label = allergen_label(issue.allergen) if issue.allergen else ""
    if issue.kind == "missed_allergen":
        return f"You mentioned {label}, so I'm avoiding it."
    if issue.kind == "diet":
        return f"You said {issue.value}, so I'm using a {issue.value} diet."
    if issue.kind == "clarify":
        return ("You mentioned nuts. To be safe I'm avoiding tree nuts and peanuts: untick "
                "peanuts if they're fine.")
    if issue.kind == "unmentioned":
        return f"I added {label}, but I'm not sure you asked for it. Untick it if it's fine."
    if issue.kind == "zip":
        return f"Using ZIP {issue.value} from your message."
    return f"Please check: {issue.value}"


def build_draft(outcome: ParseOutcome, profile: dict[str, Any]) -> dict[str, Any]:
    """state: ready | check (issues to look at) | need_zip | need_dish | invalid."""
    if outcome.status == "invalid":
        return {"state": "invalid", "error": outcome.error}
    if not outcome.query:
        return {"state": "need_dish"}
    p_avoid = ordered_allergens(profile.get("avoid") or [])
    p_diet = profile.get("diet") or "none"
    avoid = ordered_allergens([*outcome.avoid, *p_avoid])
    diet = stricter_diet(outcome.diet, p_diet)
    zip_ = outcome.zip or profile.get("zip")

    def source(in_chat: bool, in_profile: bool) -> str | None:
        return "both" if in_chat and in_profile else "chat" if in_chat else \
            "profile" if in_profile else None

    sources = {
        "avoid": {a: source(a in outcome.avoid, a in p_avoid) for a in avoid},
        "diet": source(outcome.diet == diet and diet != "none", p_diet == diet and diet != "none"),
        "zip": "chat" if outcome.zip else "profile" if zip_ else None,
    }
    issues = [{"kind": i.kind, "text": issue_text(i)}
              for i in outcome.issues if not _moot(i, profile)]
    notes = []
    for a in p_avoid:   # the chat said it's fine; the profile still avoids it
        if a in outcome.mentioned and a not in outcome.chat_avoid and a not in outcome.avoid:
            notes.append(f"Your preferences avoid {allergen_label(a)}, so I kept it. Change "
                         "that in Preferences.")
    if DIET_RANK[p_diet] > DIET_RANK[outcome.diet] and outcome.diet != "none":
        notes.append(f"Your preferences say {p_diet}, so I kept a {p_diet} diet.")
    servings = outcome.extras.get("servings") or profile.get("servings")
    chat_needs = list(outcome.extras.get("unsupported") or [])
    unsupported = chat_needs + [n for n in profile.get("needs") or [] if n not in chat_needs]
    state = "need_zip" if not zip_ else "check" if issues else "ready"
    return {
        "state": state,
        "request": {"query": outcome.query, "zip": zip_, "avoid": avoid, "diet": diet},
        "servings": servings,
        "sources": sources,
        "locked": p_avoid,          # profile allergens: shown, can't be unticked here
        "min_diet": p_diet,         # the diet can't go below the profile's
        "issues": issues,
        "notes": notes,
        "needs": [n for n in NEEDS if n in unsupported],
        "in_catalog": outcome.in_catalog,
        # for the answer (A3): never shown as is
        "extras": {**outcome.extras, "servings": servings, "unsupported": unsupported},
        "clarify": list(outcome.clarify),
    }


def confirmed_request(draft: dict[str, Any], body: dict[str, Any],
                      profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any],
                                                        list[str]]:
    """The request to search, from a confirm card the shopper may have edited. Profile
    allergens and the profile's diet are applied again, whatever the card sent.
    Returns (request, extras for A3, clarify for A3)."""
    diet = stricter_diet(body["diet"], profile.get("diet"))
    if diet not in DIETS:
        diet = "none"
    request = {
        "query": body["query"],
        "zip": body["zip"],
        "avoid": ordered_allergens([*body["avoid"], *(profile.get("avoid") or [])]),
        "diet": diet,
    }
    servings = body.get("servings") or draft.get("servings")
    extras = {**(draft.get("extras") or {}), "servings": servings}
    # a "nuts" question is answered once peanuts are avoided
    clarify = [a for a in draft.get("clarify") or [] if a not in request["avoid"]]
    return request, extras, clarify
