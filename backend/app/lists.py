"""Saved shopping lists: every finished search becomes one, in profile.lists.

A list holds only the shopper's own data: the dish, each item's name, our category,
quantity, status (buy / substitute / check_label / skip), substitute and tick, and the
recipe steps. Never Kroger data: no product, brand, size, aisle, price, stock or store.
Steps that mention a product, brand or store name from the search's result are dropped
before saving (scrub), and A3's per-item notes aren't kept (they quote products).

Each list is one encrypted value (crypto.py, bound to the user id), written and read as
pp_profile. A tick re-encrypts the whole value under a row lock.
"""

import logging
import re
from typing import Any

import sqlalchemy as sa

from app.crypto import DecryptError, ProfileCipher
from app.db import Database

log = logging.getLogger(__name__)

LIST_FORMAT = 1
MIN_NAME = 4   # shorter product/brand/store strings ("Ace") aren't used to scrub steps


class ListUnreadableError(Exception):
    """A saved list can't be decrypted (HTTP 500)."""


def kroger_names(result: dict[str, Any]) -> set[str]:
    """Every product, brand and store string of a search's result, lower-case."""
    names: set[str] = set()
    for ingredient in result.get("ingredients", []):
        for p in ingredient.get("products", []):
            names.update(v for v in (p.get("name"), p.get("brand")) if v)
    for store in result.get("stores", []):
        names.update(v for v in (store.get("name"), store.get("address")) if v)
        # "Kroger - Kroger On the Rhine": its parts too
        names.update(part.strip() for part in re.split(r"\s+-\s+", store.get("name") or ""))
    return {n.lower() for n in names if len(n.strip()) >= MIN_NAME}


def scrub(steps: list[str], names: set[str]) -> list[str]:
    """The steps that mention none of these names."""
    return [s for s in steps if not any(n in s.lower() for n in names)]


def from_search(result: dict[str, Any], answer: dict[str, Any] | None,
                extras: dict[str, Any], source: str,
                choices: dict[str, Any] | None = None) -> dict[str, Any]:
    """The list to save from a finished search (result.json + A3's answer, if any).
    choices: the shopper's picked products; only their brand is kept (the shopper's
    preference), never the product itself."""
    category = {i["name"]: i["category"] for i in result.get("ingredients", [])}
    items: list[dict[str, Any]] = []
    if answer and answer.get("ingredients"):
        for item in answer["ingredients"]:
            items.append({"name": item["name"],
                          "category": category.get(item["name"], "other"),
                          "status": item["status"], "substitute": item.get("substitute"),
                          "quantity": item.get("quantity"), "unit": item.get("unit")})
    else:   # no A3: buy what has a safe product, check the label of the rest
        for ingredient in result.get("ingredients", []):
            safe = any(p["verdict"] == "safe" for p in ingredient.get("products", []))
            items.append({"name": ingredient["name"], "category": ingredient["category"],
                          "status": "buy" if safe else "check_label", "substitute": None,
                          "quantity": None, "unit": None})
    for i, item in enumerate(items, 1):
        brand = ((choices or {}).get(item["name"]) or {}).get("brand") or None
        item.update(id=f"i{i}", ticked=False, brand=brand)
    request = result["request"]
    steps = scrub(list((answer or {}).get("steps") or []), kroger_names(result))
    return {"format": LIST_FORMAT, "dish": result["dish"]["name"],
            "cuisine": result["dish"]["cuisine"], "zip": request["zip"],
            "avoid": request["avoid"], "diet": request["diet"],
            "servings": extras.get("servings"), "items": items, "steps": steps,
            "source": source}


def summary(list_id: str, saved: dict[str, Any], created_at: Any) -> dict[str, Any]:
    items = saved["items"]
    return {"id": list_id, "dish": saved["dish"], "created_at": created_at,
            "items": len(items), "ticked": sum(i["ticked"] for i in items)}


class Lists:
    def __init__(self, db: Database, cipher: ProfileCipher):
        self.db, self.cipher = db, cipher

    def _seal(self, user_id: str, saved: dict[str, Any]) -> tuple[bytes, str]:
        return self.cipher.encrypt(user_id, saved)

    def _open(self, user_id: str, row: Any) -> dict[str, Any]:
        try:
            return self.cipher.decrypt(user_id, row.list_encrypted, row.key_version)
        except DecryptError as exc:
            log.error("List %s of %s can't be decrypted: %s", row.id, user_id, exc)
            raise ListUnreadableError("This list can't be read.") from exc

    def save(self, user_id: str, saved: dict[str, Any]) -> str:
        blob, version = self._seal(user_id, saved)
        with self.db.profile.begin() as conn:
            return str(conn.execute(sa.text(
                "INSERT INTO profile.lists (user_id, list_encrypted, key_version) "
                "VALUES (:u, :b, :v) RETURNING id"),
                {"u": user_id, "b": blob, "v": version}).scalar_one())

    def summaries(self, user_id: str) -> list[dict[str, Any]]:
        with self.db.profile.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT id, list_encrypted, key_version, created_at FROM profile.lists "
                "WHERE user_id = :u ORDER BY created_at DESC"), {"u": user_id}).all()
        out = []
        for row in rows:
            try:
                out.append(summary(str(row.id), self._open(user_id, row), row.created_at))
            except ListUnreadableError:
                continue   # logged; the others still show
        return out

    def get(self, user_id: str, list_id: str) -> dict[str, Any] | None:
        if not _is_uuid(list_id):
            return None
        with self.db.profile.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT id, list_encrypted, key_version, created_at, updated_at "
                "FROM profile.lists WHERE id = :id AND user_id = :u"),
                {"id": list_id, "u": user_id}).first()
        if row is None:
            return None
        return {**self._open(user_id, row), "id": str(row.id), "created_at": row.created_at,
                "updated_at": row.updated_at}

    def tick(self, user_id: str, list_id: str, item_id: str,
             ticked: bool) -> dict[str, Any] | None:
        """None when the list or item doesn't exist (or isn't this user's)."""
        if not _is_uuid(list_id):
            return None
        with self.db.profile.begin() as conn:
            row = conn.execute(sa.text(
                "SELECT id, list_encrypted, key_version, created_at FROM profile.lists "
                "WHERE id = :id AND user_id = :u FOR UPDATE"),
                {"id": list_id, "u": user_id}).first()
            if row is None:
                return None
            saved = self._open(user_id, row)
            item = next((i for i in saved["items"] if i["id"] == item_id), None)
            if item is None:
                return None
            item["ticked"] = ticked
            blob, version = self._seal(user_id, saved)
            updated = conn.execute(sa.text(
                "UPDATE profile.lists SET list_encrypted = :b, key_version = :v, "
                "updated_at = now() WHERE id = :id RETURNING updated_at"),
                {"b": blob, "v": version, "id": list_id}).scalar_one()
        return {**saved, "id": list_id, "created_at": row.created_at, "updated_at": updated}

    def delete(self, user_id: str, list_id: str) -> bool:
        if not _is_uuid(list_id):
            return False
        with self.db.profile.begin() as conn:
            return conn.execute(sa.text(
                "DELETE FROM profile.lists WHERE id = :id AND user_id = :u"),
                {"id": list_id, "u": user_id}).rowcount > 0


def _is_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F-]{36}", value or ""))
