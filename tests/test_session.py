import pytest


@pytest.fixture()
def session_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    return tmp_path


def test_session_log_appends_lists_loads_and_rebuilds(session_home):
    from xharness.session import SessionLog, messages_from_events

    log = SessionLog()
    log.append({"type": "message", "role": "user", "content": "hello"})
    log.append(
        {
            "type": "message",
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "bash", "arguments": "{}"}],
        }
    )
    log.append({"type": "tool-result", "call_id": "c1", "name": "bash", "output": "done", "is_error": False})

    ids = [entry["id"] for entry in SessionLog.list()]
    assert log.id in ids

    events = SessionLog.load(log.id)
    assert len(events) == 3

    messages = messages_from_events(events)
    assert messages[0]["role"] == "user"
    assert messages[1]["tool_calls"][0]["name"] == "bash"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "c1"


def test_loading_missing_session_raises(session_home):
    from xharness.session import SessionLog

    with pytest.raises(FileNotFoundError):
        SessionLog.load("no-such-session")


def test_config_env_fallback_and_toml(session_home, tmp_path, monkeypatch):
    from xharness.config import load_config

    monkeypatch.setenv("XHARNESS_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("XHARNESS_MODEL", "env-model")
    monkeypatch.chdir(tmp_path)
    config = load_config()
    assert config.provider["model"] == "env-model"

    (tmp_path / "xharness.toml").write_text(
        "\n".join(
            [
                'default_provider = "local"',
                'approval = "auto"',
                "[providers.local]",
                'base_url = "http://localhost:8080/v1"',
                'model = "file-model"',
            ]
        )
    )
    config = load_config()
    assert config.provider["model"] == "file-model"
    assert config.approval == "auto"
    config = load_config(model_override="override-model")
    assert config.provider["model"] == "override-model"

    with pytest.raises(KeyError):
        load_config(provider_override="missing")
