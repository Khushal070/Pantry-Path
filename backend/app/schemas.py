"""Request and response bodies of the API. Users and chats are stored as these models
dumped to JSON (store.py)."""

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


class DeleteAccountIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class Message(BaseModel):
    """kind: text (said in words), confirm (the request to check before searching),
    search (a search and where its results are), error."""

    id: str
    role: Literal["user", "assistant"]
    kind: Literal["text", "confirm", "search", "error"]
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


class ChatReply(BaseModel):
    session: Session
    job: Job | None = None
