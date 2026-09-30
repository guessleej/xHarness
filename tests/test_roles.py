import pytest

from xharness.config import ResolvedConfig
from xharness.identity import UserStore
from xharness.presets import build_harness

AUTH = {
    "backend": "local",
    "pbkdf2_iterations": 1000,
    "roles": {
        "principal": {"label": "校長室", "admin": True},
        "teacher": {"label": "教師", "quota_tokens_per_day": 200000},
        "student": {
            "label": "學生",
            "deny_tools": ["bash", "write", "edit", "subagent", "subagent_batch"],
            "quota_tokens_per_day": 30000,
        },
    },
}
CONFIG = ResolvedConfig(
    provider={"base_url": "http://127.0.0.1:1/v1", "model": "m"},
    provider_name="p",
    auth=AUTH,
    memory={"enabled": False},
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    return UserStore(AUTH)


def profile(store, name, role):
    store.add(name, "a-long-password", role=role)
    user, _ = store.authenticate(name, "a-long-password")
    return user


def test_builtin_roles_still_work(store):
    assert profile(store, "a", "admin").is_admin
    assert not profile(store, "b", "user").is_admin


def test_custom_tier_can_be_administrator(store):
    user = profile(store, "head", "principal")
    assert user.is_admin and user.role_label == "校長室"


def test_tier_supplies_the_quota(store):
    assert profile(store, "t", "teacher").quota_tokens_per_day == 200000
    assert profile(store, "s", "student").quota_tokens_per_day == 30000


def test_personal_quota_beats_the_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    settings = {**AUTH, "users": {"star": {"role": "student", "quota_tokens_per_day": 999}}}
    store = UserStore(settings)
    store.add("star", "a-long-password", role="student")
    user, _ = store.authenticate("star", "a-long-password")
    assert user.quota_tokens_per_day == 999


def test_unknown_tier_refused_on_create(store):
    with pytest.raises(ValueError):
        store.add("x", "a-long-password", role="nosuchtier")


def test_restricted_tier_does_not_receive_the_tools(store):
    student = profile(store, "s", "student")
    harness = build_harness(CONFIG, user=student)
    try:
        names = {tool.name for tool in harness.ctx.get("tools").list()}
        assert "bash" not in names and "write" not in names and "edit" not in names
        assert "read" in names and "grep" in names       # reading is still allowed
    finally:
        harness.dispose()


def test_ordinary_tier_keeps_everything(store):
    teacher = profile(store, "t", "teacher")
    harness = build_harness(CONFIG, user=teacher)
    try:
        names = {tool.name for tool in harness.ctx.get("tools").list()}
        assert {"bash", "write", "edit", "read"} <= names
    finally:
        harness.dispose()


def test_public_payload_exposes_the_tier(store):
    data = profile(store, "s", "student").public()
    assert data["role_label"] == "學生" and data["admin"] is False
    assert "bash" in data["denied_tools"]
