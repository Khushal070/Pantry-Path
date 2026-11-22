"""Accounts and logins, kept as JSON files like everything else.

    data/users/<id>.json      username, password hash, name and food preferences
    data/logins/<hash>.json   one logged-in browser: whose it is and when it expires

Passwords are hashed with scrypt (Python's hashlib, no extra package) with a random salt
each, checked in constant time, and never logged or sent back. A login is a random token
in an HttpOnly cookie: page scripts can't read it, and SameSite=Lax keeps other sites from
sending it with their requests. The server keeps only the token's SHA-256, so the files in
data/logins/ can't be used to log in. Five failed logins for one username within ten
minutes block that username's logins until the window passes.
"""

import base64
import hashlib
import hmac
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.schemas import Preferences, RegisterIn
from app.store import JsonStore

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
LOGIN_LIMIT, LOGIN_WINDOW_S = 5, 600


class AuthError(Exception):
    """Wrong username or password, or no valid login (HTTP 401)."""


class WrongPasswordError(Exception):
    """The password given to confirm an account change is wrong (HTTP 403)."""


class ThrottledError(Exception):
    """Too many failed logins for this username (HTTP 429)."""


class UsernameTakenError(Exception):
    """HTTP 409."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=32)
    return "$".join(["scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P), _b64(salt),
                     _b64(digest)])


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n),
                                r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Checked against for unknown usernames, so they take as long as wrong passwords
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def token_key(token: str) -> str:
    """The name a login is stored under: the token's SHA-256, never the token itself."""
    return hashlib.sha256(token.encode()).hexdigest()


def public(user: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in user.items() if k != "password_hash"}


def _now() -> datetime:
    return datetime.now(UTC)


class Throttle:
    """Failed logins per username, in memory."""

    def __init__(self, limit: int = LOGIN_LIMIT, window_s: float = LOGIN_WINDOW_S):
        self.limit, self.window_s = limit, window_s
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.window_s
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        self._failures[key] = recent
        return recent

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            self._recent(key).append(time.monotonic())

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class Accounts:
    def __init__(self, store: JsonStore, login_days: int = 30):
        self.store = store
        self.login_ttl = timedelta(days=login_days)
        self.throttle = Throttle()

    def find(self, username: str) -> dict[str, Any] | None:
        return next((u for u in self.store.all("users") if u["username"] == username), None)

    def register(self, body: RegisterIn) -> dict[str, Any]:
        password_hash = hash_password(body.password)   # slow on purpose: outside the lock
        now = _now().isoformat()
        with self.store.locked():
            if self.find(body.username):
                raise UsernameTakenError(f"The username {body.username} is taken.")
            user = {"id": "u_" + secrets.token_hex(6), "username": body.username,
                    "password_hash": password_hash, "created_at": now, "updated_at": now,
                    **Preferences(name=body.name).model_dump(mode="json")}
            self.store.put("users", user["id"], user)
        return user

    def login(self, username: str, password: str) -> tuple[dict[str, Any], str]:
        if self.throttle.blocked(username):
            raise ThrottledError("Too many tries. Wait a few minutes, then try again.")
        user = self.find(username)
        matches = verify_password(password, user["password_hash"] if user else _DUMMY_HASH)
        if user is None or not matches:
            self.throttle.fail(username)
            raise AuthError("Wrong username or password.")
        self.throttle.clear(username)
        return user, self.start_login(user["id"])

    def start_login(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        key, now = token_key(token), _now()
        self.store.put("logins", key, {
            "id": key, "user_id": user_id, "created_at": now.isoformat(),
            "expires_at": (now + self.login_ttl).isoformat()})
        return token

    def user_for(self, token: str | None) -> dict[str, Any] | None:
        """The user a login token belongs to, or None (no such login, expired, user gone)."""
        if not token:
            return None
        key = token_key(token)
        login = self.store.get("logins", key)
        if login is None:
            return None
        user = self.store.get("users", login["user_id"])
        if user is None or datetime.fromisoformat(login["expires_at"]) <= _now():
            self.store.delete("logins", key)
            return None
        return user

    def logout(self, token: str | None) -> None:
        if token:
            self.store.delete("logins", token_key(token))

    def update_preferences(self, user_id: str, prefs: Preferences) -> dict[str, Any]:
        def change(user: dict[str, Any]) -> None:
            user.update(prefs.model_dump(mode="json"), updated_at=_now().isoformat())

        user = self.store.update("users", user_id, change)
        if user is None:
            raise AuthError("Please log in.")
        return user

    def _check_password(self, user_id: str, password: str) -> dict[str, Any]:
        user = self.store.get("users", user_id)
        if user is None:
            raise AuthError("Please log in.")
        if not verify_password(password, user["password_hash"]):
            raise WrongPasswordError("That password isn't right.")
        return user

    def change_password(self, user_id: str, current: str, new: str,
                        keep_token: str | None = None) -> None:
        """New password; every other login of this user ends (this browser stays in)."""
        self._check_password(user_id, current)
        password_hash = hash_password(new)
        self.store.update("users", user_id, lambda u: u.update(
            password_hash=password_hash, updated_at=_now().isoformat()))
        self._end_logins(user_id, keep=token_key(keep_token) if keep_token else None)

    def delete(self, user_id: str, password: str) -> None:
        """The account and every login of it. The caller deletes the user's chats."""
        self._check_password(user_id, password)
        self.store.delete("users", user_id)
        self._end_logins(user_id)

    def _end_logins(self, user_id: str, keep: str | None = None) -> None:
        with self.store.locked():
            for login in self.store.all("logins"):
                if login.get("user_id") == user_id and login.get("id") != keep:
                    self.store.delete("logins", login["id"])

    def purge_expired(self) -> int:
        """Delete logins that have expired; run at startup."""
        now, removed = _now(), 0
        with self.store.locked():
            for login in self.store.all("logins"):
                if "id" in login and datetime.fromisoformat(login["expires_at"]) <= now:
                    self.store.delete("logins", login["id"])
                    removed += 1
        return removed
