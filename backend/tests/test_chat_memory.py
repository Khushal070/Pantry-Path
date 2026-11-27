"""Chats in memory (app/chat_memory.py), with a clock the tests move by hand."""

from app.chat_memory import ChatMemory


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def memory(idle_minutes: float = 120) -> tuple[ChatMemory, Clock]:
    clock = Clock()
    return ChatMemory(idle_minutes, clock=clock), clock


def test_a_chat_ends_after_it_sits_idle():
    chats, clock = memory(idle_minutes=120)
    chats.put("s_1", {"id": "s_1", "messages": []})
    clock.now += 119 * 60
    assert chats.get("s_1") is not None
    clock.now += 2 * 60
    assert chats.get("s_1") is None and chats.all() == []


def test_writing_keeps_a_chat_alive_but_a_finished_search_does_not():
    chats, clock = memory(idle_minutes=120)
    chats.put("s_1", {"id": "s_1", "messages": []})
    clock.now += 100 * 60
    chats.update("s_1", lambda c: c["messages"].append("hi"))           # the shopper typed
    clock.now += 100 * 60
    chats.update("s_1", lambda c: c.update(done=True), touch=False)      # a search finished
    clock.now += 21 * 60
    assert chats.get("s_1") is None


def test_reads_are_copies():
    chats, _ = memory()
    chats.put("s_1", {"id": "s_1", "messages": []})
    copy = chats.get("s_1")
    copy["messages"].append("changed outside")
    updated = chats.update("s_1", lambda c: c["messages"].append("inside"))
    updated["messages"].append("changed outside again")
    assert chats.get("s_1")["messages"] == ["inside"]


def test_delete_and_update_after_the_end():
    chats, _ = memory()
    chats.put("s_1", {"id": "s_1"})
    assert chats.delete("s_1") is True and chats.delete("s_1") is False
    assert chats.update("s_1", lambda c: None) is None


def test_a_change_that_fails_leaves_the_chat_as_it_was():
    chats, _ = memory()
    chats.put("s_1", {"id": "s_1", "messages": ["a", "b"]})

    def half_done(chat):
        chat["messages"].remove("b")
        raise ValueError("boom")

    try:
        chats.update("s_1", half_done)
    except ValueError:
        pass
    assert chats.get("s_1")["messages"] == ["a", "b"]
