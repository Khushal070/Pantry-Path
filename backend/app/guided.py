"""The state of one guided build, and the moves the shopper can make. Plain data and plain
functions (chat.py stores the build in the chat's "build" message and calls these).

    build = {
      "plan":      plans.DishPlan as JSON (steps and their options)
      "step":      the step shown; len(steps) once the shopper is done
      "picks":     {step id: [option names ticked]}; starts with the usual ones
      "additions": [{"name", "status": ok | clash | anyway, "clash": screen.Clash view}]
      "state":     choosing | done | searched | cancelled
      "draft":     the confirm card's data (drafts.py), for the summary at the end
    }

Additions are anything the steps didn't offer: an ingredient ("pineapple") or a product
by name ("Dave's Killer Bread"). Each is checked by screen.py; a clash waits for the
shopper's choice (use the swap, search it anyway, or remove it).
"""

import re
from typing import Any

from app.plans import DishPlan
from app.screen import Screen

FILLER = re.compile(r"^(?:(?:please|also|and|plus|then|i'd like|i want|let's|lets|maybe|"
                    r"add|some|a|an|with)\s+)+", re.IGNORECASE)
REMOVE = re.compile(r"^(?:no|remove|without|skip|drop|not|hold)\s+(?:the\s+)?", re.IGNORECASE)
SPLIT = re.compile(r"\s*(?:,|;|\band\b|&|\bplus\b|\balso\b)\s*", re.IGNORECASE)
CANCEL = re.compile(r"^\s*(?:start over|cancel|never ?mind|stop)\s*[.!]*\s*$", re.IGNORECASE)
MAX_ADDITIONS = 12


class BuildError(ValueError):
    """A move that doesn't fit the build's state (HTTP 409)."""


def new_build(plan: DishPlan, draft: dict[str, Any]) -> dict[str, Any]:
    """products: the background search of every option's products (choices.py), in
    memory only: status loading | ready | failed, the main store, the stores. choices:
    per item, its pick, alternatives and hint; chosen: per item, the product key picked."""
    return {"plan": plan.model_dump(mode="json"), "step": 0,
            "picks": {s.id: [o.name for o in s.options if o.usual] for s in plan.steps},
            "additions": [], "state": "choosing", "draft": draft,
            "products": {"status": "loading", "main": None, "stores": [], "error": None},
            "choices": {}, "chosen": {}}


def choose(build: dict[str, Any], name: str, key: str) -> None:
    """Pick another product for an item (one of its pick, hint or alternatives)."""
    from app.choices import find_view

    if build["state"] != "choosing":
        raise BuildError("This build is finished: start a new one by naming a dish.")
    choice = build["choices"].get(name)
    if choice is None or find_view(choice, key) is None:
        raise BuildError(f"No such product for {name!r}.")
    build["chosen"][name] = key


def chosen_views(build: dict[str, Any], names: list[str]) -> dict[str, dict[str, Any]]:
    """The product picked for each of these items, where there is one."""
    from app.choices import find_view

    out = {}
    for name in names:
        choice, key = build["choices"].get(name), build["chosen"].get(name)
        view = find_view(choice, key) if choice and key else None
        if view:
            out[name] = view
    return out


def replacements(build: dict[str, Any]) -> dict[str, str]:
    """For the steps check: each option not chosen -> what was chosen in its step."""
    plan = plan_of(build)
    out: dict[str, str] = {}
    for step in plan.steps:
        picked = build["picks"].get(step.id, [])
        if not picked:
            continue
        for option in step.options:
            if option.name not in picked:
                out[option.name.lower()] = " and ".join(picked)
    return out


def offered(build: dict[str, Any]) -> list[str]:
    return [o.name for s in plan_of(build).steps for o in s.options] + \
        [x.name for s in plan_of(build).steps for x in s.left_out]


def plan_of(build: dict[str, Any]) -> DishPlan:
    return DishPlan.model_validate(build["plan"])


def is_cancel(text: str) -> bool:
    return bool(CANCEL.match(text))


def read_items(text: str) -> tuple[list[str], list[str]]:
    """Typed text as (additions, removals): "add pineapple and Dave's Killer Bread, no
    onions" -> (["pineapple", "Dave's Killer Bread"], ["onions"])."""
    adds: list[str] = []
    removes: list[str] = []
    for part in SPLIT.split(text.strip().rstrip(".!")):
        part = part.strip()
        if not part:
            continue
        target = removes if REMOVE.match(part) else adds
        part = FILLER.sub("", REMOVE.sub("", part)).strip(" .!'\"")
        if part:
            target.append(part)
    return adds, removes


def _same(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    return a == b or a.rstrip("s") == b.rstrip("s")


def _covers(removal: str, name: str) -> bool:
    """A removal matches an item it names, or one a word of which it starts: "mayo" ->
    mayonnaise, "tomato" -> cherry tomatoes, "onions" -> red onion."""
    removal = removal.lower().strip().rstrip("s")
    return _same(removal, name) or any(w.startswith(removal) for w in name.lower().split())


def set_picks(build: dict[str, Any], picks: list[str] | None) -> list[str]:
    """Tick these options of the current step (unknown names are ignored); returns the
    newly ticked ones."""
    plan = plan_of(build)
    if picks is None or build["step"] >= len(plan.steps):
        return []
    step = plan.steps[build["step"]]
    allowed = [o.name for o in step.options]
    chosen = [n for n in allowed if n in set(picks)]
    before = set(build["picks"].get(step.id, []))
    build["picks"][step.id] = chosen
    return [n for n in chosen if n not in before]


def move(build: dict[str, Any], action: str, picks: list[str] | None = None) -> list[str]:
    """next / back / finish, saving the current step's picks first. Returns the options
    newly ticked (for the background search)."""
    if build["state"] != "choosing":
        raise BuildError("This build is finished: start a new one by naming a dish.")
    ticked = set_picks(build, picks)
    count = len(build["plan"]["steps"])
    if action == "next":
        build["step"] = min(build["step"] + 1, count)
    elif action == "back":
        build["step"] = max(build["step"] - 1, 0)
    elif action == "finish":
        build["step"] = count
    else:
        raise BuildError(f"unknown move {action!r}")
    if build["step"] >= count:
        build["state"] = "done"
    return ticked


def add_items(build: dict[str, Any], text: str, screen: Screen) -> list[str]:
    """Typed additions and removals. Returns the additions that passed (for the background
    search); clashing ones wait for resolve()."""
    if build["state"] != "choosing":
        raise BuildError("This build is finished: start a new one by naming a dish.")
    adds, removes = read_items(text)
    request = build["draft"]["request"]
    for name in removes:
        for step_id, chosen in build["picks"].items():
            build["picks"][step_id] = [n for n in chosen if not _covers(name, n)]
        build["additions"] = [a for a in build["additions"] if not _covers(name, a["name"])]
    passed = []
    plan = plan_of(build)
    for name in adds:
        if len(build["additions"]) >= MAX_ADDITIONS:
            break
        option = next((o for s in plan.steps for o in s.options if _same(o.name, name)), None)
        if option is not None:          # it was offered: tick it in its step
            step = next(s for s in plan.steps if option in s.options)
            if option.name not in build["picks"][step.id]:
                build["picks"][step.id].append(option.name)
                passed.append(option.name)
            continue
        if any(_same(a["name"], name) for a in build["additions"]):
            continue
        found = screen.check(name, request["avoid"], request["diet"])
        build["additions"].append({"name": name, "status": "clash" if found else "ok",
                                   "clash": found.view() if found else None})
        if found is None:
            passed.append(name)
    return passed


def resolve(build: dict[str, Any], name: str, choice: str) -> list[str]:
    """The shopper's answer to a clash: swap, anyway or remove. Returns what to prefetch."""
    item = next((a for a in build["additions"] if a["name"] == name), None)
    if item is None or item["status"] != "clash":
        raise BuildError(f"Nothing to decide about {name!r}.")
    if choice == "remove":
        build["additions"].remove(item)
        return []
    if choice == "anyway":
        item["status"] = "anyway"
        return [name]
    if choice == "swap" and item["clash"] and item["clash"].get("swap"):
        swap = item["clash"]["swap"]
        item.update(name=swap, status="ok", clash=None, instead_of=name)
        return [swap]
    raise BuildError(f"Can't {choice} {name!r}.")


def reopen(build: dict[str, Any]) -> None:
    """Back to choosing, at the last step: the shopper wants to add something at the
    summary."""
    if build["state"] != "done":
        raise BuildError("This build can't be changed any more.")
    build["state"] = "choosing"
    build["step"] = max(len(build["plan"]["steps"]) - 1, 0)


def pending_clashes(build: dict[str, Any]) -> list[str]:
    return [a["name"] for a in build["additions"] if a["status"] == "clash"]


def chosen(build: dict[str, Any], usual_for_the_rest: bool = False) -> list[dict[str, Any]]:
    """The ingredients to search: every ticked option (with its category; usual ones count
    as core), then the additions that passed or were kept anyway. usual_for_the_rest: the
    steps not reached yet count with their usual options."""
    plan = plan_of(build)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, step in enumerate(plan.steps):
        names = build["picks"].get(step.id, [])
        if usual_for_the_rest and i > build["step"]:
            names = [o.name for o in step.options if o.usual]
        for option in step.options:
            if option.name in names and option.name not in seen:
                seen.add(option.name)
                out.append({"name": option.name, "category": option.category,
                            "role": "core" if option.usual else "common"})
    for item in build["additions"]:
        if item["status"] in ("ok", "anyway") and item["name"].lower() not in seen:
            seen.add(item["name"].lower())
            out.append({"name": item["name"], "category": "other", "role": "common"})
    return out
