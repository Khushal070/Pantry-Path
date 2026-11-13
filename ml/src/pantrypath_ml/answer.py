"""A3: result.json (+ A1 extras) -> the answer the shopper reads, held to result.json.

A hybrid. Code decides the shopping list (`decide`): each ingredient's status (buy /
substitute / check the label / skip), its product and store from result.json, and its
substitute from the vetted table (configs/substitutes.yaml), each substitute checked
against the shopper's allergens and diet. The model only writes the summary, the steps
and the quantities around those fixed decisions, mentioning only the given items. Nobody
sets or changes a verdict: verdicts come only from diet_rules.assess inside the data
pipeline, and the app shows them from result.json.

The decisions and the model's text are put together into one reply, and a deterministic
check (`check`) holds it to result.json:
- every ingredient of result.json appears once, and no other;
- "buy" only names a product result.json marked safe, for that ingredient, at its store;
- no product that is not safe is named anywhere in the text;
- the summary, steps and notes name no avoided allergen and nothing the diet excludes,
  other than a word of the dish's name, an ingredient the answer buys, a substitute, or
  inside a substitution or avoid phrase ("instead of butter", "replace the heavy cream",
  "without bacon"); the name of an ingredient that is substituted, label-checked or
  skipped is screened like any other word outside such a phrase ("Stir in the heavy
  cream." fails for a milk-avoider when heavy cream is a substitute);
- every substitute passes the substitute screen (allergen words for the avoid list and
  diet words, configs/answer.yaml): diet_rules judges products, not ingredient names.
One retry shows the model what broke; anything still broken is repaired (`repair`), so
the answer that leaves this module always passes the check. Every attempt (reply, error,
violations) is kept in `history`, with the last usable reply and its violations as `raw`
and `violations`, for evaluation.

Warnings that must always be there (unknown products, estimated quantities, unchecked
needs from A1, Open Food Facts attribution) are added by code, not by the model.

No reply cache in production: the prompt holds Kroger product names, and Kroger's terms
forbid keeping them (runtime.py builds the A3 client without a cache).
"""

import json
import re
import string
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from pantrypath_ml.parse_request import (
    ALLERGEN_ORDER,
    RETRY_NOTE,
    CrossCheck,
    _alternation,
    _closed,
    _forms,
)
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import LLMError, PipelineError
from pantrypath_pipeline.llm import LLMClient, LLMReplyError

STATUSES = ("buy", "substitute", "check_label", "skip")


class AnswerItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: Literal["buy", "substitute", "check_label", "skip"]
    product: str | None = None
    store_id: str | None = None
    substitute: str | None = None
    quantity: float | None = None
    unit: str | None = None
    note: str | None = None


class AnswerReply(BaseModel):
    """The whole answer: code's decisions (ingredients) and the model's text."""
    model_config = ConfigDict(extra="forbid")
    summary: str
    servings: int | None = None
    ingredients: list[AnswerItem]
    steps: list[str]


class Quantity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    quantity: float | None = None
    unit: str | None = None


class TextReply(BaseModel):
    """What the model writes around the decided list."""
    model_config = ConfigDict(extra="forbid")
    summary: str
    servings: int | None = None
    quantities: list[Quantity]
    steps: list[str]


def answer_schema(model: type[BaseModel] = TextReply) -> dict[str, Any]:
    base = model.model_json_schema()
    return _closed(base, base.get("$defs", {}))


ANSWER_SCHEMA = answer_schema()               # hybrid: the model's text
LIST_SCHEMA = answer_schema(AnswerReply)       # model_list: the whole answer


# ---------------------------------------------------------------- result.json view


def _norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


@dataclass
class Facts:
    """What result.json allows the answer to say, per ingredient."""

    dish: str
    avoid: list[str]
    diet: str
    roles: dict[str, str]                         # ingredient -> core | common
    safe: dict[str, list[dict[str, Any]]]         # ingredient -> its safe products
    unknown: dict[str, int]
    unsafe: dict[str, int]
    not_safe_names: set[str]                      # normalized names of every non-safe product
    stores: dict[str, str]                        # store_id -> name
    any_unknown: bool
    any_off: bool

    @classmethod
    def of(cls, result: dict[str, Any]) -> "Facts":
        roles, safe, unknown, unsafe, not_safe = {}, {}, {}, {}, set()
        any_off = False
        for ing in result["ingredients"]:
            name = ing["name"]
            roles[name] = ing.get("role") or "common"
            safe[name], unknown[name], unsafe[name] = [], 0, 0
            for p in ing.get("products") or []:
                any_off |= p.get("evidence_source") == "off"
                if p["verdict"] == "safe":
                    safe[name].append(p)
                else:
                    (unknown if p["verdict"] == "unknown" else unsafe)[name] += 1
                    not_safe.add(_norm(p["name"]))
        safe_names = {_norm(p["name"]) for ps in safe.values() for p in ps}
        request = result["request"]
        return cls(dish=result["dish"]["name"], avoid=list(request.get("avoid") or []),
                   diet=request.get("diet") or "none", roles=roles, safe=safe,
                   unknown=unknown, unsafe=unsafe,
                   # a name that is safe at one store and not at another is not flagged
                   not_safe_names=not_safe - safe_names,
                   stores={s["store_id"]: s.get("name") or s["store_id"]
                           for s in result.get("stores") or []},
                   any_unknown=any(unknown.values()), any_off=any_off)

    def allowed(self, name: str) -> tuple[str, ...]:
        """The statuses result.json allows for this ingredient."""
        if self.safe[name]:
            return ("buy", "skip") if self.roles[name] == "common" else ("buy",)
        options = ["substitute"]
        if self.unknown[name]:
            options.append("check_label")
        if self.roles[name] == "common":
            options.append("skip")
        return tuple(options)


# ---------------------------------------------------------------- substitute screen


class SubstituteCheck:
    """Allergen and diet words in a suggested substitute (configs/answer.yaml)."""

    def __init__(self, acfg: Config, prcfg: Config):
        settings = acfg["substitute_check"]
        words: dict[str, set[str]] = {}
        for form, (_, allergens) in CrossCheck(prcfg["cross_check"]).term_info.items():
            for a in allergens:
                words.setdefault(a, set()).add(form)
        for allergen, extra in settings["extra_words"].items():
            words.setdefault(allergen, set()).update(f for w in extra for f in _forms(w))
        self.allergens = {a: re.compile(rf"\b(?:{_alternation(list(ws))})\b")
                          for a, ws in words.items()}
        vegetarian = settings["diet_conflicts"]["vegetarian"]
        vegan = vegetarian + settings["diet_conflicts"]["vegan"]
        self.diets = {d: re.compile(rf"\b(?:{_alternation([f for w in ws for f in _forms(w)])})\b")
                      for d, ws in (("vegetarian", vegetarian), ("vegan", vegan))}
        self.allow = re.compile(rf"\b(?:{_alternation(settings['diet_allow'])})\b")
        modified = [f for w in settings["modified"] for f in _forms(w)]
        between = rf"(?:[\s-]+(?:{_alternation(settings.get('descriptors') or ['-'])}))*"
        # a modifier, or any "<word>-free" ("egg-free mayo"), before a modified food, with
        # descriptors between ("gluten-free all-purpose flour")
        self.plant = re.compile(rf"\b({_alternation(settings['modifiers'])}|[a-z]+[\s-]free)"
                                rf"{between}[\s-]+(?:{_alternation(modified)})\b")
        meat = [f for w in settings["diet_conflicts"]["vegetarian"] for f in _forms(w)]
        # "vegetarian sausage", "plant-based ground meat": a meat-free product
        self.analog = re.compile(rf"\b({_alternation(settings.get('analog_modifiers') or [])})"
                                 rf"{between}[\s-]+(?:{_alternation(meat)})\b") \
            if settings.get("analog_modifiers") else None

    def problems(self, text: str, avoid: list[str], diet: str) -> list[str]:
        """What is wrong with this substitute for this shopper ([] = passes)."""
        text = self.plant.sub(r"\1", _norm(text))   # "coconut milk" -> "coconut"
        if self.analog:
            text = self.analog.sub(r"\1", text)    # "vegetarian sausage" -> "vegetarian"
        text = re.sub(r"\b[a-z]+[\s-]free\b", " ", text)  # "gluten-free" names an absence
        out = [a for a in avoid if a in self.allergens and self.allergens[a].search(text)]
        if diet in self.diets and self.diets[diet].search(self.allow.sub(" ", text)):
            out.append(diet)
        return out


# ---------------------------------------------------------------- check and repair
# Both work on the reply as a plain dict (AnswerReply.model_dump()).


@dataclass
class Violation:
    # invented | missing | duplicate | buy_without_safe | status | product | store |
    # text_product | substitute | text_conflict
    kind: str
    ingredient: str | None
    detail: str


def _mentions(text: str | None, names: set[str]) -> list[str]:
    norm = _norm(text)
    return sorted(n for n in names if n and n in norm)


# "no butter", "instead of cheese", "replace the cream", "substitute for ham", "avoids
# milk", "without milk, eggs or nuts": a food named in one of these is absent or being
# replaced. "label for" / "check for" name
# what a label is read for ("Read the label for wheat.").
# one to three words ("monterey jack cheese"), never a joining or cue word ("cheese or",
# "lentils instead"), so a list stops where the next phrase starts
_STOP = r"(?!(?:and|or|instead|with|in|of|for|by|to|then)\b)"
_WORD = r"[a-z]+(?:-[a-z]+)*"                   # "dairy-free", "half-and-half"
_FOOD = rf"{_STOP}{_WORD}(?:\s+{_STOP}{_WORD}){{0,2}}"
_ABSENT = re.compile(r"\b(?:no|without|instead of|in place of|rather than|skip|omit|leave out|"
                     r"free of|free from|avoid|avoids|avoided|avoiding|replace|replacing|"
                     r"swap out|swap|swapping|substitute for|substitutes for|substituting for|"
                     r"alternative to|alternatives to|alternative for|alternatives for|"
                     r"replacement for|replacements for|"
                     r"label for|labels for|check for|checking for)"
                     rf"\s+(?:the\s+|any\s+|your\s+)?{_FOOD}"
                     rf"(?:\s*(?:,|,?\s+and|,?\s+or)\s+(?:the\s+|any\s+)?{_FOOD})*")
# "couscous replaced by quinoa", "the cream swapped for coconut milk": the food before
_REPLACED = re.compile(rf"\b{_FOOD}\s+(?:is\s+|are\s+)?(?:replaced|swapped|substituted)"
                       r"(?:\s+(?:by|with|for)\b)?")


def text_problems(text: str | None, items: list[dict[str, Any]], facts: Facts,
                  screen: SubstituteCheck) -> list[str]:
    """Avoided allergens and diet conflicts a summary, step or note names. Free to name:
    the dish the shopper asked for and every word of its name ("bread pan" in zucchini
    bread, "egg-shaped" in deviled eggs: a dish word is not an ingredient mention), the
    ingredients the answer buys (a product marked safe) and the substitutes. Anything
    else, a substituted, label-checked or skipped ingredient's own name too, passes only
    inside a substitution or avoid phrase. Products are held to result.json by check()."""
    norm = _ABSENT.sub(" ", _REPLACED.sub(" ", _norm(text)))
    allowed = [facts.dish, *(i["name"] for i in items if i["status"] == "buy")]
    allowed += [i["substitute"] for i in items if i.get("substitute")]
    for phrase in sorted({_norm(a) for a in allowed if a}, key=len, reverse=True):
        norm = re.sub(rf"\b{re.escape(phrase)}\b", " ", norm)
    words = dish_words(facts.dish)
    if words:
        norm = re.sub(rf"\b(?:{_alternation(words)})\b", " ", norm)
    return screen.problems(norm, facts.avoid, facts.diet)


def dish_words(dish: str) -> list[str]:
    """The words of a dish's name, with their singular and plural forms ("eggs" -> egg,
    eggs); words shorter than 3 letters are left out."""
    out: set[str] = set()
    for word in re.split(r"[\s-]+", _norm(dish)):
        if len(word) < 3:
            continue
        stems = {word, re.sub(r"(?:es|s)$", "", word) if len(word) > 3 else word}
        out.update(f for stem in stems for f in _forms(stem))
    return sorted(out)


def check(reply: dict[str, Any], facts: Facts, screen: SubstituteCheck) -> list[Violation]:
    """Every way the reply departs from result.json and the rules."""
    out: list[Violation] = []
    seen: set[str] = set()
    names = {_norm(n): n for n in facts.roles}
    for item in reply["ingredients"]:
        name = names.get(_norm(item["name"]))
        if name is None:
            out.append(Violation("invented", item["name"], "not an ingredient in result.json"))
            continue
        if name in seen:
            out.append(Violation("duplicate", name, "listed more than once"))
            continue
        seen.add(name)
        if item["status"] == "buy" and not facts.safe[name]:
            out.append(Violation("buy_without_safe", name, "no product is marked safe"))
        elif item["status"] not in facts.allowed(name):
            out.append(Violation("status", name, f"{item['status']} not allowed; "
                                 f"result.json allows {', '.join(facts.allowed(name))}"))
        safe = {_norm(p["name"]) for p in facts.safe[name]}
        product = item.get("product")
        if product and (item["status"] != "buy" or _norm(product) not in safe):
            out.append(Violation("product", name, f"{product!r} is not a safe product to "
                                 "buy for this ingredient"))
        elif item["status"] == "buy" and product:
            stores = {p["store_id"] for p in facts.safe[name] if _norm(p["name"]) == _norm(product)}
            if item.get("store_id") not in stores:
                out.append(Violation("store", name, f"{product!r} is not sold at "
                                     f"{item.get('store_id')!r} in result.json"))
        if item.get("substitute"):
            bad = screen.problems(item["substitute"], facts.avoid, facts.diet)
            if bad:
                out.append(Violation("substitute", name, f"{item['substitute']!r} conflicts "
                                     f"with {', '.join(bad)}"))
    out += [Violation("missing", n, "not in the answer") for n in facts.roles if n not in seen]
    texts = [reply["summary"], *reply["steps"], *(i.get("note") for i in reply["ingredients"])]
    for found in sorted({n for t in texts for n in _mentions(t, facts.not_safe_names)}):
        out.append(Violation("text_product", None, f"names {found!r}, which is not safe"))
    for text in [reply["summary"], *reply["steps"], *(i.get("note") for i in reply["ingredients"])]:
        if not text:
            continue
        bad = text_problems(text, reply["ingredients"], facts, screen)
        if bad:
            out.append(Violation("text_conflict", None, f"{text!r} conflicts with "
                                 f"{', '.join(bad)}"))
    return out


def _drop_sentences(text: str | None, names: set[str],
                    bad: Any = lambda sentence: False) -> str:
    """The text without the sentences that name one of `names` or that `bad` rejects."""
    parts = re.split(r"(?<=[.!?])\s+", text or "")
    return " ".join(p for p in parts if not _mentions(p, names) and not bad(p)).strip()


def repair(reply: dict[str, Any], facts: Facts, screen: SubstituteCheck) -> dict[str, Any]:
    """The reply, changed only where it breaks a rule, so that check() finds nothing."""
    names = {_norm(n): n for n in facts.roles}
    kept: dict[str, dict[str, Any]] = {}
    for item in reply["ingredients"]:
        name = names.get(_norm(item["name"]))
        if name is not None and name not in kept:
            kept[name] = {**item, "name": name}
    items = []
    for name in facts.roles:   # result.json order
        item = dict(kept.get(name) or {
            "name": name, "status": facts.allowed(name)[0], "product": None, "store_id": None,
            "substitute": None, "quantity": None, "unit": None, "note": None})
        if item["status"] not in facts.allowed(name):
            item["status"] = facts.allowed(name)[0]
        if item["status"] == "buy":
            safe = facts.safe[name]
            named = [p for p in safe if _norm(p["name"]) == _norm(item.get("product"))]
            match = next((p for p in named if p["store_id"] == item.get("store_id")),
                         named[0] if named else safe[0])
            item.update(product=match["name"], store_id=match["store_id"])
        else:
            item.update(product=None, store_id=None)
        if item.get("substitute") and (item["status"] != "substitute" or screen.problems(
                item["substitute"], facts.avoid, facts.diet)):
            item["substitute"] = None
        if item["status"] == "substitute" and not item.get("substitute"):
            item["note"] = open_substitute_note(facts)
        items.append(item)

    def conflicts(sentence: str) -> bool:
        return bool(text_problems(sentence, items, facts, screen))

    for item in items:   # after every status is final: a note is screened against them all
        if item.get("note"):
            item["note"] = _drop_sentences(item["note"], facts.not_safe_names,
                                           conflicts) or None

    steps = [s for s in (_drop_sentences(s, facts.not_safe_names, conflicts)
                         for s in reply["steps"]) if s]
    summary = _drop_sentences(reply["summary"], facts.not_safe_names, conflicts)
    return {**reply, "ingredients": items, "steps": steps,
            "summary": summary or f"Here is your shopping list for {facts.dish}."}


# ---------------------------------------------------------------- decisions


def open_substitute_note(facts: Facts) -> str:
    """The note for a substitute nobody could name: what it must avoid."""
    restrictions = ", ".join(a.replace("_", " ") for a in facts.avoid) or "the avoided allergens"
    if facts.diet != "none":
        restrictions += f" that fits a {facts.diet} diet"
    return f"No safe product found: choose a substitute without {restrictions}."


def label_note(facts: Facts) -> str:
    parts = []
    if facts.avoid:
        parts.append("read the label for " + ", ".join(a.replace("_", " ") for a in facts.avoid))
    if facts.diet != "none":
        parts.append(f"check that it is {facts.diet}")
    return "Some products couldn't be verified: " + (" and ".join(parts) or "read the label") + "."


# what a substitute may contain (configs/substitutes.yaml): allergens, and two diet tags
DIET_TAGS = ("meat", "animal")
DIET_EXCLUDES = {"vegetarian": {"meat", "fish", "shellfish"},
                 "vegan": {"meat", "fish", "shellfish", "animal", "milk", "egg"}}


def contains_conflicts(contains: list[str], avoid: list[str], diet: str) -> list[str]:
    """The shopper's allergens and diet a substitute's contains list conflicts with."""
    out = [a for a in avoid if a in contains]
    if set(contains) & DIET_EXCLUDES.get(diet, set()):
        out.append(diet)
    return out


class SubstituteTable:
    """configs/substitutes.yaml: candidate substitutes, each with what it may contain, by
    the key that ends an ingredient's name ("cheddar cheese" -> cheese), longest key
    first; plurals count."""

    def __init__(self, scfg: Config):
        allowed = {*ALLERGEN_ORDER, *DIET_TAGS}
        self.table: dict[str, list[dict[str, Any]]] = {}
        for key, entries in scfg["substitutes"].items():
            for entry in entries:
                if not isinstance(entry, dict) or "name" not in entry \
                        or not isinstance(entry.get("contains"), list):
                    raise PipelineError(f"substitutes.yaml {key!r}: every candidate needs a "
                                        f"name and a contains list ([] for nothing): {entry!r}")
                unknown = set(entry["contains"]) - allowed
                if unknown:
                    raise PipelineError(f"substitutes.yaml {key!r} {entry['name']!r}: unknown "
                                        f"contains {sorted(unknown)}; use {sorted(allowed)}")
            self.table[_norm(key)] = [dict(e) for e in entries]
        self.keys = sorted(((f, k) for k in self.table for f in _forms(k)),
                           key=lambda fk: len(fk[0]), reverse=True)

    def candidates(self, name: str) -> list[dict[str, Any]]:
        norm = _norm(name)
        for form, key in self.keys:
            if norm == form or norm.endswith(" " + form):
                return [c for c in self.table[key] if _norm(c["name"]) != norm]
        return []

    def pick(self, name: str, facts: Facts, screen: SubstituteCheck) -> str | None:
        """The first candidate whose contains list and name both pass for this shopper."""
        return next((c["name"] for c in self.candidates(name)
                     if not contains_conflicts(c["contains"], facts.avoid, facts.diet)
                     and not screen.problems(c["name"], facts.avoid, facts.diet)), None)


def _item(name: str, status: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "product": None, "store_id": None,
            "substitute": None, "quantity": None, "unit": None, "note": None, **fields}


def decide(result: dict[str, Any], facts: Facts, screen: SubstituteCheck,
           table: SubstituteTable) -> list[dict[str, Any]]:
    """Each ingredient's status, product, store and substitute, in result.json order:
    - a safe product: buy it, at the nearest store that has one;
    - else, if the ingredient itself conflicts with the avoid list or diet (butter for a
      milk-avoider), a label can't fix it: a vetted substitute, else skip (common) or an
      open substitute (core);
    - else check the label when some products couldn't be verified, then a vetted
      substitute, then skip (common) or an open substitute (core).
    Every decision passes check() on its own."""
    distance = {s["store_id"]: s.get("distance_miles") or 0.0
                for s in result.get("stores") or []}
    items = []
    for name in facts.roles:
        safe = facts.safe[name]
        if safe:
            best = min(safe, key=lambda p: distance.get(p["store_id"], float("inf")))
            items.append(_item(name, "buy", product=best["name"], store_id=best["store_id"]))
            continue
        inherent = bool(screen.problems(name, facts.avoid, facts.diet))
        substitute = table.pick(name, facts, screen)
        if facts.unknown[name] and not inherent:
            items.append(_item(name, "check_label", note=label_note(facts)))
        elif substitute:
            items.append(_item(name, "substitute", substitute=substitute,
                               note=f"No product was marked safe: use {substitute}."))
        elif facts.roles[name] == "common":
            items.append(_item(name, "skip", note="No safe product: the dish works without it."))
        else:
            items.append(_item(name, "substitute", note=open_substitute_note(facts)))
    return items


def assemble(items: list[dict[str, Any]], text: dict[str, Any]) -> tuple[dict[str, Any],
                                                                         list[str]]:
    """The answer: the decided items with the model's quantities, its summary and steps.
    Also the quantity names that match no item (ignored)."""
    by_name = {_norm(q["name"]): q for q in text["quantities"]}
    out, used = [], set()
    for item in items:
        q = by_name.get(_norm(item["name"]))
        if q and item["status"] != "skip":
            used.add(_norm(item["name"]))
            item = {**item, "quantity": q.get("quantity"), "unit": q.get("unit")}
        out.append(dict(item))
    ignored = [q["name"] for k, q in by_name.items() if k not in used]
    return ({"summary": text["summary"], "servings": text.get("servings"),
             "ingredients": out, "steps": text["steps"]}, ignored)


# ---------------------------------------------------------------- prompt and run


def variant_name(acfg: Config, variant: str | None = None) -> str:
    variant = variant or acfg["default_variant"]
    if variant not in acfg["variants"]:
        raise PipelineError(f"Unknown A3 prompt variant {variant!r}; use one of "
                            f"{sorted(acfg['variants'])}")
    return variant


def variant_of_version(acfg: Config, version: str | None) -> str | None:
    """The variant whose prompt file has this version (for runs that didn't record it)."""
    for name in acfg["variants"]:
        if load_prompt(acfg, name)[1] == version:
            return name
    return None


def load_prompt(acfg: Config, variant: str | None = None) -> tuple[string.Template, str]:
    """A variant's prompt template and its version (first line: 'prompt_version: ...')."""
    path = acfg.path(acfg["variants"][variant_name(acfg, variant)]["prompt_file"])
    if not path.is_file():
        raise PipelineError(f"Prompt file not found: {path}")
    first, _, body = path.read_text().partition("\n")
    if not first.startswith("prompt_version:"):
        raise PipelineError(f"{path} must start with 'prompt_version: ...'")
    return string.Template(body), first.split(":", 1)[1].strip()


def render_prompt(acfg: Config, result: dict[str, Any], extras: dict[str, Any],
                  items: list[dict[str, Any]],
                  template: string.Template | None = None) -> str:
    """The prompt: the shopper, the dish and the decided list (only what is used: a
    bought product's name, a substitute; never a product that is not safe)."""
    facts = Facts.of(result)
    lines = []
    for item in items:
        line: dict[str, Any] = {"name": item["name"], "use": item["status"]}
        if item["status"] == "buy":
            line["product"] = item["product"]
        if item["status"] == "substitute":
            line["substitute"] = item["substitute"]
        lines.append(json.dumps(line))
    return (template or load_prompt(acfg, "hybrid")[0]).substitute(
        avoid=", ".join(a.replace("_", " ") for a in facts.avoid) or "nothing",
        diet=facts.diet, dish=facts.dish, servings=servings(acfg, extras),
        tweaks=", ".join(extras.get("tweaks") or []) or "none", items="\n".join(lines),
        swaps=swap_lines(items))


def swap_lines(items: list[dict[str, Any]]) -> str:
    """hybrid_swaps (answer/3): one line per substituted or left-out ingredient."""
    lines = []
    for item in items:
        if item["status"] == "substitute" and item.get("substitute"):
            lines.append(f"- use {item['substitute']} instead of {item['name']}")
        elif item["status"] == "substitute":
            lines.append(f"- {item['name']}: write \"your substitute for {item['name']}\"")
        elif item["status"] == "skip":
            lines.append(f"- leave out {item['name']}")
    return "\n".join(lines) or "- none"


def render_list_prompt(acfg: Config, result: dict[str, Any], extras: dict[str, Any],
                       template: string.Template | None = None) -> str:
    """model_list (answer/1): the ingredients with their safe products' names and store,
    and counts of unsafe and unverified ones; the model writes the whole list."""
    facts = Facts.of(result)
    shown = int(acfg["safe_products_shown"])
    lines = [json.dumps({"name": name, "role": facts.roles[name],
                         "safe_products": [{"name": p["name"], "store_id": p["store_id"]}
                                           for p in facts.safe[name][:shown]],
                         "unsafe_products": facts.unsafe[name],
                         "unverified_products": facts.unknown[name]})
             for name in facts.roles]
    stores = "\n".join(f"{sid}: {name}" for sid, name in facts.stores.items()) or "(none)"
    return (template or load_prompt(acfg, "model_list")[0]).substitute(
        avoid=", ".join(facts.avoid) or "nothing", diet=facts.diet, dish=facts.dish,
        servings=servings(acfg, extras), tweaks=", ".join(extras.get("tweaks") or []) or "none",
        ingredients="\n".join(lines), stores=stores)


def servings(acfg: Config, extras: dict[str, Any]) -> int:
    return int(extras.get("servings") or acfg["default_servings"])


def warnings(acfg: Config, facts: Facts, extras: dict[str, Any], reply: dict[str, Any],
             clarify: list[str] | None = None) -> list[str]:
    text = acfg["warnings"]
    out = []
    if facts.any_unknown:
        out.append(text["unknown"])
    if any(i.get("quantity") is not None for i in reply["ingredients"]):
        out.append(text["quantities"].format(servings=reply.get("servings")
                                             or servings(acfg, extras)))
    if any(i.get("substitute") for i in reply["ingredients"]):
        out.append(text["substitutes"])
    out += [text["unsupported"].format(label=label.replace("_", " "))
            for label in extras.get("unsupported") or []]
    if clarify:
        out.append(text["clarify"])
    if facts.any_off:
        out.append(text["off_attribution"])
    return out


@dataclass
class AnswerResult:
    status: str                                   # ok | repaired | invalid
    answer: dict[str, Any] | None = None          # always passes check() when present
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None             # the last usable reply, put together
    violations: list[dict[str, Any]] = field(default_factory=list)  # of `raw`
    # every attempt in order: {attempt, text (the model's), reply (put together; None if
    # unusable), ignored (quantity names that match no item), error, violations}; the
    # decided list is `decisions`; the repair, if any, is `answer`
    history: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def answer(acfg: Config, result: dict[str, Any], client: LLMClient, *,
           extras: dict[str, Any] | None = None, clarify: list[str] | None = None,
           screen: SubstituteCheck, template: string.Template | None = None,
           table: SubstituteTable | None = None, variant: str | None = None) -> AnswerResult:
    """A3 on one result.json. hybrid: code's decisions and the model's text around them;
    model_list (answer/1): the model writes the whole answer. Then (one retry on an
    unusable or rule-breaking reply) the check and, if needed, the repair."""
    extras = extras or {}
    facts = Facts.of(result)
    variant = variant_name(acfg, variant)
    hybrid = acfg["variants"][variant]["mode"] == "hybrid"
    template = template or load_prompt(acfg, variant)[0]
    items: list[dict[str, Any]] = []
    if hybrid:
        table = table or SubstituteTable(load_table(acfg))
        items = decide(result, facts, screen, table)
        prompt = render_prompt(acfg, result, extras, items, template)
    else:
        prompt = render_list_prompt(acfg, result, extras, template)
    reply: dict[str, Any] | None = None
    violations: list[Violation] = []
    history: list[dict[str, Any]] = []
    error, attempts = None, 0
    for attempt in range(2):
        attempts += 1
        this: dict[str, Any] | None = None
        text: dict[str, Any] | None = None
        ignored: list[str] = []
        this_violations: list[Violation] = []
        stop = False
        try:
            data = client.complete_json(prompt, ANSWER_SCHEMA if hybrid else LIST_SCHEMA,
                                        purpose="answer").data
            if hybrid:
                text = TextReply.model_validate(data).model_dump(mode="json")
                this, ignored = assemble(items, text)
            else:
                this = text = AnswerReply.model_validate(data).model_dump(mode="json")
            reply = this
            violations = this_violations = check(this, facts, screen)
            error = "; ".join(f"{v.ingredient or 'text'}: {v.detail}"
                              for v in violations)[:800] or None
            stop = not violations
        except LLMReplyError as exc:
            error = str(exc)
        except LLMError as exc:
            error, stop = str(exc), True
        except (ValidationError, ValueError) as exc:
            error = "reply failed validation: " + " ".join(str(exc).split())[:600]
        history.append({"attempt": attempts, "text": text, "reply": this, "ignored": ignored,
                        "error": error, "violations": [asdict(v) for v in this_violations]})
        if stop:
            break
        if attempt == 0:
            prompt += RETRY_NOTE.format(error=error)
    if reply is None:
        return AnswerResult("invalid", attempts=attempts, error=error, history=history,
                            decisions=items)
    final = repair(reply, facts, screen) if violations else reply
    return AnswerResult("repaired" if violations else "ok", answer=final,
                        warnings=warnings(acfg, facts, extras, final, clarify), raw=reply,
                        violations=[asdict(v) for v in violations], history=history,
                        decisions=items, attempts=attempts,
                        error=error if violations else None)


def load_table(acfg: Config) -> Config:
    from pantrypath_ml import config

    return config.load("substitutes", acfg.path(acfg["substitutes_file"]))
