"""Profile encryption (app/crypto.py): no database needed."""

import base64

import pytest

from app.crypto import DecryptError, KeyConfigError, ProfileCipher, new_key

PREFS = {"name": "Ana", "zip": "45202", "avoid": ["milk", "peanuts"], "diet": "vegan"}


def cipher(value: str | None = None) -> ProfileCipher:
    return ProfileCipher.from_setting(value or f"v1:{new_key()}")


def test_round_trip_and_nothing_readable():
    c = cipher()
    blob, version = c.encrypt("user-1", PREFS)
    assert version == "v1"
    assert c.decrypt("user-1", blob, version) == PREFS
    for word in (b"Ana", b"45202", b"milk", b"peanuts", b"vegan"):
        assert word not in blob


def test_same_data_encrypts_differently_each_time():
    c = cipher()
    assert c.encrypt("user-1", PREFS)[0] != c.encrypt("user-1", PREFS)[0]


def test_a_value_moved_to_another_user_does_not_decrypt():
    c = cipher()
    blob, version = c.encrypt("user-1", PREFS)
    with pytest.raises(DecryptError):
        c.decrypt("user-2", blob, version)


def test_a_changed_byte_is_caught():
    c = cipher()
    blob, version = c.encrypt("user-1", PREFS)
    changed = bytearray(blob)
    changed[-1] ^= 1
    with pytest.raises(DecryptError):
        c.decrypt("user-1", bytes(changed), version)


def test_the_wrong_key_does_not_decrypt():
    blob, version = cipher().encrypt("user-1", PREFS)
    with pytest.raises(DecryptError):
        cipher().decrypt("user-1", blob, version)       # another v1 key


def test_key_rotation_reads_old_values_and_writes_with_the_new_key():
    old_key, new = new_key(), new_key()
    before = ProfileCipher.from_setting(f"v1:{old_key}")
    blob, version = before.encrypt("user-1", PREFS)
    after = ProfileCipher.from_setting(f"v2:{new}, v1:{old_key}")
    assert after.decrypt("user-1", blob, version) == PREFS
    assert after.encrypt("user-1", PREFS)[1] == "v2"
    with pytest.raises(DecryptError, match="no key 'v1'"):
        ProfileCipher.from_setting(f"v2:{new}").decrypt("user-1", blob, version)


def test_a_relabelled_key_version_does_not_decrypt():
    key = new_key()
    c = ProfileCipher.from_setting(f"v2:{key},v1:{key}")      # same key, two names
    blob, _ = c.encrypt("user-1", PREFS)
    with pytest.raises(DecryptError):
        c.decrypt("user-1", blob, "v1")


@pytest.mark.parametrize("value", [
    None, "", "  ", "nokey", "v1:", ":abc", "v1:not base64!",
    "v1:" + base64.b64encode(b"short").decode(),
    f"v1:{new_key()},v1:{new_key()}",
    f"bad version!:{new_key()}",
])
def test_bad_key_settings_stop_the_backend(value):
    with pytest.raises(KeyConfigError):
        ProfileCipher.from_setting(value)
