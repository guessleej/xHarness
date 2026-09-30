import os

import pytest

from xharness.context import Harness
from xharness.session import SessionLog, session_owners, session_plugin, sessions_dir


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    return tmp_path


def test_shared_directory_without_a_user(home):
    assert sessions_dir() == os.path.join(str(home), "sessions")


def test_each_user_gets_their_own_directory(home):
    assert sessions_dir("alice").endswith(os.path.join("sessions", "u", "alice"))
    assert sessions_dir("alice") != sessions_dir("bob")


def test_path_traversal_in_a_user_name_is_refused(home):
    with pytest.raises(ValueError):
        sessions_dir("../../etc")
    with pytest.raises(ValueError):
        sessions_dir("a/b")


def test_listing_shows_only_your_own(home):
    SessionLog("s-alice", user="alice").append({"type": "message", "content": "hi"})
    SessionLog("s-bob", user="bob").append({"type": "message", "content": "hi"})
    assert [row["id"] for row in SessionLog.list("alice")] == ["s-alice"]
    assert [row["id"] for row in SessionLog.list("bob")] == ["s-bob"]
    assert SessionLog.list() == []  # the shared store stays empty


def test_one_user_cannot_load_anothers_transcript(home):
    SessionLog("s-alice", user="alice").append({"type": "message", "content": "secret"})
    with pytest.raises(FileNotFoundError):
        SessionLog.load("s-alice", user="bob")
    assert SessionLog.load("s-alice", user="alice")[0]["content"] == "secret"


def test_events_carry_the_owner(home):
    SessionLog("s1", user="alice").append({"type": "message", "content": "hi"})
    assert SessionLog.load("s1", user="alice")[0]["user"] == "alice"


def test_owners_enumerates_shared_and_per_user(home):
    SessionLog("shared").append({"type": "message", "content": "x"})
    SessionLog("s1", user="alice").append({"type": "message", "content": "x"})
    owners = dict((user, path) for user, path in session_owners())
    assert None in owners and "alice" in owners


def test_plugin_writes_meta_for_new_sessions(home):
    harness = Harness()
    harness.use("session", session_plugin(None, "alice", meta={"model": "m", "hosting": "self-hosted"}))
    log = harness.ctx.get("session")
    events = SessionLog.load(log.id, user="alice")
    assert events[0]["type"] == "meta" and events[0]["hosting"] == "self-hosted"


def test_resume_does_not_rewrite_meta(home):
    SessionLog("s1", user="alice").append({"type": "message", "content": "hi"})
    harness = Harness()
    harness.use("session", session_plugin("s1", "alice", meta={"model": "m"}))
    assert [event["type"] for event in SessionLog.load("s1", user="alice")] == ["message"]
