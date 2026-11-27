"""Accounts, logins and food preferences, in Postgres (db.py).

    auth.accounts       username and password hash               (as pp_auth)
    auth.logins         one logged-in browser: whose, and until   (as pp_auth)
    profile.profiles    name and food preferences, encrypted      (as pp_profile, crypto.py)

Passwords are hashed with scrypt (Python's hashlib, no extra package) with a random salt
each, checked in constant time, and never logged or sent back. A login is a random token
in an HttpOnly cookie: page scripts can't read it, and SameSite=Lax keeps other sites from
sending it with their requests. The database keeps only the token's SHA-256, so its rows
can't be used to log in. Five failed logins for one username within ten minutes block that
username's logins until the window passes (counted in memory).

The profile is one encrypted value, {"format": 1, "prefs": {...}}, so fields can be added
to it (such as where a saved item came from) without changing the table.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

from app.crypto import DecryptError, ProfileCipher
from app.db import Database
from app.schemas import Preferences, RegisterIn

log = logging.getLogger(__name__)

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
LOGIN_LIMIT, LOGIN_WINDOW_S = 5, 600
PROFILE_FORMAT = 1


class AuthError(Exception):
    """Wrong username or password, or no valid login (HTTP 401)."""


class WrongPasswordError(Exception):
    """The password given to confirm an account change is wrong (HTTP 403)."""


class ThrottledError(Exception):
    """Too many failed logins for this username (HTTP 429)."""


class UsernameTakenError(Exception):
    """HTTP 409."""


class ProfileUnreadableError(Exception):
    """A saved profile can't be decrypted (wrong or missing key): HTTP 500."""


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
    """The key a login is stored under: the token's SHA-256, never the token itself."""
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


ACCOUNT_COLUMNS = "id, username, password_hash, created_at, updated_at"


class Accounts:
    def __init__(self, db: Database, cipher: ProfileCipher, login_days: int = 30):
        self.db, self.cipher = db, cipher
        self.login_ttl = timedelta(days=login_days)
        self.throttle = Throttle()

    # ------------------------------------------------------------ reading

    def _account(self, where: str, **params: Any) -> sa.Row | None:
        with self.db.auth.connect() as conn:
            return conn.execute(sa.text(f"SELECT {ACCOUNT_COLUMNS} FROM auth.accounts "
                                        f"WHERE {where}"), params).first()

    def _user(self, account: sa.Row) -> dict[str, Any]:
        """The account and its decrypted preferences, as the rest of the app uses them."""
        user_id = str(account.id)
        with self.db.profile.connect() as conn:
            profile = conn.execute(sa.text(
                "SELECT prefs_encrypted, key_version, updated_at FROM profile.profiles "
                "WHERE user_id = :id"), {"id": account.id}).first()
        if profile is None:   # registering failed halfway: start from blank preferences
            log.warning("Account %s has no profile; using blank preferences", user_id)
            prefs, updated = Preferences(name=account.username).model_dump(mode="json"), None
        else:
            try:
                saved = self.cipher.decrypt(user_id, profile.prefs_encrypted,
                                            profile.key_version)
            except DecryptError as exc:
                log.error("The profile of account %s can't be decrypted: %s", user_id, exc)
                raise ProfileUnreadableError("Your saved preferences can't be read.") from exc
            prefs, updated = saved["prefs"], profile.updated_at
        return {"id": user_id, "username": account.username,
                "password_hash": account.password_hash, "created_at": account.created_at,
                "updated_at": max(t for t in (account.updated_at, updated) if t), **prefs}

    def find(self, username: str) -> dict[str, Any] | None:
        account = self._account("username = :u", u=username)
        return self._user(account) if account else None

    def get(self, user_id: str) -> dict[str, Any] | None:
        account = self._account("id = :id", id=user_id)
        return self._user(account) if account else None

    # ------------------------------------------------------------ writing

    def _save_profile(self, user_id: str, prefs: Preferences) -> None:
        blob, version = self.cipher.encrypt(
            user_id, {"format": PROFILE_FORMAT, "prefs": prefs.model_dump(mode="json")})
        with self.db.profile.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO profile.profiles (user_id, prefs_encrypted, key_version) "
                "VALUES (:id, :blob, :version) "
                "ON CONFLICT (user_id) DO UPDATE SET prefs_encrypted = excluded.prefs_encrypted,"
                " key_version = excluded.key_version, updated_at = now()"),
                {"id": user_id, "blob": blob, "version": version})

    def register(self, body: RegisterIn) -> dict[str, Any]:
        password_hash = hash_password(body.password)   # slow on purpose
        try:
            with self.db.auth.begin() as conn:
                user_id = str(conn.execute(sa.text(
                    "INSERT INTO auth.accounts (username, password_hash) VALUES (:u, :h) "
                    "RETURNING id"), {"u": body.username, "h": password_hash}).scalar_one())
        except IntegrityError as exc:
            if isinstance(exc.orig, UniqueViolation):
                raise UsernameTakenError(f"The username {body.username} is taken.") from exc
            raise
        try:
            self._save_profile(user_id, Preferences(name=body.name))
        except Exception:
            # Two database users, two transactions: undo the account by hand
            with self.db.auth.begin() as conn:
                conn.execute(sa.text("DELETE FROM auth.accounts WHERE id = :id"),
                             {"id": user_id})
            raise
        return self.get(user_id)

    def login(self, username: str, password: str) -> tuple[dict[str, Any], str]:
        if self.throttle.blocked(username):
            raise ThrottledError("Too many tries. Wait a few minutes, then try again.")
        account = self._account("username = :u", u=username)
        matches = verify_password(password,
                                  account.password_hash if account else _DUMMY_HASH)
        if account is None or not matches:
            self.throttle.fail(username)
            raise AuthError("Wrong username or password.")
        self.throttle.clear(username)
        return self._user(account), self.start_login(str(account.id))

    def start_login(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.db.auth.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO auth.logins (token_hash, user_id, expires_at) "
                "VALUES (:k, :u, :e)"),
                {"k": token_key(token), "u": user_id, "e": _now() + self.login_ttl})
        return token

    def user_for(self, token: str | None) -> dict[str, Any] | None:
        """The user a login token belongs to, or None (no such login, or expired)."""
        if not token:
            return None
        key = token_key(token)
        with self.db.auth.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT l.expires_at, a.id, a.username, a.password_hash, a.created_at, "
                "a.updated_at FROM auth.logins l JOIN auth.accounts a ON a.id = l.user_id "
                "WHERE l.token_hash = :k"), {"k": key}).first()
        if row is None:
            return None
        if row.expires_at <= _now():
            self.logout(token)
            return None
        return self._user(row)

    def logout(self, token: str | None) -> None:
        if token:
            with self.db.auth.begin() as conn:
                conn.execute(sa.text("DELETE FROM auth.logins WHERE token_hash = :k"),
                             {"k": token_key(token)})

    def update_preferences(self, user_id: str, prefs: Preferences) -> dict[str, Any]:
        try:
            self._save_profile(user_id, prefs)
        except IntegrityError as exc:   # the account was deleted meanwhile
            raise AuthError("Please log in.") from exc
        user = self.get(user_id)
        if user is None:
            raise AuthError("Please log in.")
        return user

    def _check_password(self, user_id: str, password: str) -> None:
        account = self._account("id = :id", id=user_id)
        if account is None:
            raise AuthError("Please log in.")
        if not verify_password(password, account.password_hash):
            raise WrongPasswordError("That password isn't right.")

    def change_password(self, user_id: str, current: str, new: str,
                        keep_token: str | None = None) -> None:
        """New password; every other login of this user ends (this browser stays in)."""
        self._check_password(user_id, current)
        password_hash = hash_password(new)
        with self.db.auth.begin() as conn:
            conn.execute(sa.text("UPDATE auth.accounts SET password_hash = :h, "
                                 "updated_at = now() WHERE id = :id"),
                         {"h": password_hash, "id": user_id})
            conn.execute(sa.text("DELETE FROM auth.logins WHERE user_id = :id "
                                 "AND token_hash IS DISTINCT FROM :keep"),
                         {"id": user_id, "keep": token_key(keep_token) if keep_token else None})

    def delete(self, user_id: str, password: str) -> None:
        """The account; Postgres deletes its logins and profile with it (ON DELETE CASCADE).
        The caller ends the user's chats."""
        self._check_password(user_id, password)
        with self.db.auth.begin() as conn:
            conn.execute(sa.text("DELETE FROM auth.accounts WHERE id = :id"), {"id": user_id})

    def purge_expired(self) -> int:
        """Delete logins that have expired; run at startup."""
        with self.db.auth.begin() as conn:
            return conn.execute(sa.text("DELETE FROM auth.logins WHERE expires_at <= :now"),
                                {"now": _now()}).rowcount
