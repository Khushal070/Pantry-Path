"""The chat: a user's sessions, messages, confirm cards and searches, in memory only
(chat_memory.py): chats are never saved, and end after PANTRYPATH_CHAT_IDLE_MIN idle minutes.

Every method takes the logged-in user (main.py loads it from the login cookie). A chat
that belongs to someone else is "not found", exactly like one that doesn't exist.

A session's messages, in the order the shopper sees them:
- text: what the shopper typed, or a question from the assistant ("Which ZIP code?").
  These, from context_start on, are what A1 reads.
- confirm: the request read from the chat and the user's preferences (drafts.py). Only
  the newest message can be confirmed, so a correction typed after a card replaces it.
- search: a confirmed request and the id of its search. The results stay in memory
  (jobs.py); the message keeps only the request, the dish name and the outcome, so the
  search can be run again after the results expire. "Ask a stronger model" adds a new
  search message (data.stronger, data.redo_of), so the first answer stays as it was.
- error: something went wrong on our side.
"""

import logging
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app import choices as choice_rules
from app import guided
from app.chat_memory import ChatMemory
from app.drafts import build_draft, confirmed_request
from app.engines.base import Engine, EngineError
from app.jobs import Jobs, JobState
from app.schemas import SearchIn
from app.vocab import allergen_label, join_words
from pantrypath_core.result import RequestInfo

log = logging.getLogger(__name__)

NEW_CHAT = "New chat"
# Also for a chat that never existed or is someone else's: they look the same
CHAT_ENDED = "This chat has ended: chats aren't saved. Start a new one."
User = dict[str, Any]


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class InvalidRequestError(Exception):
    pass


class UnavailableError(Exception):
    """Something this server isn't set up to do (no stronger model)."""


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
    def __init__(self, chats: ChatMemory, engine: Engine, jobs: Jobs,
                 guided_builds: bool = True, lists: Any = None):
        self.chats, self.engine, self.jobs = chats, engine, jobs
        self.lists = lists   # lists.Lists: every finished search is saved (None: not saved)
        # Builds' product searches (choices.py), in the background
        self._options_pool = ThreadPoolExecutor(2, thread_name_prefix="options")
        # Guided builds need an engine that plans (a test stand-in may not)
        self.guided = guided_builds and hasattr(engine, "plan")
        self._screen: Any = None

    @property
    def screen(self) -> Any:
        if self._screen is None:
            from app.screen import Screen
            self._screen = getattr(self.engine, "screen", None) or Screen()
        return self._screen

    # ------------------------------------------------------------ sessions

    def list_sessions(self, user: User) -> list[dict[str, Any]]:
        out = []
        for s in self.chats.all():
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
        self.chats.put(session["id"], session)
        return session

    def get_session(self, user: User, session_id: str) -> dict[str, Any]:
        session = self.chats.get(session_id)
        if session is None or session.get("user_id") != user["id"]:
            raise NotFoundError(CHAT_ENDED)
        return session

    def delete_session(self, user: User, session_id: str) -> None:
        with self.chats.locked():
            self.get_session(user, session_id)
            self.chats.delete(session_id)

    def delete_all(self, user_id: str) -> None:
        """Every chat of a user (their account is being deleted)."""
        with self.chats.locked():
            for s in self.chats.all():
                if s.get("user_id") == user_id:
                    self.chats.delete(s["id"])

    def _update(self, user: User, session_id: str,
                change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        def touched(session: dict[str, Any]) -> None:
            change(session)
            session["updated_at"] = _now()

        with self.chats.locked():
            self.get_session(user, session_id)
            session = self.chats.update(session_id, touched)
        if session is None:
            raise NotFoundError(CHAT_ENDED)
        return session

    # ------------------------------------------------------------ the chat

    def post_message(self, user: User, session_id: str, text: str) -> dict[str, Any]:
        """The shopper's message, then the assistant's reply: a question or a confirm card.
        Reading the chat may take a while (A1 on the model server); the lock isn't held."""

        def add_user(s: dict[str, Any]) -> None:
            s["messages"].append(_message("user", "text", text))
            if s["title"] == NEW_CHAT:
                s["title"] = text[:48] + ("…" if len(text) > 48 else "")

        current = self.get_session(user, session_id)
        open_build = _open_build(current)
        if open_build is not None:   # typed while choosing: additions, not a new request
            return self._typed_into_build(user, session_id, text, open_build["id"])
        summary = _open_summary(current)
        if summary is not None and not guided.is_cancel(text):
            # typed at a build's summary: more items for it ("add ketchup")
            return self.add_more(user, session_id, summary["id"], text, typed=True)
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

        session = self._update(user, session_id, add_reply)
        if reply["kind"] == "build":
            plan = guided.plan_of(reply["data"])
            self._start_options(user, session_id, reply["id"],
                                [o.name for s in plan.steps for o in s.options])
        return session

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
        if self.guided:
            return self._plan_reply(draft)
        return _message("assistant", "confirm",
                        request_line(draft["request"], draft.get("servings")), draft)

    def start_search(self, user: User, session_id: str,
                     body: SearchIn) -> tuple[dict[str, Any], JobState]:
        """Search the request on a confirm card (as the shopper left it)."""
        with self.chats.locked():
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
            guided_search = (card["data"] or {}).get("guided")
            if guided_search:
                guided_search = _confirmed_guided(guided_search, card["data"]["request"],
                                                  request)
            try:
                RequestInfo(**request)
            except ValidationError as exc:
                raise InvalidRequestError(exc.errors()[0]["msg"]) from exc
            job = self.jobs.create(owner=user["id"])
            search = _message("assistant", "search", request_line(request, extras.get("servings")),
                              {"job_id": job.id, "request": request, "extras": extras,
                               "clarify": clarify, "status": "running",
                               "guided": guided_search})

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
        self._submit(job, session_id, search["id"], request, extras, clarify,
                     guided_search=guided_search)
        return session, job

    def rerun_search(self, user: User, session_id: str,
                     message_id: str) -> tuple[dict[str, Any], JobState]:
        """Search a confirmed request again (its results expired, or it failed)."""
        with self.chats.locked():
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
        # a search that asked for the stronger model asks for it again
        self._submit(job, session_id, message_id, data["request"], data.get("extras") or {},
                     data.get("clarify") or [], stronger=bool(data.get("stronger")),
                     guided_search=data.get("guided"), list_id=data.get("list_id"))
        return session, job

    def ask_stronger(self, user: User, session_id: str,
                     message_id: str) -> tuple[dict[str, Any], JobState]:
        """Search a request again with the stronger model working out the ingredients
        (the shopper said the first ones look wrong). Adds a new search message, so the
        first answer stays as it was."""
        offer = self.engine.info().get("stronger_model") or {}
        if not offer.get("available"):
            raise UnavailableError("No stronger model is set up on this server.")
        label = offer.get("label") or "a stronger model"
        with self.chats.locked():
            session = self.get_session(user, session_id)
            message = next((m for m in session["messages"] if m["id"] == message_id), None)
            if message is None or message["kind"] != "search":
                raise NotFoundError(f"No search {message_id} in this chat")
            data = message["data"]
            if data.get("guided"):
                raise ConflictError("You chose these ingredients yourself, so there's nothing "
                                    f"for {label} to redo.")
            if data.get("stronger"):
                raise ConflictError(f"These ingredients already come from {label}.")
            if any((m["data"] or {}).get("redo_of") == message_id for m in session["messages"]):
                raise ConflictError(f"You already asked {label} about this search: its "
                                    "answer is below.")
            current = self.jobs.get(data["job_id"])
            if current is not None and current.status == "running":
                raise ConflictError("This search is still running: wait for it to finish.")
            outcome = current.outcome if current is not None else None
            if outcome is not None and outcome.result["dish"]["source"] == "catalog":
                raise ConflictError("This dish comes from our catalog, so there's nothing for "
                                    "a stronger model to redo.")
            request, extras = data["request"], data.get("extras") or {}
            clarify = data.get("clarify") or []
            job = self.jobs.create(owner=user["id"])
            search = _message("assistant", "search",
                              f"{request_line(request, extras.get('servings'))}, "
                              f"ingredients by {label}",
                              {"job_id": job.id, "request": request, "extras": extras,
                               "clarify": clarify, "status": "running", "stronger": True,
                               "redo_of": message_id})

            def change(s: dict[str, Any]) -> None:
                s["messages"].append(search)
                s["context_start"] = len(s["messages"])   # the next message starts afresh

            session = self._update(user, session_id, change)
        self._submit(job, session_id, search["id"], request, extras, clarify, stronger=True)
        return session, job

    def _submit(self, job: JobState, session_id: str, message_id: str,
                request: dict[str, Any], extras: dict[str, Any], clarify: list[str],
                stronger: bool = False, guided_search: dict[str, Any] | None = None,
                list_id: str | None = None) -> None:
        """list_id: the list this search already saved (a rerun): kept, not saved again."""
        def on_finish(done: JobState) -> None:
            def change(s: dict[str, Any]) -> None:
                for m in s["messages"]:
                    if m["id"] == message_id and m["data"].get("job_id") == done.id:
                        m["data"]["status"] = done.status
                        m["data"]["error"] = done.error
                        m["data"]["error_kind"] = done.error_kind
                        if done.outcome:   # the dish's name only: no store data in the chat
                            m["data"]["dish"] = done.outcome.result["dish"]["name"]
                            m["data"]["list_id"] = done.outcome.list_id

            if self.chats.update(session_id, change, touch=False) is None:
                log.info("Chat %s ended before its search finished", session_id)

        # stronger is passed only when asked for, so an engine without the option (or a
        # test stand-in) keeps working
        options = {"stronger": True} if stronger else {}

        def run(progress: Any) -> Any:
            if guided_search is not None:
                outcome = self.engine.search_ingredients(request, guided_search, extras,
                                                         clarify, progress)
            else:
                outcome = self.engine.search(request, extras, clarify, progress, **options)
            # saved before the job is done, so the page never shows results without it
            if list_id:
                outcome.list_id = list_id
            else:
                self._save_list(job.owner, outcome, extras, guided_search)
            return outcome

        self.jobs.submit(job, run, on_finish)

    def _save_list(self, owner: str | None, outcome: Any, extras: dict[str, Any],
                   guided_search: dict[str, Any] | None) -> None:
        if self.lists is None or owner is None:
            return
        from app.lists import from_search
        try:
            saved = from_search(outcome.result, outcome.answer, extras,
                                "guided" if guided_search else "search", outcome.choices)
            outcome.list_id = self.lists.save(owner, saved)
        except Exception:
            log.exception("Saving the list of a search failed")
            outcome.list_error = True

    def find_products(self, user: User, saved: dict[str, Any]) -> JobState:
        """'Find products again' for a saved list: a fresh search of its items at its ZIP
        (the substitute for a substitute item; leave-out items not at all), in memory
        only, without A3. Saved preferences are applied again, as on a confirm card."""
        ingredients = []
        for item in saved["items"]:
            if item["status"] == "skip":
                continue
            name = item["substitute"] if item["status"] == "substitute" and item["substitute"] \
                else item["name"]
            if item.get("brand") and item["status"] != "substitute":
                name = choice_rules.brand_term(item["brand"], name)   # the shopper's brand
            ingredients.append({"name": name, "category": item["category"], "role": "core"})
        if not ingredients:
            raise ConflictError("Nothing on this list to look for.")
        body = {"query": saved["dish"], "zip": saved["zip"], "avoid": saved["avoid"],
                "diet": saved["diet"], "servings": saved.get("servings")}
        request, extras, _ = confirmed_request({}, body, user)
        guided_search = {"dish": saved["dish"], "cuisine": saved["cuisine"],
                         "source": "list", "ingredients": ingredients}
        job = self.jobs.create(owner=user["id"])
        self.engine.prefetch(request["zip"], [])
        self.jobs.submit(job, lambda progress: self.engine.search_ingredients(
            request, guided_search, extras, [], progress, answer=False), lambda done: None)
        return job

    # ------------------------------------------------------------ guided builds

    def _plan_reply(self, draft: dict[str, Any]) -> dict[str, Any]:
        """A build card for the dish, or today's confirm card when no plan can be made.
        Stores near the ZIP are looked up while the plan is made (a model call can take
        15-20 s), and products for the usual options as soon as it exists."""
        request = draft["request"]
        self.engine.prefetch(request["zip"], [])
        try:
            plan = self.engine.plan(request["query"], request["avoid"], request["diet"])
        except EngineError as exc:
            log.info("No guided build for %r: %s", request["query"], exc)
            notes = [*draft.get("notes", []),
                     "I couldn't plan the choices for this dish, so this searches its usual "
                     "ingredients."]
            return _message("assistant", "confirm",
                            request_line(request, draft.get("servings")),
                            {**draft, "notes": notes})
        self.engine.prefetch(request["zip"], [o.name for o in plan.usual()])
        draft = {**draft, "request": {**request, "query": plan.dish}}
        return _message("assistant", "build", f"Choosing what goes in your {plan.dish}",
                        guided.new_build(plan, draft))

    def _start_options(self, user: User, session_id: str, build_id: str,
                       items: list[str]) -> None:
        """Products for these items (and the shopper's favourite brands and family hints),
        fetched and judged in the background; the build's choices fill in when they land.
        The main store is chosen from the first batch (a build's own options)."""
        favourites = {k.lower(): v for k, v in (user.get("favourites") or {}).items()}
        session = self.chats.get(session_id)
        card = next((m for m in (session or {}).get("messages", []) if m["id"] == build_id),
                    None)
        if card is None or not items:
            return
        build = card["data"]
        request = build["draft"]["request"]
        usual = [o.name for o in guided.plan_of(build).usual()]

        def run() -> None:
            extra = choice_rules.search_terms(items, favourites)
            terms = list(dict.fromkeys(
                [*items, *(t for e in extra.values() for k, t in e.items()
                           if k in ("favourite", "hint"))]))
            try:
                found = self.engine.options(request, terms)
                error = None
            except EngineError as exc:
                found, error = None, str(exc)
            except Exception:
                log.exception("Product search for a build failed")
                found, error = None, "The store search didn't answer. Please try again."

            def change(s: dict[str, Any]) -> None:
                card = next((m for m in s["messages"] if m["id"] == build_id), None)
                if card is None:
                    return
                b = card["data"]
                if found is None:
                    if b["products"]["status"] == "loading":
                        b["products"].update(status="failed", error=error)
                    for name in items:
                        b["choices"].setdefault(name, {"pick": None, "alternatives": [],
                                                       "error": error})
                    return
                main = b["products"]["main"] or choice_rules.main_store(found, usual)
                if b["products"]["status"] != "ready":
                    b["products"].update(status="ready", main=main, stores=found["stores"],
                                         error=None)
                for name in items:
                    choice = choice_rules.choice_for(name, found, main, extra.get(name))
                    b["choices"][name] = choice
                    if name not in b["chosen"] and choice["pick"]:
                        b["chosen"][name] = choice["pick"]["key"]

            self.chats.update(session_id, change, touch=False)

        self._options_pool.submit(run)

    def _typed_into_build(self, user: User, session_id: str, text: str,
                          build_id: str) -> dict[str, Any]:
        """Text typed while a build is open: items to add or drop, or "start over"."""
        passed: list[str] = []
        zip_code = ""

        def change(s: dict[str, Any]) -> None:
            nonlocal passed, zip_code
            build = next(m for m in s["messages"] if m["id"] == build_id)
            if build["data"]["state"] != "choosing":   # finished meanwhile (Use the usual)
                raise guided.BuildError("This build was finished: name a dish to start again.")
            s["messages"].remove(build)
            s["messages"].append(_message("user", "text", text))
            if guided.is_cancel(text):
                build["data"]["state"] = "cancelled"
                s["messages"].append(build)
                s["messages"].append(_message("assistant", "text",
                                              "OK, let's start over. Which dish would you "
                                              "like to cook?"))
                s["context_start"] = len(s["messages"])
                return
            passed = guided.add_items(build["data"], text, self.screen)
            zip_code = build["data"]["draft"]["request"]["zip"]
            s["messages"].append(build)   # the build card stays the newest message

        with self.chats.locked():
            self.get_session(user, session_id)
            try:
                session = self._update(user, session_id, change)
            except guided.BuildError as exc:
                raise ConflictError(str(exc)) from exc
        if passed:
            self.engine.prefetch(zip_code, passed)
            self._start_options(user, session_id, build_id, passed)
        return session

    def build_action(self, user: User, session_id: str, message_id: str, action: str,
                     picks: list[str] | None = None, text: str | None = None,
                     name: str | None = None, choice: str | None = None,
                     key: str | None = None) -> tuple[dict[str, Any], JobState | None]:
        """A move on the newest build card: next, back, finish (to the summary card),
        usual (search the usual for the rest now), add (items), resolve (a clash: swap,
        anyway or remove), cancel."""
        to_fetch: list[str] = []
        launch: tuple[dict[str, Any], dict[str, Any]] | None = None
        with self.chats.locked():
            session = self.get_session(user, session_id)
            card = next((m for m in session["messages"] if m["id"] == message_id), None)
            if card is None or card["kind"] != "build":
                raise NotFoundError(f"No build {message_id} in this chat")
            if card is not session["messages"][-1] or card["data"]["state"] != "choosing":
                raise ConflictError("This build was replaced or finished: use the newest card.")
            build = card["data"]
            zip_code = build["draft"]["request"]["zip"]
            try:
                if action in ("next", "back", "finish"):
                    if action != "back" and _would_finish(build, action):
                        _no_pending(build)
                    to_fetch = guided.move(build, action, picks)
                elif action == "usual":
                    guided.set_picks(build, picks)
                    _no_pending(build)
                    launch = (build, card)
                elif action == "add":
                    to_fetch = guided.add_items(build, text or "", self.screen)
                elif action == "resolve":
                    to_fetch = guided.resolve(build, name or "", choice or "")
                elif action == "choose":
                    guided.choose(build, name or "", key or "")
                elif action == "cancel":
                    build["state"] = "cancelled"
                else:
                    raise guided.BuildError(f"unknown action {action!r}")
            except guided.BuildError as exc:
                raise ConflictError(str(exc)) from exc
            if launch is None:
                summary = (self._summary_card(build, user, message_id)
                           if build["state"] == "done" else None)

                def change(s: dict[str, Any]) -> None:
                    for m in s["messages"]:
                        if m["id"] == message_id:
                            m["data"] = build
                    if summary is not None:
                        s["messages"].append(summary)
                    if build["state"] == "cancelled":
                        s["messages"].append(_message(
                            "assistant", "text",
                            "OK, let's start over. Which dish would you like to cook?"))
                        s["context_start"] = len(s["messages"])

                session = self._update(user, session_id, change)
        if launch is not None:
            return self._search_usual(user, session_id, message_id, *launch)
        if to_fetch:
            self.engine.prefetch(zip_code, to_fetch)
            new = [n for n in to_fetch if n not in build["choices"]]
            if new:
                self._start_options(user, session_id, message_id, new)
        return session, None

    def add_more(self, user: User, session_id: str, summary_id: str, text: str,
                 typed: bool = False) -> dict[str, Any]:
        """'Anything else to add?' at a build's summary: the build opens again at its last
        step with these items added (searched, checked against allergies, with products),
        and the summary goes; Review makes a new one."""
        passed: list[str] = []
        zip_code = ""
        build_id = ""

        def change(s: dict[str, Any]) -> None:
            nonlocal passed, zip_code, build_id
            summary = next(m for m in s["messages"] if m["id"] == summary_id)
            build_id = summary["data"]["build_id"]
            build = next(m for m in s["messages"] if m["id"] == build_id)
            s["messages"].remove(summary)
            s["messages"].remove(build)
            if typed:
                s["messages"].append(_message("user", "text", text))
            guided.reopen(build["data"])
            passed = guided.add_items(build["data"], text, self.screen)
            zip_code = build["data"]["draft"]["request"]["zip"]
            s["messages"].append(build)   # the build card is the newest message again

        with self.chats.locked():
            session = self.get_session(user, session_id)
            if _open_summary(session) is None or session["messages"][-1]["id"] != summary_id:
                raise ConflictError("This list was replaced or already searched: use the "
                                    "newest card.")
            try:
                session = self._update(user, session_id, change)
            except guided.BuildError as exc:
                raise ConflictError(str(exc)) from exc
        if passed:
            self.engine.prefetch(zip_code, passed)
            self._start_options(user, session_id, build_id, passed)
        return session

    def _summary_card(self, build: dict[str, Any], user: User,
                      build_id: str) -> dict[str, Any]:
        """The confirm card at the end of a build: the request (editable as always) and the
        chosen products, searched as they are. It asks whether to add anything (add_more)."""
        draft = build["draft"]
        data = {**draft, "guided": self._guided_search(build, user, guided.chosen(build)),
                "build_id": build_id}
        line = request_line(draft["request"], draft.get("servings"))
        return _message("assistant", "confirm", line, data)

    def _guided_search(self, build: dict[str, Any], user: User,
                       ingredients: list[dict[str, Any]]) -> dict[str, Any]:
        """What a guided search needs: the ingredients, the chosen product of each, the
        steps check's inputs, and favourite brands to keep (new) or ask about (changed)."""
        plan = guided.plan_of(build)
        names = [i["name"] for i in ingredients]
        picked = guided.chosen_views(build, names)
        favourites = {k.lower(): v for k, v in (user.get("favourites") or {}).items()}
        new: dict[str, str] = {}
        ask: list[dict[str, str]] = []
        produce = {i["name"] for i in ingredients if i.get("category") == "produce"}
        for name, view in picked.items():
            brand = (view.get("brand") or "").strip()
            # loose produce has no real brand ("Fresh Tomatoes"): nothing to remember
            if not brand or name in produce or brand.lower().startswith("fresh"):
                continue
            old = favourites.get(name.lower())
            if old is None:
                new[name.lower()] = brand
            elif old.lower() != brand.lower():
                ask.append({"item": name, "old": old, "new": brand})
        # an item picked from a brand search is searched the same way at the end
        ingredients = [{**i, "term": picked[i["name"]]["term"]}
                       if i["name"] in picked and picked[i["name"]].get("term")
                       and picked[i["name"]]["term"] != i["name"] else i
                       for i in ingredients]
        return {"dish": plan.dish, "cuisine": plan.cuisine, "source": plan.source,
                "ingredients": ingredients, "choices": picked,
                "added": [a["name"] for a in build["additions"]
                          if a["status"] in ("ok", "anyway")],
                "replacements": guided.replacements(build), "offered": guided.offered(build),
                "favourites_new": new, "favourites_ask": ask,
                "main_store": build["products"].get("main")}

    def _search_usual(self, user: User, session_id: str, message_id: str,
                      build: dict[str, Any], card: dict[str, Any]
                      ) -> tuple[dict[str, Any], JobState]:
        """'Use the usual': search now. An untouched catalog dish is today's search (its
        whole profile; the stronger model can redo it); anything else searches what was
        chosen so far plus the usual options of the steps not reached."""
        plan = guided.plan_of(build)
        draft = build["draft"]
        untouched = (plan.source == "catalog" and not build["additions"] and all(
            build["picks"].get(s.id, []) == [o.name for o in s.options if o.usual]
            for s in plan.steps))
        body = {**draft["request"], "servings": draft.get("servings")}
        request, extras, clarify = confirmed_request(draft, body, user)
        guided_search = None
        if not untouched:
            guided_search = self._guided_search(
                build, user, guided.chosen(build, usual_for_the_rest=True))
            # "Use the usual" has no summary card to ask on: only new favourites are kept
        build["state"] = "searched"
        job = self.jobs.create(owner=user["id"])
        search = _message("assistant", "search", request_line(request, extras.get("servings")),
                          {"job_id": job.id, "request": request, "extras": extras,
                           "clarify": clarify, "status": "running", "guided": guided_search})

        def change(s: dict[str, Any]) -> None:
            for m in s["messages"]:
                if m["id"] == message_id:
                    m["data"] = build
            s["messages"].append(search)
            s["context_start"] = len(s["messages"])
            s["title"] = request["query"].capitalize()

        session = self._update(user, session_id, change)
        self._submit(job, session_id, search["id"], request, extras, clarify,
                     guided_search=guided_search)
        return session, job

    def job(self, user: User, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None or job.owner != user["id"]:
            raise NotFoundError("These results have expired. Store data isn't kept, so "
                                "search again.")
        return job.view()


def _open_build(session: dict[str, Any]) -> dict[str, Any] | None:
    """The newest message, when it's a build the shopper is still choosing in."""
    last = session["messages"][-1] if session["messages"] else None
    if last and last["kind"] == "build" and last["data"]["state"] == "choosing":
        return last
    return None


def _would_finish(build: dict[str, Any], action: str) -> bool:
    return action == "finish" or build["step"] + 1 >= len(build["plan"]["steps"])


def _no_pending(build: dict[str, Any]) -> None:
    pending = guided.pending_clashes(build)
    if pending:
        raise guided.BuildError(f"First decide about {' and '.join(pending)}: use the swap, "
                                "search it anyway, or remove it.")


def _open_summary(session: dict[str, Any]) -> dict[str, Any] | None:
    """The newest message, when it's a build's summary not searched yet."""
    last = session["messages"][-1] if session["messages"] else None
    if (last and last["kind"] == "confirm" and (last["data"] or {}).get("build_id")
            and not last["data"].get("submitted")):
        return last
    return None


def _confirmed_guided(guided_search: dict[str, Any], built: dict[str, Any],
                      request: dict[str, Any]) -> dict[str, Any]:
    """A build's summary, as confirmed. Its dish and ZIP stay those of the build (its
    products came from that ZIP's stores; start over to change them). If allergies or the
    diet changed on the card, the picks, brand searches and favourites go: the products
    are then searched afresh and judged against the new restrictions."""
    request["query"], request["zip"] = built["query"], built["zip"]
    if set(request["avoid"]) == set(built["avoid"]) and request["diet"] == built["diet"]:
        return guided_search
    return {**guided_search, "choices": {}, "favourites_new": {}, "favourites_ask": [],
            "ingredients": [{k: v for k, v in i.items() if k != "term"}
                            for i in guided_search["ingredients"]]}
