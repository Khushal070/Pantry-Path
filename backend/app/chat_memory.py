"""Chats live in this process's memory only: they are never saved.

A chat ends when nobody has written in it for PANTRYPATH_CHAT_IDLE_MIN minutes (default
20), when it is deleted, when its user logs out or deletes their account, or when the
server restarts. The user's lasting preferences are kept in their profile (auth.py), not here.

Every read hands out a copy, so a reply being turned into JSON is never changed halfway by
a search finishing in the background. One lock serializes read-modify-write, which needs
one backend process: one uvicorn worker, one Cloud Run instance.
"""

import copy
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

Chat = dict[str, Any]


class ChatMemory:
    def __init__(self, idle_minutes: float = 20, clock: Callable[[], float] = time.monotonic):
        self.idle_s = idle_minutes * 60
        self._clock = clock
        self._chats: dict[str, Chat] = {}
        self._last_used: dict[str, float] = {}
        self._lock = threading.RLock()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the lock across a read-modify-write of one or more chats."""
        with self._lock:
            yield

    def _forget_idle(self) -> None:
        cutoff = self._clock() - self.idle_s
        for chat_id in [c for c, t in self._last_used.items() if t <= cutoff]:
            self._chats.pop(chat_id, None)
            self._last_used.pop(chat_id, None)

    def get(self, chat_id: str) -> Chat | None:
        with self._lock:
            self._forget_idle()
            chat = self._chats.get(chat_id)
            return copy.deepcopy(chat) if chat is not None else None

    def put(self, chat_id: str, chat: Chat) -> None:
        with self._lock:
            self._chats[chat_id] = copy.deepcopy(chat)
            self._last_used[chat_id] = self._clock()

    def update(self, chat_id: str, change: Callable[[Chat], None],
               touch: bool = True) -> Chat | None:
        """Change a chat under the lock; None if it has ended. The change runs on a copy
        that replaces the chat only if it succeeds, so a change that fails halfway leaves
        the chat as it was. touch=False for changes nobody typed (a search finishing), so
        they don't keep a chat alive."""
        with self._lock:
            self._forget_idle()
            current = self._chats.get(chat_id)
            if current is None:
                return None
            chat = copy.deepcopy(current)
            change(chat)
            self._chats[chat_id] = chat
            if touch:
                self._last_used[chat_id] = self._clock()
            return copy.deepcopy(chat)

    def delete(self, chat_id: str) -> bool:
        with self._lock:
            self._last_used.pop(chat_id, None)
            return self._chats.pop(chat_id, None) is not None

    def all(self) -> list[Chat]:
        with self._lock:
            self._forget_idle()
            return [copy.deepcopy(c) for c in self._chats.values()]
