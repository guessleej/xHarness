import os

import pytest

from xharness.config import ResolvedConfig
from xharness.identity import UserStore
from xharness.seccheck import FAIL, PASS, WARN, run

PROVIDER = {"base_url": "http://127.0.0.1:8080/v1", "model": "m"}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # no xharness.toml in the way
    return tmp_path


def find(report, key):
    return next(item for item in report["items"] if item["id"] == key)


def test_loopback_binding_passes(home):
    report = run(ResolvedConfig(provider=PROVIDER, provider_name="p"), bound_host="127.0.0.1")
    assert find(report, "binding")["state"] == PASS


def test_public_binding_without_token_fails(home):
    report = run(ResolvedConfig(provider=PROVIDER, provider_name="p"), bound_host="0.0.0.0", has_token=False)
    item = find(report, "binding")
    assert item["state"] == FAIL and item["fix"]


def test_public_binding_with_token_passes(home):
    report = run(ResolvedConfig(provider=PROVIDER, provider_name="p"), bound_host="0.0.0.0", has_token=True)
    assert find(report, "binding")["state"] == PASS


def test_secret_in_url_is_caught(home):
    config = ResolvedConfig(provider={"base_url": "https://api.example/v1?api_key=abc123", "model": "m"}, provider_name="p")
    item = find(run(config, bound_host="127.0.0.1"), "secret-in-url")
    assert item["state"] == FAIL


def test_report_never_contains_the_secret_value(home):
    config = ResolvedConfig(provider={"base_url": "https://api.example/v1?api_key=SUPERSECRET", "model": "m"}, provider_name="p")
    report = run(config, bound_host="127.0.0.1")
    assert "SUPERSECRET" not in str(report)


def test_inline_secret_in_config_file_fails(home):
    (home / "xharness.toml").write_text(
        'default_provider = "local"\n[providers.local]\nbase_url = "http://x/v1"\nmodel = "m"\napi_key = "sk-live-abc"\n',
        encoding="utf-8",
    )
    report = run(ResolvedConfig(provider=PROVIDER, provider_name="p"), bound_host="127.0.0.1")
    item = find(report, "config-inline-secret")
    assert item["state"] == FAIL
    assert "sk-live-abc" not in str(report)


def test_env_reference_is_not_an_inline_secret(home):
    (home / "xharness.toml").write_text(
        '[providers.local]\nbase_url = "http://x/v1"\nmodel = "m"\napi_key_env = "XHARNESS_API_KEY"\n',
        encoding="utf-8",
    )
    report = run(ResolvedConfig(provider=PROVIDER, provider_name="p"), bound_host="127.0.0.1")
    assert find(report, "config-inline-secret")["state"] == PASS


def test_node_token_in_config_fails(home):
    config = ResolvedConfig(
        provider=PROVIDER, provider_name="p",
        fleet={"nodes": {"farm": {"url": "http://n:3080", "token": "plain-token"}}},
    )
    report = run(config, bound_host="127.0.0.1")
    assert find(report, "node-token-farm")["state"] == FAIL
    assert "plain-token" not in str(report)


def test_sandbox_modes(home):
    for mode, expected in (("require", PASS), ("auto", WARN), ("off", FAIL)):
        config = ResolvedConfig(provider=PROVIDER, provider_name="p", sandbox={"mode": mode})
        assert find(run(config, bound_host="127.0.0.1"), "sandbox")["state"] == expected


def test_identity_off_is_a_warning(home):
    report = run(ResolvedConfig(provider=PROVIDER, provider_name="p"), bound_host="127.0.0.1", users=UserStore({}))
    assert find(report, "identity")["state"] == WARN


def test_plain_ldap_fails(home):
    users = UserStore({"backend": "ldap", "ldap": {"url": "ldap://ad.example", "user_dn": "{user}@x"}})
    config = ResolvedConfig(provider=PROVIDER, provider_name="p")
    assert find(run(config, bound_host="127.0.0.1", users=users), "ldap-tls")["state"] == FAIL


def test_ldaps_with_verification_passes(home):
    users = UserStore({"backend": "ldap", "ldap": {"url": "ldaps://ad.example", "user_dn": "{user}@x"}})
    config = ResolvedConfig(provider=PROVIDER, provider_name="p")
    assert find(run(config, bound_host="127.0.0.1", users=users), "ldap-tls")["state"] == PASS


def test_weak_token_value_flagged(home, monkeypatch):
    monkeypatch.setenv("XHARNESS_WEB_TOKEN", "changeme")
    report = run(ResolvedConfig(provider=PROVIDER, provider_name="p"), bound_host="127.0.0.1")
    assert find(report, "weak-values")["state"] == FAIL


def test_verdict_is_the_worst_state(home):
    report = run(ResolvedConfig(provider=PROVIDER, provider_name="p"), bound_host="0.0.0.0", has_token=False)
    assert report["verdict"] == FAIL
    assert report["summary"]["fail"] >= 1


def test_every_finding_carries_a_fix(home):
    report = run(ResolvedConfig(provider=PROVIDER, provider_name="p"), bound_host="0.0.0.0", has_token=False)
    for item in report["items"]:
        if item["state"] in (WARN, FAIL):
            assert item["fix"], f"{item['id']} has no remediation"
