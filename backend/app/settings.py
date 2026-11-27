"""Backend settings: environment variables, with the repo-root .env as a fallback.

PANTRYPATH_MODE            demo (default) | live
PANTRYPATH_DB_AUTH_URL     Postgres as pp_auth: accounts and logins (required)
PANTRYPATH_DB_PROFILE_URL  Postgres as pp_profile: food preferences (required)
PANTRYPATH_PROFILE_KEYS    the keys that encrypt preferences, newest first (required;
                           app/crypto.py)
PANTRYPATH_GUIDED          true (default): a dish is built step by step (guided.py) before
                           searching; false: straight to the confirm card, as before
PANTRYPATH_CHAT_IDLE_MIN   minutes a chat stays in memory after its last message
                           (default 20); chats are never saved
PANTRYPATH_RESULT_TTL_MIN  how long search results stay in memory (default 60)
PANTRYPATH_SEARCH_WORKERS  searches that may run at once (default 2)
PANTRYPATH_DEMO_DELAY_S    demo mode: seconds each search stage pretends to take (default 1.2)
PANTRYPATH_LOGIN_DAYS      how long a login lasts before the user must log in again (default 30)
PANTRYPATH_SECURE_COOKIES  true when the app is served over HTTPS (the login cookie is then
                           sent over HTTPS only); leave false for http://localhost
PANTRYPATH_CORS_ORIGINS    extra allowed origins, comma-separated (the Vite dev server
                           proxies /api, so this is only for other setups)
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_HOME = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_HOME.parent
MODES = ("demo", "live")


@dataclass(frozen=True)
class Settings:
    mode: str = "demo"
    db_auth_url: str | None = None
    db_profile_url: str | None = None
    profile_keys: str | None = None
    chat_idle_minutes: int = 20
    guided_builds: bool = True
    result_ttl_minutes: int = 60
    search_workers: int = 2
    demo_delay_s: float = 1.2
    login_days: int = 30
    secure_cookies: bool = False
    cors_origins: tuple[str, ...] = ()
    frontend_dist: Path = REPO_ROOT / "frontend" / "dist"


def _flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def load_settings() -> Settings:
    load_dotenv(os.environ.get("PANTRYPATH_ENV_FILE") or REPO_ROOT / ".env", override=False)
    mode = (os.environ.get("PANTRYPATH_MODE") or "demo").strip().lower()
    if mode not in MODES:
        raise ValueError(f"PANTRYPATH_MODE must be one of {MODES}, not {mode!r}")
    origins = os.environ.get("PANTRYPATH_CORS_ORIGINS", "")
    return Settings(
        mode=mode,
        db_auth_url=os.environ.get("PANTRYPATH_DB_AUTH_URL") or None,
        db_profile_url=os.environ.get("PANTRYPATH_DB_PROFILE_URL") or None,
        profile_keys=os.environ.get("PANTRYPATH_PROFILE_KEYS") or None,
        chat_idle_minutes=int(os.environ.get("PANTRYPATH_CHAT_IDLE_MIN") or 20),
        guided_builds=(os.environ.get("PANTRYPATH_GUIDED") or "true").strip().lower()
        not in ("0", "false", "no", "off"),
        result_ttl_minutes=int(os.environ.get("PANTRYPATH_RESULT_TTL_MIN") or 60),
        search_workers=int(os.environ.get("PANTRYPATH_SEARCH_WORKERS") or 2),
        demo_delay_s=float(os.environ.get("PANTRYPATH_DEMO_DELAY_S") or 1.2),
        login_days=int(os.environ.get("PANTRYPATH_LOGIN_DAYS") or 30),
        secure_cookies=_flag("PANTRYPATH_SECURE_COOKIES"),
        cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
    )
