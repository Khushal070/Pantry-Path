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
confirmation. The scan only ever adds caution: it never removes an allergen the model
chose to avoid.
"""

import json
import re
import string
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


@dataclass
class Scan:
    avoid: list[str] = field(default_factory=list)
    diet: str = Diet.none.value
    unsupported: list[str] = field(default_factory=list)
    # allergens named in an avoid or "fine" phrase, or implied by a condition (celiac)
    mentioned: list[str] = field(default_factory=list)


class CrossCheck:
    """Keyword scan of the user's messages for allergens, diets and unsupported needs."""

    def __init__(self, settings: dict[str, Any]):
        self.s = settings
        self.term_to_allergen = {form: allergen for allergen, words in settings["allergens"].items()
                                 for w in words for form in _forms(w)}
        self.term_to_unsupported = {
            form: label for label, words in settings["unsupported_allergens"].items()
            for w in words for form in _forms(w)}
        terms = _alternation([*self.term_to_allergen, *self.term_to_unsupported])
        self.terms = re.compile(rf"\b({terms})\b")
        self.fine_after = re.compile(rf"\b({terms})\s+(?:{_alternation(settings['fine_after'])})\b")
        self.fine_before = re.compile(rf"\b(?:{_alternation(settings['fine_before'])})\b")
        self.avoid_after = re.compile(
            rf"\b({terms})[\s-]+(?:{_alternation(settings['avoid_after'])})\b")
        self.avoid_before = re.compile(rf"\b(?:{_alternation(settings['avoid_before'])})\b")
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
        for item in re.split(r"(,|\band\b|\bor\b|/)", span):
            if len(item.split()) > 3:
                break
            found += [m for m in self.terms.finditer(text, offset, offset + len(item))]
            offset += len(item)
        return found

    def scan_text(self, text: str) -> tuple[list[tuple[int, str, str]], set[str], str | None]:
        """(events (position, 'avoid'|'fine', allergen), unsupported labels, last diet)."""
        text = text.lower().replace("’", "'")
        events: list[tuple[int, str, str]] = []
        unsupported: set[str] = set()
        fine_spans: list[tuple[int, int]] = []

        def add(kind: str, m: re.Match) -> None:
            word = m.group(1)
            if word in self.term_to_allergen:
                events.append((m.start(1), kind, self.term_to_allergen[word]))
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
        for cue in self.avoid_before.finditer(text):
            if in_fine(cue.start()):
                continue
            for m in self._list_terms(text, cue.end()):
                if not in_fine(m.start()):
                    add("avoid", m)
        for m in self.conditions.finditer(text):
            events.append((m.start(), "avoid", self.s["conditions"][m.group(1)]))

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
        """User messages in order: an avoid adds, a later 'fine' removes, last diet wins."""
        avoid: set[str] = set()
        mentioned: set[str] = set()
        unsupported: set[str] = set()
        diet = Diet.none.value
        for message in messages:
            if message["role"] != "user":
                continue
            events, found, last_diet = self.scan_text(message["content"])
            mentioned |= {allergen for _, _, allergen in events}
            # at one position a "fine" phrase wins ("eggs are fine" inside a "no" list)
            for _, kind, allergen in sorted(events, key=lambda e: (e[0], e[1] == "fine")):
                if kind == "avoid":
                    avoid.add(allergen)
                else:
                    avoid.discard(allergen)
            unsupported |= found
            diet = last_diet or diet
        return Scan([a for a in ALLERGEN_ORDER if a in avoid], diet, sorted(unsupported),
                    [a for a in ALLERGEN_ORDER if a in mentioned])

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
    error: str | None = None
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate(data: dict[str, Any]) -> tuple[ParseReply, dict[str, Any] | None]:
    """The parsed reply and, with a ZIP, the RequestInfo. Raises ValueError when unusable."""
    reply = ParseReply.model_validate(data)
    if reply.zip is None:
        return reply, None
    request = RequestInfo(query=reply.query, zip=reply.zip, avoid=reply.avoid, diet=reply.diet)
    return reply, request.model_dump(mode="json")


def parse_request(prcfg: Config, messages: list[dict[str, str]], client: LLMClient, *,
                  variant: str = "base", template: string.Template | None = None,
                  checker: CrossCheck | None = None) -> ParseResult:
    """Parse one chat (one retry on an unusable reply), then cross-check it."""
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
    fields = reply.model_dump(mode="json")
    extras = fields.pop("extras")
    model_unsupported = checker.unsupported_labels(extras["unsupported"],
                                                   prcfg["unsupported_labels"])
    extras["unsupported"] = sorted(set(model_unsupported) | set(scan.unsupported))
    conflicts = [f"the chat mentions {a}, which the model did not put in avoid"
                 for a in scan.avoid if a not in fields["avoid"]]
    if DIET_RANK[scan.diet] > DIET_RANK[fields["diet"]]:
        conflicts.append(f"the chat says {scan.diet}, the model chose {fields['diet']}")
    # Asked, never dropped: the shopper may have said it in words the scan doesn't know
    unmentioned = [a for a in fields["avoid"] if a not in scan.mentioned]
    conflicts += [f"the model put {a} in avoid, which the chat never mentions"
                  for a in unmentioned]
    suggested = None
    if conflicts:
        suggested = {**fields, "avoid": [a for a in ALLERGEN_ORDER
                                         if a in {*fields["avoid"], *scan.avoid}],
                     "diet": max(fields["diet"], scan.diet, key=DIET_RANK.__getitem__)}
    missing = [] if request else ["zip"]
    status = "needs_confirmation" if conflicts else "incomplete" if missing else "ok"
    return ParseResult(status, request=request, fields=fields, extras=extras,
                       model_unsupported=model_unsupported, conflicts=conflicts,
                       unmentioned=unmentioned, suggested=suggested, missing=missing,
                       scan=asdict(scan), attempts=attempts)
