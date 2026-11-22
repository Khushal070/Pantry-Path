"""The chat: a user's sessions, messages, confirm cards and searches, over the JSON store.

Every method takes the logged-in user (main.py loads it from the login cookie). A chat
that belongs to someone else is "not found", exactly like one that doesn't exist.

A session's messages, in the order the shopper sees them:
- text: what the shopper typed, or a question from the assistant ("Which ZIP code?").
  These, from context_start on, are what A1 reads.
- confirm: the request read from the chat and the user's preferences (drafts.py). Only
  the newest message can be confirmed, so a correction typed after a card replaces it.
- search: a confirmed request and the id of its search. The results stay in memory
  (jobs.py); the message keeps only the request, the dish name and the outcome, so the
  search can be run again after the results expire.
- error: something went wrong on our side.
"""

import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.drafts import build_draft, confirmed_request
from app.engines.base import Engine
from app.jobs import Jobs, JobState
from app.schemas import SearchIn
from app.store import JsonStore
from app.vocab import allergen_label, join_words
from pantrypath_core.result import RequestInfo

log = logging.getLogger(__name__)

NEW_CHAT = "New chat"
User = dict[str, Any]


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class InvalidRequestError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _message(role: str, kind: str, text: str = "",
             data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": f"m_{secrets.token_hex(6)}", "role": role, "kind": kind, "text": text,
            "data": data, "created_at": _now()}


def chat_context(session: dict[str, Any]) -> list[dict[str, str]]:
    """What A1 reads: the words said since the last confirmed search."""
    return [{"role": m["role"], "content": m["text"]}
            for m in session["messages"][session.get("context_start", 0):]
            if m["kind"] == "text" and m["text"]]


def request_line(request: dict[str, Any], servings: int | None = None) -> str:
    """'Quiche lorraine near 45202, vegetarian, without milk and gluten, for 4'."""
    parts = [f"{request['query'].capitalize()} near {request['zip']}"]
    if request["diet"] != "none":
        parts.append(request["diet"])
    if request["avoid"]:
        parts.append("without " + join_words([allergen_label(a) for a in request["avoid"]]))
    if servings:
        parts.append(f"for {servings}")
    return ", ".join(parts)


class ChatService:
    def __init__(self, store: JsonStore, engine: Engine, jobs: Jobs):
        self.store, self.engine, self.jobs = store, engine, jobs

    # ------------------------------------------------------------ sessions

    def list_sessions(self, user: User) -> list[dict[str, Any]]:
        out = []
        for s in self.store.all("sessions"):
            if s.get("user_id") != user["id"]:
                continue
            last = next((m["text"] for m in reversed(s["messages"]) if m["text"]), "")
            out.append({k: s[k] for k in ("id", "user_id", "title", "created_at",
                                           "updated_at")} | {"preview": last[:90]})
        return sorted(out, key=lambda s: s["updated_at"], reverse=True)

    def create_session(self, user: User) -> dict[str, Any]:
        now = _now()
        session = {"id": f"s_{secrets.token_hex(6)}", "user_id": user["id"],
                   "title": NEW_CHAT, "created_at": now, "updated_at": now,
                   "context_start": 0, "messages": []}
        self.store.put("sessions", session["id"], session)
        return session

    def get_session(self, user: User, session_id: str) -> dict[str, Any]:
        session = self.store.get("sessions", session_id)
        if session is None or session.get("user_id") != user["id"]:
            raise NotFoundError(f"No chat {session_id}")
        return session

    def delete_session(self, user: User, session_id: str) -> None:
        with self.store.locked():
            self.get_session(user, session_id)
            self.store.delete("sessions", session_id)

    def delete_all(self, user_id: str) -> None:
        """Every chat of a user (their account is being deleted)."""
        with self.store.locked():
            for s in self.store.all("sessions"):
                if s.get("user_id") == user_id:
                    self.store.delete("sessions", s["id"])

    def _update(self, user: User, session_id: str,
                change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        def touched(session: dict[str, Any]) -> None:
            change(session)
            session["updated_at"] = _now()

        with self.store.locked():
            self.get_session(user, session_id)
            session = self.store.update("sessions", session_id, touched)
        if session is None:
            raise NotFoundError(f"No chat {session_id}")
        return session

    # ------------------------------------------------------------ the chat

    def post_message(self, user: User, session_id: str, text: str) -> dict[str, Any]:
        """The shopper's message, then the assistant's reply: a question or a confirm card.
        Reading the chat may take a while (A1 on the model server); the lock isn't held."""

        def add_user(s: dict[str, Any]) -> None:
            s["messages"].append(_message("user", "text", text))
            if s["title"] == NEW_CHAT:
                s["title"] = text[:48] + ("…" if len(text) > 48 else "")

        session = self._update(user, session_id, add_user)
        draft: dict[str, Any] = {}
        try:
            draft = build_draft(self.engine.parse(chat_context(session)), user)
            reply = self._reply(draft)
        except Exception:
            log.exception("Reading a message failed (chat %s)", session_id)
            reply = _message("assistant", "error",
                             "Sorry, something went wrong reading that. Please try again.")

        def add_reply(s: dict[str, Any]) -> None:
            s["messages"].append(reply)
            if draft.get("request"):
                s["title"] = draft["request"]["query"].capitalize()

        return self._update(user, session_id, add_reply)

    def _reply(self, draft: dict[str, Any]) -> dict[str, Any]:
        state = draft["state"]
        if state == "invalid":
            return _message("assistant", "text",
                            "Sorry, I couldn't make sense of that. Tell me a dish you'd like "
                            "to cook, for example \"quiche for 4, no dairy\".",
                            {"error": draft.get("error")})
        if state == "need_dish":
            examples = self.engine.info().get("examples") or []
            hint = f" For example: {join_words(examples[:3])}." if examples else ""
            return _message("assistant", "text", f"Which dish would you like to cook?{hint}")
        if state == "need_zip":
            return _message("assistant", "text",
                            "Which 5-digit ZIP code should I search near? You can also save "
                            "one in your preferences so I don't have to ask.")
        return _message("assistant", "confirm",
                        request_line(draft["request"], draft.get("servings")), draft)

    def start_search(self, user: User, session_id: str,
                     body: SearchIn) -> tuple[dict[str, Any], JobState]:
        """Search the request on a confirm card (as the shopper left it)."""
        with self.store.locked():
            session = self.get_session(user, session_id)
            messages = session["messages"]
            card = next((m for m in messages if m["id"] == body.message_id), None)
            if card is None or card["kind"] != "confirm":
                raise NotFoundError(f"No request card {body.message_id} in this chat")
            if card is not messages[-1] or (card["data"] or {}).get("submitted"):
                raise ConflictError("This request was replaced or already searched: use "
                                    "the newest card.")
            request, extras, clarify = confirmed_request(
                card["data"], body.model_dump(mode="json"), user)
            try:
                RequestInfo(**request)
            except ValidationError as exc:
                raise InvalidRequestError(exc.errors()[0]["msg"]) from exc
            job = self.jobs.create(owner=user["id"])
            search = _message("assistant", "search", request_line(request, extras.get("servings")),
                              {"job_id": job.id, "request": request, "extras": extras,
                               "clarify": clarify, "status": "running"})

            def change(s: dict[str, Any]) -> None:
                for m in s["messages"]:
                    if m["id"] == card["id"]:
                        m["data"]["submitted"] = True
                        # what was searched, not the first reading: the shopper may have
                        # edited the card, and saved preferences are applied again
                        m["text"] = search["text"]
                s["messages"].append(search)
                s["context_start"] = len(s["messages"])   # the next message starts afresh
                s["title"] = request["query"].capitalize()

            session = self._update(user, session_id, change)
        self._submit(job, session_id, search["id"], request, extras, clarify)
        return session, job

    def rerun_search(self, user: User, session_id: str,
                     message_id: str) -> tuple[dict[str, Any], JobState]:
        """Search a confirmed request again (its results expired, or it failed)."""
        with self.store.locked():
            session = self.get_session(user, session_id)
            message = next((m for m in session["messages"] if m["id"] == message_id), None)
            if message is None or message["kind"] != "search":
                raise NotFoundError(f"No search {message_id} in this chat")
            data = message["data"]
            current = self.jobs.get(data["job_id"])
            if current is not None and current.status == "running":
                return session, current
            job = self.jobs.create(owner=user["id"])

            def change(s: dict[str, Any]) -> None:
                for m in s["messages"]:
                    if m["id"] == message_id:
                        m["data"].update(job_id=job.id, status="running", error=None)

            session = self._update(user, session_id, change)
        self._submit(job, session_id, message_id, data["request"], data.get("extras") or {},
                     data.get("clarify") or [])
        return session, job

    def _submit(self, job: JobState, session_id: str, message_id: str,
                request: dict[str, Any], extras: dict[str, Any], clarify: list[str]) -> None:
        def on_finish(done: JobState) -> None:
            def change(s: dict[str, Any]) -> None:
                for m in s["messages"]:
                    if m["id"] == message_id and m["data"].get("job_id") == done.id:
                        m["data"]["status"] = done.status
                        m["data"]["error"] = done.error
                        m["data"]["error_kind"] = done.error_kind
                        if done.outcome:   # the dish's name only: no store data on disk
                            m["data"]["dish"] = done.outcome.result["dish"]["name"]

            if self.store.update("sessions", session_id, change) is None:
                log.info("Chat %s was deleted before its search finished", session_id)

        self.jobs.submit(job, lambda progress: self.engine.search(request, extras, clarify,
                                                                  progress), on_finish)

    def job(self, user: User, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None or job.owner != user["id"]:
            raise NotFoundError("These results have expired. Store data isn't kept, so "
                                "search again.")
        return job.view()
