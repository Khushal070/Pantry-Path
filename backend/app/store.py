"""JSON file storage: one readable file per record, no database.

    data/users/<id>.json       an account: username, password hash, food preferences
    data/logins/<hash>.json    one logged-in browser (auth.py)
    data/sessions/<id>.json    one chat: its messages and the requests it confirmed

Writes go to a temporary file in the same folder and are then renamed over the old file
(os.replace is atomic), so a crash never leaves half a file. One lock serializes every
read-modify-write, which is enough for one backend process: run uvicorn with a single
worker (the default). Several workers or several Cloud Run instances would each have
their own lock and, on Cloud Run, their own disk.

Store results (Kroger data) never come here: they stay in memory (jobs.py).
"""

import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KINDS = ("users", "logins", "sessions")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StoreError(Exception):
    """A record exists but can't be read."""


class JsonStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()
        for kind in KINDS:
            (self.root / kind).mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the lock across a read-modify-write of one or more records."""
        with self._lock:
            yield

    def _path(self, kind: str, record_id: str) -> Path:
        if kind not in KINDS:
            raise ValueError(f"unknown record kind {kind!r}")
        if not ID_RE.match(record_id):
            raise KeyError(record_id)  # also stops path tricks such as "../x"
        return self.root / kind / f"{record_id}.json"

    def get(self, kind: str, record_id: str) -> dict[str, Any] | None:
        try:
            path = self._path(kind, record_id)
        except KeyError:
            return None
        with self._lock:
            if not path.is_file():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StoreError(f"{path} can't be read: {exc}") from exc

    def put(self, kind: str, record_id: str, data: dict[str, Any]) -> None:
        path = self._path(kind, record_id)
        text = json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{record_id}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

    def update(self, kind: str, record_id: str,
               change: Callable[[dict[str, Any]], None]) -> dict[str, Any] | None:
        """Read, change in place, write back: under the lock. None if the record is gone."""
        with self._lock:
            data = self.get(kind, record_id)
            if data is None:
                return None
            change(data)
            self.put(kind, record_id, data)
            return data

    def delete(self, kind: str, record_id: str) -> bool:
        try:
            path = self._path(kind, record_id)
        except KeyError:
            return False
        with self._lock:
            if not path.is_file():
                return False
            path.unlink()
            return True

    def all(self, kind: str) -> list[dict[str, Any]]:
        """Every readable record of a kind. A file that can't be read is logged and skipped."""
        records = []
        with self._lock:
            for path in sorted((self.root / kind).glob("*.json")):
                try:
                    records.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError) as exc:
                    log.warning("Skipping %s: %s", path, exc)
        return records
