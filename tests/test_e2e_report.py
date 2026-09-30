"""End-to-end: sign-in, per-user isolation, and the admin-only
/api/usage-report and /api/security-check surfaces a school's IT office
and management actually use.
"""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.llm import AssistantTurn
from xharness.telemetry import telemetry_plugin
from xharness.tools import tools_plugin
from xharness.web import CSRF_HEADER, WebApp, make_handler

AUTH = {"backend": "local", "session_hours": 1, "default_quota_tokens_per_day": 1000,
        "pbkdf2_iterations": 1000}  # cheap hashing keeps the suite fast; production uses the default
CONFIG = ResolvedConfig(
    provider={"base_url": "http://127.0.0.1:1/v1", "model": "fake"},
    provider_name="fake",
    auth=AUTH,
)


class ScriptedAdapter:
    model = "fake"

    def stream(self, messages, tools, on_delta=None):
        return AssistantTurn(content="done")


def factory(resume, user=None):
    harness = Harness()
    harness.use("tools", tools_plugin)
    harness.use("telemetry", telemetry_plugin())
    harness.ctx.provide("llm", ScriptedAdapter())
    return harness


class Client:
    def __init__(self, port):
        self.port = port
        self.token = None

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json", CSRF_HEADER: "1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return response.status, raw.decode("utf-8", errors="replace")

    def sign_in(self, user, password):
        status, payload = self.call("POST", "/api/login", {"user": user, "password": password})
        if status == 200:
            self.token = payload["token"]
        return status, payload


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    app = WebApp(CONFIG, approval_mode="auto", harness_factory=factory)
    app.bound_host = "127.0.0.1"
    app.users.add("admin1", "a-long-password", role="admin", display="資訊組")
    app.users.add("teacher1", "a-long-password", role="user", display="張老師")
    app.users.add("teacher2", "a-long-password", role="user")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, None))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Client(httpd.server_address[1]), app
    httpd.shutdown()
    app.dispose()


def test_anonymous_is_refused_and_told_to_sign_in(server):
    client, _ = server
    status, payload = client.call("GET", "/api/conversations")
    assert status == 401 and payload["auth"] == "users"


def test_sign_in_and_identify(server):
    client, _ = server
    assert client.sign_in("teacher1", "a-long-password")[0] == 200
    status, me = client.call("GET", "/api/me")
    assert status == 200
    assert me["user"]["display"] == "張老師" and me["admin"] is False
    assert me["quota"]["day"]["limit"] == 1000


def test_bad_password_gives_nothing_away(server):
    client, _ = server
    status, payload = client.sign_in("teacher1", "wrong")
    assert status == 401
    unknown = client.sign_in("nobody", "wrong")[1]
    assert payload["error"] == unknown["error"]


def test_logout_revokes_the_token(server):
    client, _ = server
    client.sign_in("teacher1", "a-long-password")
    assert client.call("POST", "/api/logout", {})[0] == 200
    assert client.call("GET", "/api/conversations")[0] == 401


def test_conversations_are_private_to_their_owner(server):
    client, _ = server
    client.sign_in("teacher1", "a-long-password")
    status, conv = client.call("POST", "/api/conversations", {})
    assert status == 201 and conv["user"] == "teacher1"
    client.sign_in("teacher2", "a-long-password")
    assert client.call("GET", "/api/conversations")[1] == []
    assert client.call("POST", f"/api/conversations/{conv['id']}/messages", {"text": "hi"})[0] == 404


def test_admin_sees_every_conversation(server):
    client, _ = server
    client.sign_in("teacher1", "a-long-password")
    client.call("POST", "/api/conversations", {})
    client.sign_in("admin1", "a-long-password")
    assert len(client.call("GET", "/api/conversations")[1]) == 1


def test_usage_report_is_admin_only(server):
    client, _ = server
    client.sign_in("teacher1", "a-long-password")
    assert client.call("GET", "/api/usage-report")[0] == 403


def test_usage_report_returns_all_six_blocks(server):
    client, _ = server
    client.sign_in("admin1", "a-long-password")
    status, report = client.call("GET", "/api/usage-report?since=30d")
    assert status == 200
    # The six blocks the report promises, each present and populated.
    for block in ("people", "spend", "hosting", "machines", "outbound", "health"):
        assert block in report, f"missing block: {block}"
        assert report[block] not in (None, {}, [])
    assert report["people"]["accounts"] == 3
    assert report["spend"]["cost"]["assumption"]  # an estimate always states its assumption
    assert isinstance(report["outbound"]["items"], list) and report["outbound"]["items"]
    assert "governance" in report["health"]


def test_usage_report_reports_its_own_problems(server):
    client, app = server
    client.sign_in("admin1", "a-long-password")
    report = client.call("GET", "/api/usage-report")[1]
    # Nothing is configured with a cost rate here, so the block must say so
    # rather than showing a confident zero.
    assert report["spend"]["cost"]["collected"] is False
    assert report["spend"]["cost"]["estimate"] is None
    assert isinstance(report["health"]["issues"], list)


def test_security_check_is_admin_only_and_scores_the_install(server):
    client, _ = server
    client.sign_in("teacher1", "a-long-password")
    assert client.call("GET", "/api/security-check")[0] == 403
    client.sign_in("admin1", "a-long-password")
    status, report = client.call("GET", "/api/security-check")
    assert status == 200
    assert report["verdict"] in ("pass", "warn", "fail")
    assert report["items"] and all({"id", "title", "state", "detail"} <= set(item) for item in report["items"])
    assert "a-long-password" not in json.dumps(report)


def test_admin_can_create_and_disable_accounts(server):
    client, app = server
    client.sign_in("admin1", "a-long-password")
    status, created = client.call("POST", "/api/users", {
        "action": "add", "user": "teacher3", "password": "a-long-password", "role": "user"})
    assert status == 201 and created["name"] == "teacher3"
    assert client.call("POST", "/api/users", {"action": "disable", "user": "teacher3"})[0] == 200
    names = {row["name"]: row for row in client.call("GET", "/api/users")[1]["users"]}
    assert names["teacher3"]["disabled"] is True


def test_non_admin_cannot_create_accounts(server):
    client, _ = server
    client.sign_in("teacher1", "a-long-password")
    status, _ = client.call("POST", "/api/users", {
        "action": "add", "user": "mallory", "password": "a-long-password", "role": "admin"})
    assert status == 403


def test_weak_password_refused_by_the_api(server):
    client, _ = server
    client.sign_in("admin1", "a-long-password")
    status, payload = client.call("POST", "/api/users", {"action": "add", "user": "x", "password": "short"})
    assert status == 400 and "8" in payload["error"]


def test_access_audit_records_sign_ins(server):
    client, _ = server
    client.sign_in("teacher1", "wrong")
    client.sign_in("admin1", "a-long-password")
    status, records = client.call("GET", "/api/access-audit")
    assert status == 200
    assert any(row["user"] == "teacher1" and row["ok"] is False for row in records)


def test_sessions_list_is_per_user(server):
    client, _ = server
    client.sign_in("teacher1", "a-long-password")
    assert client.call("GET", "/api/sessions")[1] == []


def test_csrf_header_still_required(server):
    client, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    conn.request("POST", "/api/login", body="{}", headers={"Host": f"127.0.0.1:{client.port}"})
    assert conn.getresponse().status == 403
    conn.close()
