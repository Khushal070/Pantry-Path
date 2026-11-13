"""A1: chat messages -> RequestInfo (query, zip, avoid, diet) plus extras, checked twice.

The model fills a closed JSON schema built from RequestInfo.model_json_schema() plus an
extras object (servings, tweaks, unsupported). Its reply is validated against RequestInfo
(one retry on an unusable reply, like refine.py). Then a deterministic cross-check scans
the user's messages for allergen, diet and unsupported-need words (configs/
parse_request.yaml). If the model's avoid misses an allergen the scan found, or its diet
is less strict, the result needs the user's confirmation, with a suggested request that
takes the stricter of the two. Unsupported needs from the scan always end up in
extras.unsupported. An allergen the model put in avoid that the chat never mentions (no
avoid or "fine" phrase names it, and no condition such as celiac implies it) also needs
confirmation, and so does an ambiguous word ("nuts": peanuts too?) the model didn't
resolve. The scan only ever adds caution: it never removes an allergen the model chose to
avoid. The ZIP is checked against the chat the same way: a ZIP the chat doesn't contain
is dropped, a missing one is taken from the chat, and a different one needs confirmation.
"""

import json
import re
import string
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from pantrypath_core.diet_rules import Allergen, Diet
from pantrypath_core.result import RequestInfo
from pantrypath_pipeline.config import Config
from pantrypath_pipeline.errors import LLMError, PipelineError
from pantrypath_pipeline.llm import LLMClient, LLMReplyError

ALLERGEN_ORDER = [a.value for a in Allergen]
DIET_RANK = {Diet.none.value: 0, Diet.vegetarian.value: 1, Diet.vegan.value: 2}
RETRY_NOTE = (
    "\n\nYour previous answer could not be used: {error}\n"
    "Answer again with one JSON object that follows the instructions above."
)
EXTRAS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "servings": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "tweaks": {"type": "array", "items": {"type": "string"}},
        "unsupported": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["servings", "tweaks", "unsupported"],
    "additionalProperties": False,
}
_KEEP = ("type", "properties", "items", "enum", "anyOf")


def _closed(schema: Any, defs: dict[str, Any]) -> Any:
    """Inline $refs and keep the subset every server's structured output takes: closed
    objects, every field required, no titles, defaults or patterns."""
    if isinstance(schema, list):
        return [_closed(s, defs) for s in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        return _closed(defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    out = {k: _closed(v, defs) if k != "properties" else
           {name: _closed(p, defs) for name, p in v.items()}
           for k, v in schema.items() if k in _KEEP}
    if out.get("type") == "object":
        out["required"] = list(out.get("properties", {}))
        out["additionalProperties"] = False
    return out


def parse_schema() -> dict[str, Any]:
    """RequestInfo's fields, zip nullable (a chat may not give one), plus extras."""
    base = RequestInfo.model_json_schema()
    schema = _closed(base, base.get("$defs", {}))
    schema["properties"]["zip"] = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    # At most one entry per allergen: Ollama 0.40.15 enforces maxItems in `format` (it
    # stops a repeating list), but ignores uniqueItems, so ParseReply drops repeats.
    schema["properties"]["avoid"]["maxItems"] = len(ALLERGEN_ORDER)
    schema["properties"]["extras"] = EXTRAS_SCHEMA
    schema["required"] = list(schema["properties"])
    return schema


PARSE_SCHEMA = parse_schema()


class Extras(BaseModel):
    servings: int | None = Field(default=None, ge=1)
    tweaks: list[str] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)


class ParseReply(BaseModel):
    """What the model must return (validated before use)."""

    query: str = Field(min_length=1)
    zip: str | None = None
    avoid: list[Allergen] = Field(default_factory=list)
    diet: Diet = Diet.none
    extras: Extras = Field(default_factory=Extras)

    @field_validator("avoid")
    @classmethod
    def _unique(cls, avoid: list[Allergen]) -> list[Allergen]:
        return list(dict.fromkeys(avoid))


# ---------------------------------------------------------------- prompt


def load_prompt(prcfg: Config) -> tuple[string.Template, str]:
    """The prompt template and its version (first line: 'prompt_version: ...')."""
    path = prcfg.path(prcfg["prompt_file"])
    if not path.is_file():
        raise PipelineError(f"Prompt file not found: {path}")
    first, _, body = path.read_text().partition("\n")
    if not first.startswith("prompt_version:"):
        raise PipelineError(f"{path} must start with 'prompt_version: ...'")
    return string.Template(body), first.split(":", 1)[1].strip()


def prompt_version(prcfg: Config, variant: str) -> str:
    return f"{load_prompt(prcfg)[1]}+{variant}"


def render_chat(messages: list[dict[str, str]]) -> str:
    names = {"user": "Shopper", "assistant": "Assistant"}
    return "\n".join(f"{names.get(m['role'], m['role'])}: {m['content']}" for m in messages)


def render_prompt(prcfg: Config, variant: str, messages: list[dict[str, str]],
                  template: string.Template | None = None) -> str:
    if variant not in prcfg["variants"]:
        raise PipelineError(f"Unknown prompt variant {variant!r}; use one of "
                            f"{sorted(prcfg['variants'])}")
    settings = prcfg["variants"][variant]
    # extra_rules: true for the extra_rules text, or the name of another rules text
    rules_key = settings.get("extra_rules")
    rules_key = "extra_rules" if rules_key is True else rules_key
    rules = f"\nExtra rules:\n{prcfg[rules_key]}" if rules_key else ""
    examples = ""
    pool = prcfg[settings.get("example_set") or "few_shot_examples"]
    shown = pool[:int(settings.get("examples") or 0)]
    if shown:
        examples = "\nExamples:\n" + "\n".join(
            f"Chat:\n{render_chat(e['chat'])}\nAnswer:\n{json.dumps(e['reply'])}\n"
            for e in shown)
    return (template or load_prompt(prcfg)[0]).substitute(
        allergens=", ".join(ALLERGEN_ORDER),
        diets=", ".join(DIET_RANK),
        unsupported_labels=", ".join(prcfg["unsupported_labels"]),
        rules=rules, examples=examples, chat=render_chat(messages),
    )


# ---------------------------------------------------------------- cross-check


def _forms(word: str) -> list[str]:
    return [word, f"{word}s", f"{word}es"]


def _alternation(words: list[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


def _cue(words: list[str]) -> re.Pattern:
    """A cue phrase; it may end in punctuation ("allergies:"), so it ends with a lookahead
    instead of a word boundary."""
    return re.compile(rf"\b(?:{_alternation(words)})(?![a-z0-9])")


# A list after a cue is split into items at these separators
_ITEM_SPLIT = re.compile(r"(,|&|/|\(|\)|:|\band\b|\bor\b)")


@dataclass
class Scan:
    avoid: list[str] = field(default_factory=list)
    diet: str = Diet.none.value
    unsupported: list[str] = field(default_factory=list)
    # allergens named in an avoid or "fine" phrase, or implied by a condition (celiac)
    mentioned: list[str] = field(default_factory=list)
    # allergens an avoided word leaves open ("nuts": peanuts too?); never added to avoid
    clarify: list[str] = field(default_factory=list)


class CrossCheck:
    """Keyword scan of the user's messages for allergens, diets and unsupported needs.

    Every avoid or "fine" mention is an event (position, kind, allergen, word). A word can
    stand for several allergens ("seafood": fish and shellfish). A "fine" mention clears an
    allergen only when it names the whole allergen ("eggs are fine", "dairy is fine"); a
    narrower word only withdraws itself, so "allergic to shrimp but crab is fine" still
    avoids shellfish.
    """

    def __init__(self, settings: dict[str, Any]):
        self.s = settings
        # word form -> (the configured word, the allergens it stands for)
        self.term_info: dict[str, tuple[str, list[str]]] = {}
        for allergen, words in settings["allergens"].items():
            for w in words:
                for form in _forms(w):
                    _, allergens = self.term_info.setdefault(form, (w, []))
                    if allergen not in allergens:
                        allergens.append(allergen)
        self.category = {a: set(settings.get("category_words", {}).get(a) or [words[0]])
                         for a, words in settings["allergens"].items()}
        self.ambiguous = dict(settings.get("ambiguous") or {})
        self.term_to_unsupported = {
            form: label for label, words in settings["unsupported_allergens"].items()
            for w in words for form in _forms(w)}
        terms = _alternation([*self.term_info, *self.term_to_unsupported])
        self.terms = re.compile(rf"\b({terms})\b")
        self.fine_after = re.compile(rf"\b({terms})\s+(?:{_alternation(settings['fine_after'])})\b")
        self.fine_before = _cue(settings["fine_before"])
        self.avoid_after = re.compile(
            rf"\b({terms})[\s-]+(?:{_alternation(settings['avoid_after'])})\b")
        self.avoid_before = _cue(settings["avoid_before"])
        self.conditions = re.compile(rf"\b({_alternation(list(settings['conditions']))})\b")
        self.breaks = re.compile(rf"\b(?:{_alternation(settings['clause_breaks'])})\b")
        self.diets = [(diet, re.compile(rf"\b(?:{_alternation(words)})\b"))
                      for diet, words in settings["diets"].items()]
        self.negation = re.compile(rf"\b(?:{_alternation(settings['diet_negations'])})\s+$")
        self.unsupported = [(label, re.compile(rf"\b(?:{_alternation(words)})\b"))
                            for label, words in settings["unsupported"].items()]

    def _list_terms(self, text: str, start: int) -> list[re.Match]:
        """Allergen words in the list right after a cue: up to the sentence end or a
        clause break, item by item, stopping at an item of more than 3 words."""
        end = re.search(r"[.;!?\n]", text[start:])
        span = text[start:start + end.start()] if end else text[start:]
        cut = self.breaks.search(span)
        if cut:
            span = span[:cut.start()]
        found, offset = [], start
        for item in _ITEM_SPLIT.split(span):
            if len(item.split()) > 3:
                break
            found += [m for m in self.terms.finditer(text, offset, offset + len(item))]
            offset += len(item)
        return found

    def _list_terms_before(self, text: str, end: int) -> list[re.Match]:
        """Allergen words in the list right before a word with a cue after it ("fish and
        shellfish allergy", "sesame, soy and tree nut allergy"): item by item backwards,
        each item exactly an allergen word, from the sentence or clause start."""
        start = max((m.end() for m in re.finditer(r"[.;!?\n]", text[:end])), default=0)
        cut = list(self.breaks.finditer(text, start, end))
        if cut:
            start = cut[-1].end()
        parts = _ITEM_SPLIT.split(text[start:end])
        if not parts or parts[-1].strip():    # the cued word must follow a separator
            return []
        found: list[re.Match] = []
        pos = end - len(parts[-1])
        i = len(parts) - 2
        while i >= 1:
            sep, item = parts[i], parts[i - 1]
            pos -= len(sep)
            item_end, item_start = pos, pos - len(item)
            lead = len(item) - len(item.lstrip())
            trail = len(item) - len(item.rstrip())
            m = self.terms.fullmatch(text, item_start + lead, item_end - trail) \
                if item.strip() else None
            if m is None or sep.strip() in ("(", ")", ":"):
                break
            found.append(m)
            pos = item_start
            i -= 2
        return found

    def scan_text(self, text: str) -> tuple[list[tuple[int, str, str, str]], set[str],
                                            str | None]:
        """(events (position, 'avoid'|'fine', allergen, word), unsupported labels, last diet)."""
        text = text.lower().replace("\u2019", "'")
        events: list[tuple[int, str, str, str]] = []
        unsupported: set[str] = set()
        fine_spans: list[tuple[int, int]] = []

        def add(kind: str, m: re.Match) -> None:
            word = m.group(1)
            if word in self.term_info:
                base, allergens = self.term_info[word]
                events.extend((m.start(1), kind, a, base) for a in allergens)
            elif kind == "avoid":
                unsupported.add(self.term_to_unsupported[word])

        for m in self.fine_after.finditer(text):
            add("fine", m)
            fine_spans.append((m.start(), m.end()))
        for cue in self.fine_before.finditer(text):
            terms = self._list_terms(text, cue.end())
            for m in terms:
                add("fine", m)
            fine_spans.append((cue.start(), terms[-1].end() if terms else cue.end()))

        def in_fine(pos: int) -> bool:
            return any(a <= pos < b for a, b in fine_spans)

        for m in self.avoid_after.finditer(text):
            if not in_fine(m.start()):
                add("avoid", m)
                for before in self._list_terms_before(text, m.start(1)):
                    if not in_fine(before.start()):
                        add("avoid", before)
        for cue in self.avoid_before.finditer(text):
            if in_fine(cue.start()):
                continue
            for m in self._list_terms(text, cue.end()):
                if not in_fine(m.start()):
                    add("avoid", m)
        for m in self.conditions.finditer(text):
            events.append((m.start(), "avoid", self.s["conditions"][m.group(1)], m.group(1)))

        diet, diet_pos = None, -1
        for name, pattern in self.diets:
            for m in pattern.finditer(text):
                if m.start() > diet_pos and not self.negation.search(text[:m.start()]):
                    diet, diet_pos = name, m.start()
        for label, pattern in self.unsupported:
            if pattern.search(text):
                unsupported.add(label)
        return events, unsupported, diet

    def scan(self, messages: list[dict[str, str]]) -> Scan:
        """User messages in order: an avoid adds, a later 'fine' withdraws, last diet wins."""
        avoid: dict[str, set[str]] = {}  # allergen -> the words that asked to avoid it
        mentioned: set[str] = set()
        unsupported: set[str] = set()
        diet = Diet.none.value
        for message in messages:
            if message["role"] != "user":
                continue
            events, found, last_diet = self.scan_text(message["content"])
            mentioned |= {allergen for _, _, allergen, _ in events}
            # at one position a "fine" phrase wins ("eggs are fine" inside a "no" list)
            for _, kind, allergen, word in sorted(events, key=lambda e: (e[0], e[1] == "fine")):
                if kind == "avoid":
                    avoid.setdefault(allergen, set()).add(word)
                elif word in self.category[allergen]:
                    avoid.pop(allergen, None)
                elif allergen in avoid:
                    avoid[allergen].discard(word)
                    if not avoid[allergen]:
                        del avoid[allergen]
            unsupported |= found
            diet = last_diet or diet
        words = {w for ws in avoid.values() for w in ws}
        clarify = sorted({other for word, other in self.ambiguous.items()
                          if word in words and other not in mentioned})
        return Scan([a for a in ALLERGEN_ORDER if a in avoid], diet, sorted(unsupported),
                    [a for a in ALLERGEN_ORDER if a in mentioned], clarify)

    def unsupported_labels(self, values: list[str], labels: list[str]) -> list[str]:
        """Map the model's free-text unsupported needs to labels where a word matches."""
        out: set[str] = set()
        for value in values:
            norm = re.sub(r"[\s-]+", "_", value.strip().lower())
            if norm in labels:
                out.add(norm)
                continue
            text = value.lower()
            hits = {label for label, p in self.unsupported if p.search(text)}
            hits |= {self.term_to_unsupported[m.group(1)] for m in self.terms.finditer(text)
                     if m.group(1) in self.term_to_unsupported}
            out |= hits or {norm}
        return sorted(out)


# ---------------------------------------------------------------- ZIP check

# A US ZIP or ZIP+4 in free text, not part of a longer number
ZIP_IN_TEXT = re.compile(r"(?<![\d-])(\d{5})(?:-\d{4})?(?![\d-])")
ZIP_FULL = re.compile(r"\s*(\d{5})(?:-\d{4})?\s*")


def normalize_zip(value: Any) -> Any:
    """'02115-1234' -> '02115'; anything else is returned unchanged for validation."""
    if isinstance(value, str):
        m = ZIP_FULL.fullmatch(value)
        if m:
            return m.group(1)
    return value


def chat_zip(messages: list[dict[str, str]]) -> str | None:
    """The last ZIP the user wrote (a later message overrides an earlier one)."""
    found = [m.group(1) for message in messages if message["role"] == "user"
             for m in ZIP_IN_TEXT.finditer(message["content"])]
    return found[-1] if found else None


# ---------------------------------------------------------------- parse


@dataclass
class ParseResult:
    """status: ok | needs_confirmation (cross-check conflict) | incomplete (no zip) |
    invalid (no usable reply)."""

    status: str
    request: dict[str, Any] | None = None  # RequestInfo as JSON, when complete
    fields: dict[str, Any] | None = None  # the model's parsed fields
    extras: dict[str, Any] = field(default_factory=dict)
    model_unsupported: list[str] = field(default_factory=list)  # before the scan's additions
    conflicts: list[str] = field(default_factory=list)
    unmentioned: list[str] = field(default_factory=list)  # in avoid, never named in the chat
    suggested: dict[str, Any] | None = None  # the stricter request to confirm
    missing: list[str] = field(default_factory=list)
    scan: dict[str, Any] = field(default_factory=dict)
    # allergens to ask about ("nuts": peanuts too?); in suggested, never added to avoid
    clarify: list[str] = field(default_factory=list)
    # where the request's ZIP came from: model | chat (the model gave none) | None
    zip_source: str | None = None
    notes: list[str] = field(default_factory=list)  # query and ZIP fixes made by code
    error: str | None = None
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate(data: dict[str, Any]) -> tuple[ParseReply, dict[str, Any] | None]:
    """The parsed reply and, with a ZIP, the RequestInfo. Raises ValueError when unusable.

    A ZIP+4 is cut to its first five digits here, so it never costs a retry.
    """
    reply = ParseReply.model_validate({**data, "zip": normalize_zip(data.get("zip"))})
    if reply.zip is None:
        return reply, None
    request = RequestInfo(query=reply.query, zip=reply.zip, avoid=reply.avoid, diet=reply.diet)
    return reply, request.model_dump(mode="json")


def clean_query(prcfg: Config, query: str) -> str:
    """The dish query without the diet words before it and the serving forms after it
    (parse_request.yaml query_cleanup), so the catalog lookup sees the dish's name."""
    settings = prcfg.get("query_cleanup") or {}
    words = query.split()
    lead = {w.lower() for w in settings.get("leading") or []}
    trail = {w.lower() for w in settings.get("trailing") or []}
    changed = True
    while changed and len(words) > 1:
        changed = False
        for n in (2, 1):   # two-word phrases ("gluten free") first
            if len(words) > n and " ".join(words[:n]).lower() in lead:
                words, changed = words[n:], True
                break
            if len(words) > n and " ".join(words[-n:]).lower() in trail:
                words, changed = words[:-n], True
                break
    return " ".join(words)


def _request(reply: ParseReply, zip_: str | None) -> dict[str, Any] | None:
    """RequestInfo as JSON with this ZIP, or None (no ZIP, or one RequestInfo rejects)."""
    if zip_ is None:
        return None
    try:
        return RequestInfo(query=reply.query, zip=zip_, avoid=reply.avoid,
                           diet=reply.diet).model_dump(mode="json")
    except ValidationError:
        return None


def catalog_resolver(pcfg: Config) -> Callable[[str | None], str | None]:
    """query -> catalog dish_id (or None), with the request flow's catalog and fuzzy
    threshold: the lookup build_result() makes."""
    from pantrypath_pipeline.store import find_dish

    db = pcfg.path(pcfg["catalog"]["sqlite_output"])
    min_score = float(pcfg["request"]["fuzzy_min_score"])

    def resolve(query: str | None) -> str | None:
        if not query:
            return None
        dish = find_dish(db, query, min_score)
        return dish.dish_id if dish else None
    return resolve


def parse_request(prcfg: Config, messages: list[dict[str, str]], client: LLMClient, *,
                  variant: str | None = None, template: string.Template | None = None,
                  checker: CrossCheck | None = None,
                  resolve: Callable[[str | None], str | None] | None = None) -> ParseResult:
    """Parse one chat (one retry on an unusable reply), then cross-check it. `variant`
    defaults to parse_request.yaml's default. With `resolve` (catalog_resolver), a dish
    query the catalog doesn't know is cleaned (clean_query) when the cleaned one is known."""
    variant = variant or prcfg["default"]
    checker = checker or CrossCheck(prcfg["cross_check"])
    prompt = render_prompt(prcfg, variant, messages, template)
    scan = checker.scan(messages)
    error: str | None = None
    parsed: tuple[ParseReply, dict[str, Any] | None] | None = None
    attempts = 0
    for attempt in range(2):
        attempts += 1
        try:
            reply = client.complete_json(prompt, PARSE_SCHEMA, purpose=f"parse_request:{variant}")
            parsed = _validate(reply.data)
            break
        except LLMReplyError as exc:  # answered, but unusable: worth one retry
            error = str(exc)
        except LLMError as exc:  # the server failed even after its own retries
            error = str(exc)
            break
        except (ValidationError, ValueError) as exc:
            error = "reply failed validation: " + " ".join(str(exc).split())[:600]
        if attempt == 0:
            prompt += RETRY_NOTE.format(error=error)
    if parsed is None:
        return ParseResult("invalid", scan=asdict(scan), error=error, attempts=attempts)

    reply, request = parsed
    notes: list[str] = []
    query = reply.query
    if resolve is not None and resolve(query) is None:
        cleaned = clean_query(prcfg, query)
        if cleaned != query and resolve(cleaned) is not None:
            query = cleaned
    if query != reply.query:
        notes.append(f"dish query {reply.query!r} -> {query!r} for the catalog lookup")
        reply = reply.model_copy(update={"query": query})
        if request:
            request = {**request, "query": query}
    fields = reply.model_dump(mode="json")
    extras = fields.pop("extras")
    model_unsupported = checker.unsupported_labels(extras["unsupported"],
                                                   prcfg["unsupported_labels"])
    extras["unsupported"] = sorted(set(model_unsupported) | set(scan.unsupported))
    conflicts = [f"the chat mentions {a}, which the model did not put in avoid"
                 for a in scan.avoid if a not in fields["avoid"]]
    if DIET_RANK[scan.diet] > DIET_RANK[fields["diet"]]:
        conflicts.append(f"the chat says {scan.diet}, the model chose {fields['diet']}")
    # An ambiguous word the model resolved cautiously (peanuts in avoid) needs no question
    clarify = [a for a in scan.clarify if a not in fields["avoid"]]
    # the suggested fix avoids both: tree nuts (the scan's own reading) and peanuts
    conflicts += [f"the chat mentions nuts, which can mean tree nuts or {a.replace('_', ' ')}: "
                  "the suggested fix avoids both" for a in clarify]
    # Asked, never dropped: the shopper may have said it in words the scan doesn't know
    unmentioned = [a for a in fields["avoid"]
                   if a not in scan.mentioned and a not in scan.clarify]
    conflicts += [f"the model put {a} in avoid, which the chat never mentions"
                  for a in unmentioned]

    # ZIP: the chat is the authority. fields keeps the model's own answer (for scoring);
    # the request uses the resolved one.
    in_chat, model_zip = chat_zip(messages), fields["zip"]
    zip_, zip_source = model_zip, "model" if model_zip else None
    if model_zip and in_chat is None:
        zip_, zip_source = None, None
        notes.append(f"the model gave ZIP {model_zip}, which is not in the chat; dropped it")
    elif in_chat and model_zip is None:
        zip_, zip_source = in_chat, "chat"
        notes.append(f"the model gave no ZIP; used {in_chat} from the chat")
    elif in_chat and model_zip != in_chat:
        conflicts.append(f"the chat gives ZIP {in_chat}, the model chose {model_zip}")
    if zip_ != model_zip:
        request = _request(reply, zip_)
        if zip_ and request is None:
            zip_source = None

    suggested = None
    if conflicts:
        suggested = {**fields, "zip": in_chat or zip_,
                     "avoid": [a for a in ALLERGEN_ORDER
                               if a in {*fields["avoid"], *scan.avoid, *clarify}],
                     "diet": max(fields["diet"], scan.diet, key=DIET_RANK.__getitem__)}
    missing = [] if request else ["zip"]
    status = "needs_confirmation" if conflicts else "incomplete" if missing else "ok"
    return ParseResult(status, request=request, fields=fields, extras=extras,
                       model_unsupported=model_unsupported, conflicts=conflicts,
                       unmentioned=unmentioned, suggested=suggested, missing=missing,
                       scan=asdict(scan), clarify=clarify, zip_source=zip_source,
                       notes=notes, attempts=attempts)
