"""Encrypting a user's food preferences before they reach the database.

AES-256-GCM (the `cryptography` package): it hides the data and detects any change to it,
so a changed byte makes decrypting fail instead of returning something else. Each value is
stored as nonce (12 random bytes) + ciphertext, next to the version of the key that made
it. The user's id and that version are bound in as associated data, so a value copied onto
another account, or relabelled with another key version, doesn't decrypt.

Keys come from PANTRYPATH_PROFILE_KEYS, newest first:

    PANTRYPATH_PROFILE_KEYS=v2:<base64 of 32 bytes>,v1:<base64 of 32 bytes>

The first key encrypts; every key listed can still decrypt. To change the key, put a new
one first and keep the old one until every profile has been saved again. A new key:

    python -c "from app.crypto import new_key; print(new_key())"

In production the value comes from Google Secret Manager, never from a file in the repo.
"""

import base64
import binascii
import json
import os
import re
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12
VERSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


class KeyConfigError(ValueError):
    """PANTRYPATH_PROFILE_KEYS is missing or malformed (the backend won't start)."""


class DecryptError(Exception):
    """A stored value can't be decrypted: wrong user, unknown key, or changed bytes."""


def new_key() -> str:
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()


def _aad(user_id: str, version: str) -> bytes:
    return f"pantrypath/profile/{user_id}/{version}".encode()


class ProfileCipher:
    def __init__(self, keys: dict[str, bytes], current: str):
        if current not in keys:
            raise KeyConfigError(f"no key {current!r}")
        self._keys = {version: AESGCM(key) for version, key in keys.items()}
        self.current = current

    @classmethod
    def from_setting(cls, value: str | None) -> "ProfileCipher":
        """From PANTRYPATH_PROFILE_KEYS ("v2:base64,v1:base64", newest first)."""
        if not value or not value.strip():
            raise KeyConfigError(
                "PANTRYPATH_PROFILE_KEYS isn't set. Make a key with: python -c \"from "
                "app.crypto import new_key; print(new_key())\" and set it as v1:<key>.")
        keys: dict[str, bytes] = {}
        for item in (part.strip() for part in value.split(",") if part.strip()):
            version, _, encoded = item.partition(":")
            if not VERSION_RE.match(version) or not encoded:
                raise KeyConfigError("PANTRYPATH_PROFILE_KEYS entries look like "
                                     "v1:<base64 key>, comma-separated")
            if version in keys:
                raise KeyConfigError(f"PANTRYPATH_PROFILE_KEYS lists {version} twice")
            try:
                key = base64.b64decode(encoded, validate=True)
            except binascii.Error as exc:
                raise KeyConfigError(f"key {version} isn't valid base64") from exc
            if len(key) != 32:
                raise KeyConfigError(f"key {version} is {len(key)} bytes; AES-256 needs 32")
            keys[version] = key
        return cls(keys, current=next(iter(keys)))

    def encrypt(self, user_id: str, data: dict[str, Any]) -> tuple[bytes, str]:
        """(nonce + ciphertext, key version) for a JSON-able dict."""
        nonce = os.urandom(NONCE_BYTES)
        plain = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        sealed = self._keys[self.current].encrypt(nonce, plain, _aad(user_id, self.current))
        return nonce + sealed, self.current

    def decrypt(self, user_id: str, blob: bytes, version: str) -> dict[str, Any]:
        cipher = self._keys.get(version)
        if cipher is None:
            raise DecryptError(f"no key {version!r} in PANTRYPATH_PROFILE_KEYS")
        blob = bytes(blob)
        try:
            plain = cipher.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:],
                                   _aad(user_id, version))
        except InvalidTag as exc:
            raise DecryptError("the value was changed, or belongs to another user") from exc
        return json.loads(plain)
