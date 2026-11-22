import pytest

from app.store import JsonStore, StoreError


def test_put_get_update_delete(tmp_path):
    store = JsonStore(tmp_path)
    store.put("users", "p_1", {"id": "p_1", "name": "Ana"})
    assert store.get("users", "p_1") == {"id": "p_1", "name": "Ana"}
    updated = store.update("users", "p_1", lambda p: p.update(name="Ana B"))
    assert updated["name"] == "Ana B"
    assert store.get("users", "p_1")["name"] == "Ana B"
    assert store.delete("users", "p_1") is True
    assert store.get("users", "p_1") is None
    assert store.delete("users", "p_1") is False
    assert store.update("users", "p_1", lambda p: None) is None


def test_files_are_readable_json_and_no_temp_files_remain(tmp_path):
    store = JsonStore(tmp_path)
    store.put("sessions", "s_1", {"id": "s_1", "title": "Quiche — für 4"})
    assert (tmp_path / "sessions" / "s_1.json").read_text(encoding="utf-8").startswith("{\n")
    assert "für" in (tmp_path / "sessions" / "s_1.json").read_text(encoding="utf-8")
    assert [p.name for p in (tmp_path / "sessions").iterdir()] == ["s_1.json"]


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "x" * 65, "a.b"])
def test_unsafe_ids_are_never_paths(tmp_path, bad):
    store = JsonStore(tmp_path)
    assert store.get("users", bad) is None
    assert store.delete("users", bad) is False
    with pytest.raises(KeyError):
        store.put("users", bad, {})


def test_a_broken_file_is_skipped_in_listings_and_reported_on_read(tmp_path):
    store = JsonStore(tmp_path)
    store.put("users", "good", {"id": "good"})
    (tmp_path / "users" / "bad.json").write_text("{not json")
    assert store.all("users") == [{"id": "good"}]
    with pytest.raises(StoreError):
        store.get("users", "bad")
