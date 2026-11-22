"""What the chat needs from the models and the request flow, whichever engine runs them.

live (live.py): A1, A2 and A3 from the champion (pantrypath_ml.runtime) and the data
pipeline's build_result, with live store data.
demo (demo.py): the same flow offline, for building and showing the app anywhere.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Issue:
    """Something about the parsed request the shopper should look at before searching.

    kind: missed_allergen (the chat names an allergen the parser left out), diet (the chat
    says a stricter diet), clarify ("nuts": peanuts too?), unmentioned (the parser avoids
    an allergen the chat never names), zip (the chat's ZIP differs from the parser's),
    other (a conflict we can't classify: never dropped).
    """

    kind: str
    allergen: str | None = None
    value: str | None = None


@dataclass
class ParseOutcome:
    """One chat, read: the request so far (the stricter suggestion when the parser was
    unsure), what the shopper should check, and the extras the answer uses."""

    status: str                       # ok | invalid
    query: str | None = None          # None: no dish yet
    zip: str | None = None            # None: the chat gives no ZIP
    avoid: list[str] = field(default_factory=list)
    diet: str = "none"
    extras: dict[str, Any] = field(default_factory=dict)  # servings, tweaks, unsupported
    issues: list[Issue] = field(default_factory=list)
    mentioned: list[str] = field(default_factory=list)    # allergens the chat names at all
    chat_avoid: list[str] = field(default_factory=list)   # allergens the chat asks to avoid
    clarify: list[str] = field(default_factory=list)
    in_catalog: bool | None = None    # None: unknown (no dish, or no catalog to look in)
    notes: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class SearchOutcome:
    result: dict[str, Any]                 # result.json, contract v1.0
    answer: dict[str, Any] | None = None   # A3's answer; None when A3 isn't available
    answer_status: str | None = None       # ok | repaired | invalid | unavailable
    warnings: list[str] = field(default_factory=list)


class EngineError(Exception):
    """A search that failed in a way we can explain to the shopper.

    kind: not_found (no such dish), provider (the store search failed), data (reference
    data missing on the server), model (the model server failed), invalid (bad request).
    """

    def __init__(self, message: str, *, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


Progress = Callable[[str], None]   # called with a stage name: searching, answering


class Engine(Protocol):
    mode: str

    def info(self) -> dict[str, Any]: ...

    def parse(self, messages: list[dict[str, str]]) -> ParseOutcome: ...

    def search(self, request: dict[str, Any], extras: dict[str, Any], clarify: list[str],
               progress: Progress) -> SearchOutcome: ...
