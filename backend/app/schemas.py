"""Request and response bodies of the API. Preferences are saved, encrypted, as this model
dumped to JSON (auth.py); chats are kept in memory in the Session shape (chat_memory.py)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.vocab import NEEDS, ordered_allergens
from pantrypath_core.diet_rules import Allergen, Diet

USERNAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,31}$"


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _username(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


class Preferences(BaseModel):
    """What a shopper saves once, so the chat doesn't have to ask every time."""

    name: str = Field(min_length=1, max_length=60)
    zip: str | None = Field(default=None, pattern=r"^\d{5}$")
    avoid: list[Allergen] = Field(default_factory=list)
    diet: Diet = Diet.none
    servings: int | None = Field(default=None, ge=1, le=24)
    needs: list[str] = Field(default_factory=list, description="Needs we can't check yet")
    # Favourite brands by item ("sandwich bread" -> "Dave's Killer Bread"): the shopper's
    # own choice, never Kroger's product record. A save that leaves it out keeps it.
    favourites: dict[str, str] = Field(default_factory=dict, max_length=200)

    _strip_name = field_validator("name", mode="before")(_strip)

    @field_validator("zip", mode="before")
    @classmethod
    def _blank_zip(cls, value: Any) -> Any:
        value = _strip(value)
        return value or None

    @field_validator("avoid")
    @classmethod
    def _order_avoid(cls, value: list[Allergen]) -> list[Allergen]:
        return [Allergen(a) for a in ordered_allergens(a.value for a in value)]

    @field_validator("favourites")
    @classmethod
    def _clean_favourites(cls, value: dict[str, str]) -> dict[str, str]:
        out = {}
        for item, brand in value.items():
            item, brand = item.strip().lower()[:80], brand.strip()[:80]
            if item and brand:
                out[item] = brand
        return out

    @field_validator("needs")
    @classmethod
    def _known_needs(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(NEEDS))
        if unknown:
            raise ValueError(f"unknown needs {unknown}; use any of {NEEDS}")
        return [n for n in NEEDS if n in set(value)]


class User(Preferences):
    """An account as the API shows it: never with its password hash."""

    id: str
    username: str
    created_at: datetime
    updated_at: datetime


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    username: str = Field(pattern=USERNAME_PATTERN,
                          description="3 to 32 of a-z, 0-9, dot, dash, underscore")
    password: str = Field(min_length=8, max_length=128)

    _strip_name = field_validator("name", mode="before")(_strip)
    _clean_username = field_validator("username", mode="before")(_username)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

    _clean_username = field_validator("username", mode="before")(_username)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class FavouriteIn(BaseModel):
    """Set (brand) or remove (brand null) one favourite."""

    item: str = Field(min_length=1, max_length=80)
    brand: str | None = Field(default=None, max_length=80)


class DeleteAccountIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class Message(BaseModel):
    """kind: text (said in words), build (a guided build: steps to choose from, guided.py),
    confirm (the request to check before searching; at the end of a build, with its
    ingredients), search (a search and where its results are), error."""

    id: str
    role: Literal["user", "assistant"]
    kind: Literal["text", "build", "confirm", "search", "error"]
    text: str = ""
    data: dict[str, Any] | None = None
    created_at: datetime


class Session(BaseModel):
    id: str
    user_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    # A1 reads the messages from here on: a confirmed search starts a fresh request
    context_start: int = 0
    messages: list[Message] = Field(default_factory=list)


class SessionSummary(BaseModel):
    id: str
    user_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    preview: str = ""


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)

    _strip_text = field_validator("text", mode="before")(_strip)


class SearchIn(BaseModel):
    """The request the shopper confirmed (possibly edited) on a confirm card."""

    message_id: str
    query: str = Field(min_length=1, max_length=120)
    zip: str = Field(pattern=r"^\d{5}$")
    avoid: list[Allergen] = Field(default_factory=list)
    diet: Diet = Diet.none
    servings: int | None = Field(default=None, ge=1, le=24)

    _strip_query = field_validator("query", "zip", mode="before")(_strip)


class BuildActionIn(BaseModel):
    """A move on a build card: next / back / finish (with the current step's picks),
    usual (search now), add (text), resolve (name + choice: swap, anyway, remove), cancel."""

    action: Literal["next", "back", "finish", "usual", "add", "resolve", "choose", "cancel"]
    picks: list[str] | None = Field(default=None, max_length=20)
    text: str | None = Field(default=None, max_length=300)
    name: str | None = Field(default=None, max_length=80)
    choice: Literal["swap", "anyway", "remove"] | None = None
    key: str | None = Field(default=None, max_length=300)   # choose: the product picked


class Job(BaseModel):
    """A search running in the background. Results live in memory only and expire."""

    id: str
    status: Literal["running", "done", "failed"]
    stage: str
    error: str | None = None
    error_kind: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    expires_at: datetime | None = None
    result: dict[str, Any] | None = None   # result.json, contract v1.0
    answer: dict[str, Any] | None = None   # A3's answer; None when A3 isn't available
    answer_status: str | None = None
    warnings: list[str] = Field(default_factory=list)
    # {"a2": model name or None (catalog dish), "stronger": bool}: who worked out the
    # ingredients (stronger_model.py)
    model_used: dict[str, Any] | None = None
    # The saved list this search made (GET /api/lists/{id}); list_error: saving failed
    list_id: str | None = None
    list_error: bool = False
    # The shopper's chosen product per item (a guided build), with store and aisle
    choices: dict[str, Any] | None = None


class SavedItem(BaseModel):
    id: str
    name: str
    category: str
    status: Literal["buy", "substitute", "check_label", "skip"]
    substitute: str | None = None
    quantity: float | None = None
    unit: str | None = None
    ticked: bool = False
    brand: str | None = None   # the brand the shopper picked (theirs, not Kroger's record)


class SavedList(BaseModel):
    """A saved shopping list (lists.py): the shopper's own data, never Kroger's."""

    id: str
    dish: str
    cuisine: str
    zip: str
    avoid: list[str]
    diet: str
    servings: int | None = None
    items: list[SavedItem]
    steps: list[str]
    source: str
    created_at: datetime
    updated_at: datetime | None = None


class SavedListSummary(BaseModel):
    id: str
    dish: str
    created_at: datetime
    items: int
    ticked: int


class TickIn(BaseModel):
    ticked: bool


class ChatReply(BaseModel):
    session: Session
    job: Job | None = None
