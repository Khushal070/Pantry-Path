"""Pantry Path API. Run from backend/:

    uvicorn app.main:app --reload            # http://localhost:8000, docs at /docs

PANTRYPATH_MODE=demo (default) runs offline with synthetic stores; PANTRYPATH_MODE=live
uses the champion models and live store data (backend/README.md). Accounts and
preferences are in Postgres (db.py); chats and search results live in memory, so keep one
worker (and one Cloud Run instance).

Everything under /api except config, health and the /api/auth routes needs a login: the
pantrypath_login cookie that register and login set (auth.py).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import sqlalchemy.exc
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.auth import (
    Accounts,
    AuthError,
    ProfileUnreadableError,
    ThrottledError,
    UsernameTakenError,
    WrongPasswordError,
    public,
)
from app.chat import (
    ChatService,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    UnavailableError,
)
from app.chat_memory import ChatMemory
from app.crypto import ProfileCipher
from app.db import Database
from app.engines import Engine, make_engine
from app.jobs import Jobs
from app.schemas import (
    ChatReply,
    DeleteAccountIn,
    Job,
    LoginIn,
    MessageIn,
    PasswordChangeIn,
    Preferences,
    RegisterIn,
    SearchIn,
    Session,
    SessionSummary,
    User,
)
from app.settings import Settings, load_settings
from app.vocab import ALLERGEN_LABELS, ALLERGENS, DIET_LABELS, DIETS, NEED_LABELS, NEEDS

log = logging.getLogger("pantrypath.api")
COOKIE = "pantrypath_login"


def current_user(request: Request) -> dict[str, Any]:
    """The logged-in user, from the login cookie; 401 without a valid login."""
    user = request.app.state.accounts.user_for(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please log in.")
    return user


Me = Annotated[dict[str, Any], Depends(current_user)]


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        cipher = ProfileCipher.from_setting(settings.profile_keys)   # stops on a bad key
        db = Database(settings.db_auth_url, settings.db_profile_url)
        db.check()                                       # and on a wrong URL or password
        jobs = Jobs(settings.search_workers, settings.result_ttl_minutes)
        app.state.accounts = Accounts(db, cipher, settings.login_days)
        app.state.chat = ChatService(ChatMemory(settings.chat_idle_minutes),
                                     engine or make_engine(settings), jobs)
        expired = app.state.accounts.purge_expired()
        log.info("Pantry Path API: %s mode, accounts in Postgres, profile key %s "
                 "(%d expired logins removed)", app.state.chat.engine.mode, cipher.current,
                 expired)
        yield
        jobs.shutdown()
        db.dispose()

    app = FastAPI(title="Pantry Path API", version="0.2.0", lifespan=lifespan)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                           allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    def chat(request: Request) -> ChatService:
        return request.app.state.chat

    def accounts(request: Request) -> Accounts:
        return request.app.state.accounts

    def set_login(response: Response, token: str) -> None:
        response.set_cookie(COOKIE, token, max_age=settings.login_days * 86400, path="/",
                            httponly=True, samesite="lax", secure=settings.secure_cookies)

    def clear_login(response: Response) -> None:
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax",
                               secure=settings.secure_cookies)

    def error(code: int):
        def handler(_: Request, exc: Exception) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=code)
        return handler

    for exc_type, code in ((NotFoundError, 404), (ConflictError, 409),
                           (InvalidRequestError, 422), (AuthError, 401),
                           (WrongPasswordError, 403), (ThrottledError, 429),
                           (UsernameTakenError, 409), (UnavailableError, 503)):
        app.add_exception_handler(exc_type, error(code))

    @app.exception_handler(ProfileUnreadableError)
    def _profile_unreadable(_: Request, exc: ProfileUnreadableError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=500)

    @app.exception_handler(sqlalchemy.exc.OperationalError)
    def _db_down(_: Request, exc: sqlalchemy.exc.OperationalError) -> JSONResponse:
        log.error("The database can't be reached: %s", exc.orig)
        return JSONResponse({"detail": "Accounts are unavailable right now. Please try "
                                       "again in a minute."}, status_code=503)

    @app.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        return {"status": "ok", "mode": chat(request).engine.mode}

    @app.get("/api/config")
    def config(request: Request) -> dict[str, Any]:
        """What the app needs to draw its forms, and which engine answers."""
        info = chat(request).engine.info()
        return {
            "mode": chat(request).engine.mode,
            "engine": info,
            # "Not right? Ask a stronger model" (stronger_model.py): off without a key
            "stronger_model": info.get("stronger_model") or {"available": False, "label": None},
            "allergens": [{"id": a, "label": ALLERGEN_LABELS[a]} for a in ALLERGENS],
            "diets": [{"id": d, "label": DIET_LABELS[d]} for d in DIETS],
            "needs": [{"id": n, "label": NEED_LABELS[n]} for n in NEEDS],
            "result_ttl_minutes": settings.result_ttl_minutes,
        }

    # ------------------------------------------------------------ accounts

    @app.post("/api/auth/register", response_model=User, status_code=status.HTTP_201_CREATED)
    def register(body: RegisterIn, request: Request, response: Response) -> dict[str, Any]:
        """Create an account and log it in."""
        user = accounts(request).register(body)
        set_login(response, accounts(request).start_login(user["id"]))
        return public(user)

    @app.post("/api/auth/login", response_model=User)
    def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
        user, token = accounts(request).login(body.username, body.password)
        set_login(response, token)
        return public(user)

    @app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(request: Request, response: Response) -> None:
        """Ends the login and the user's open chats (chats are never saved)."""
        token = request.cookies.get(COOKIE)
        user = accounts(request).user_for(token)
        if user is not None:
            chat(request).delete_all(user["id"])
        accounts(request).logout(token)
        clear_login(response)

    @app.get("/api/me", response_model=User)
    def me(user: Me) -> dict[str, Any]:
        return public(user)

    @app.put("/api/me/preferences", response_model=User)
    def save_preferences(body: Preferences, user: Me, request: Request) -> dict[str, Any]:
        return public(accounts(request).update_preferences(user["id"], body))

    @app.post("/api/me/password", status_code=status.HTTP_204_NO_CONTENT)
    def change_password(body: PasswordChangeIn, user: Me, request: Request) -> None:
        """A new password. Every other browser logged in to this account is logged out."""
        accounts(request).change_password(user["id"], body.current_password,
                                          body.new_password,
                                          keep_token=request.cookies.get(COOKIE))

    @app.post("/api/me/delete", status_code=status.HTTP_204_NO_CONTENT)
    def delete_account(body: DeleteAccountIn, user: Me, request: Request,
                       response: Response) -> None:
        """The account, its logins and its chats. Asks for the password again."""
        accounts(request).delete(user["id"], body.password)
        chat(request).delete_all(user["id"])
        clear_login(response)

    # ------------------------------------------------------------ chats

    @app.get("/api/sessions", response_model=list[SessionSummary])
    def list_sessions(user: Me, request: Request) -> list[dict[str, Any]]:
        return chat(request).list_sessions(user)

    @app.post("/api/sessions", response_model=Session, status_code=status.HTTP_201_CREATED)
    def create_session(user: Me, request: Request) -> dict[str, Any]:
        return chat(request).create_session(user)

    @app.get("/api/sessions/{session_id}", response_model=Session)
    def get_session(session_id: str, user: Me, request: Request) -> dict[str, Any]:
        return chat(request).get_session(user, session_id)

    @app.delete("/api/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_session(session_id: str, user: Me, request: Request) -> None:
        chat(request).delete_session(user, session_id)

    @app.post("/api/sessions/{session_id}/messages", response_model=ChatReply)
    def post_message(session_id: str, body: MessageIn, user: Me,
                     request: Request) -> dict[str, Any]:
        """The shopper's message and the assistant's reply. Blocking: A1 can take seconds
        (minutes on Cloud Run CPUs), so FastAPI runs this in its thread pool."""
        return {"session": chat(request).post_message(user, session_id, body.text)}

    @app.post("/api/sessions/{session_id}/search", response_model=ChatReply)
    def search(session_id: str, body: SearchIn, user: Me, request: Request) -> dict[str, Any]:
        session, job = chat(request).start_search(user, session_id, body)
        return {"session": session, "job": job.view()}

    @app.post("/api/sessions/{session_id}/messages/{message_id}/rerun",
              response_model=ChatReply)
    def rerun(session_id: str, message_id: str, user: Me, request: Request) -> dict[str, Any]:
        session, job = chat(request).rerun_search(user, session_id, message_id)
        return {"session": session, "job": job.view()}

    @app.post("/api/sessions/{session_id}/messages/{message_id}/rerun-stronger",
              response_model=ChatReply)
    def rerun_stronger(session_id: str, message_id: str, user: Me,
                       request: Request) -> dict[str, Any]:
        """Search again with the stronger model working out the ingredients: a new search
        message. 409 for a catalog dish or one already redone, 503 without a stronger
        model."""
        session, job = chat(request).ask_stronger(user, session_id, message_id)
        return {"session": session, "job": job.view()}

    @app.get("/api/jobs/{job_id}", response_model=Job)
    def get_job(job_id: str, user: Me, request: Request) -> dict[str, Any]:
        return chat(request).job(user, job_id)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"],
                   include_in_schema=False)
    def no_route(path: str) -> None:
        raise HTTPException(404, f"No API route /api/{path}")

    # The built React app (frontend/dist), when there is one: one server for a demo
    if settings.frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="app")
    return app


app = create_app()
