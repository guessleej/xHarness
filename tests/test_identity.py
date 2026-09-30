import json
import os

import pytest

from xharness.identity import LOCKOUT_THRESHOLD, UserStore, hash_password, verify_password


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    return UserStore({"backend": "local", "default_quota_tokens_per_day": 1000, "pbkdf2_iterations": 1000})


def test_password_round_trip():
    # Low iteration count keeps the test fast; the default is set in the module.
    encoded = hash_password("correct horse battery", iterations=1000)
    assert verify_password("correct horse battery", encoded)
    assert not verify_password("wrong", encoded)
    assert "correct horse battery" not in encoded


def test_hash_is_salted():
    assert hash_password("same", iterations=1000) != hash_password("same", iterations=1000)


def test_verify_rejects_garbage():
    assert not verify_password("x", "not-an-encoded-hash")
    assert not verify_password("x", "md5$1$aa$bb")


def test_add_and_authenticate(store):
    store.add("alice", "a-long-password", role="admin", display="王小明")
    user, detail = store.authenticate("alice", "a-long-password")
    assert detail == "ok"
    assert user.is_admin and user.display == "王小明"
    assert user.quota_tokens_per_day == 1000  # inherited from the default


def test_account_file_is_owner_only(store, tmp_path):
    store.add("alice", "a-long-password")
    mode = os.stat(tmp_path / "users.json").st_mode & 0o777
    assert mode == 0o600


def test_password_never_stored_in_clear(store, tmp_path):
    store.add("alice", "a-long-password")
    assert "a-long-password" not in (tmp_path / "users.json").read_text()


def test_wrong_password_refused(store):
    store.add("alice", "a-long-password")
    user, detail = store.authenticate("alice", "nope")
    assert user is None and detail == "invalid credentials"


def test_unknown_user_refused_the_same_way(store):
    store.add("alice", "a-long-password")
    _, known = store.authenticate("alice", "nope")
    _, unknown = store.authenticate("mallory", "nope")
    # Identical wording: a different message would confirm which names exist.
    assert known == unknown


def test_lockout_after_repeated_failures(store):
    store.add("alice", "a-long-password")
    for _ in range(LOCKOUT_THRESHOLD):
        store.authenticate("alice", "nope")
    user, detail = store.authenticate("alice", "a-long-password")
    assert user is None and "try again" in detail


def test_short_password_refused(store):
    with pytest.raises(ValueError):
        store.add("alice", "short")


def test_unsafe_username_refused(store):
    with pytest.raises(ValueError):
        store.add("alice,cn=admin", "a-long-password")


def test_disable_keeps_history_and_kills_sessions(store):
    store.add("alice", "a-long-password")
    user, _ = store.authenticate("alice", "a-long-password")
    token = store.issue(user)
    assert store.resolve(token) is not None
    assert store.remove("alice")
    assert store.resolve(token) is None
    # The account is disabled, not erased, so past usage stays attributable.
    data = json.loads((store._file() and open(store._file(), encoding="utf-8").read()))
    assert data["alice"]["disabled"] is True
    assert store.authenticate("alice", "a-long-password")[1] == "account disabled"


def test_password_change_revokes_sessions(store):
    store.add("alice", "a-long-password")
    user, _ = store.authenticate("alice", "a-long-password")
    token = store.issue(user)
    store.set_password("alice", "another-long-password")
    assert store.resolve(token) is None


def test_token_expiry(store, monkeypatch):
    store.settings["session_hours"] = 0
    store.add("alice", "a-long-password")
    user, _ = store.authenticate("alice", "a-long-password")
    assert store.resolve(store.issue(user)) is None


def test_config_role_beats_stored_role(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    store = UserStore({"backend": "local", "users": {"alice": {"role": "admin"}}, "pbkdf2_iterations": 1000})
    store.add("alice", "a-long-password", role="user")
    user, _ = store.authenticate("alice", "a-long-password")
    assert user.is_admin


def test_audit_records_outcomes_without_secrets(store):
    store.add("alice", "a-long-password")
    store.authenticate("alice", "nope")
    store.authenticate("alice", "a-long-password")
    records = store.audit_tail()
    assert [row["ok"] for row in records[:2]] == [True, False]
    assert all("a-long-password" not in json.dumps(row) for row in records)


def test_disabled_store_is_inert(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    assert UserStore({}).enabled is False
