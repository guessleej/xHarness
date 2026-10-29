"""Notification channels: encrypted at rest, never echoed back, validated against
fixed hosts, sent only for the events a channel asked for, and admin-only on the web."""
import json
import os
import stat
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from xharness import channels
from xharness.channels import ChannelError, ChannelHub
from xharness.config import ResolvedConfig
from xharness.llm import AssistantTurn
from xharness.telegram import TelegramBridge
from xharness.web import WebApp, make_handler

from test_e2e_report import CONFIG as AUTH_CONFIG, Client as AuthClient, factory as auth_factory
from test_telegram import FakeApi, message
from test_web import CONFIG, factory_with

TG_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"
LINE_TOKEN = "lineTOKENabcdefghijklmnopqrstuvwxyz0123456789"
LINE_USER = "U" + "0123456789abcdef" * 2
TEAMS_URL = "https://prod-01.westus.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sv=1.0&sig=SECRETSIGabcdefghijklmnop"


class FakePost:
    def __init__(self, status=200):
        self.calls = []  # (url, body, headers)
        self.status = status

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        if url.endswith("/getMe"):
            return 200, {"ok": True, "result": {"username": "xh_test_bot"}}
        if url.endswith("/v2/bot/info"):
            return 200, {"displayName": "云碩通知"}
        return self.status, ({"ok": True} if self.status == 200 else {"description": f"bad bot{TG_TOKEN} at /invoke?sv=1.0&sig=SECRETSIGabcdefghijklmnop"})

    def sent(self):
        return [c for c in self.calls if not c[0].endswith(("/getMe", "/v2/bot/info"))]


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    monkeypatch.delenv("XHARNESS_CHANNELS_KEY_FILE", raising=False)
    return tmp_path


def hub_with(status=200):
    post = FakePost(status)
    return ChannelHub(post=post), post


def test_secret_is_encrypted_and_files_are_private(home):
    hub, _ = hub_with()
    saved = hub.save({"type": "telegram", "secret": TG_TOKEN, "targets": ["413430644"], "events": ["done"]})
    raw = (home / "channels.json").read_text(encoding="utf-8")
    assert TG_TOKEN not in raw and channels.PREFIX in raw
    assert stat.S_IMODE(os.stat(home / "channels.json").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(home / "channels.key").st_mode) == 0o600
    assert "secret" not in saved and saved["secret_set"] and saved["account"] == "@xh_test_bot"
    assert hub.telegram_settings() == {"token": TG_TOKEN, "allowed_chats": [413430644]}


def test_tampered_ciphertext_is_refused(home):
    sealed = channels.encrypt("x" * 30)
    broken = sealed[:-6] + ("A" if sealed[-6] != "A" else "B") + sealed[-5:]
    with pytest.raises(ChannelError):
        channels.decrypt(broken)
    with pytest.raises(ChannelError):
        channels.decrypt("plain-text-token")


@pytest.mark.parametrize("form, words", [
    ({"type": "telegram", "secret": "not-a-token", "events": ["done"]}, "BotFather"),
    ({"type": "telegram", "secret": TG_TOKEN, "targets": ["abc"], "events": ["done"]}, "chat id"),
    ({"type": "line", "secret": LINE_TOKEN, "targets": ["U123"], "events": ["done"]}, "User ID"),
    ({"type": "line", "secret": LINE_TOKEN, "targets": [], "events": ["done"]}, "接收對象"),
    ({"type": "teams", "secret": "http://prod.logic.azure.com/x", "events": ["done"]}, "https"),
    ({"type": "teams", "secret": "https://192.168.11.27/hook", "events": ["done"]}, "https"),
    ({"type": "teams", "secret": "https://evil.example/logic.azure.com", "events": ["done"]}, "https"),
    ({"type": "teams", "secret": TEAMS_URL, "events": []}, "事件"),
    ({"type": "smtp", "secret": "x", "events": ["done"]}, "類型"),
])
def test_bad_forms_are_refused_in_words(home, form, words):
    hub, _ = hub_with()
    with pytest.raises(ChannelError) as raised:
        hub.save(form)
    assert words in str(raised.value)
    assert hub.raw() == []


def test_teams_validation_makes_no_request(home):
    hub, post = hub_with()
    hub.save({"type": "teams", "secret": TEAMS_URL, "events": ["done"]})
    assert post.calls == []


def test_update_with_blank_secret_keeps_it_and_one_telegram_only(home):
    hub, _ = hub_with()
    first = hub.save({"type": "telegram", "secret": TG_TOKEN, "targets": ["1234"], "events": ["done"]})
    hub.save({"id": first["id"], "name": "手機", "secret": "", "targets": ["1234", "5678"], "events": ["error"]})
    assert hub.telegram_settings()["token"] == TG_TOKEN
    assert hub.get(first["id"])["events"] == ["error"] and hub.get(first["id"])["name"] == "手機"
    with pytest.raises(ChannelError):
        hub.save({"type": "telegram", "secret": TG_TOKEN, "events": ["done"]})


def test_line_and_teams_payloads(home):
    hub, post = hub_with()
    line = hub.save({"type": "line", "secret": LINE_TOKEN, "targets": [LINE_USER], "events": ["external"]})
    teams = hub.save({"type": "teams", "secret": TEAMS_URL, "events": ["external"]})
    assert hub.dispatch("external", "標題", "內容", wait=True) == 2
    by_url = {url: (body, headers) for url, body, headers in post.sent()}
    body, headers = by_url["https://api.line.me/v2/bot/message/push"]
    assert headers["Authorization"] == f"Bearer {LINE_TOKEN}" and body["to"] == LINE_USER
    assert "內容" in body["messages"][0]["text"]
    card = by_url[TEAMS_URL][0]["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard" and card["body"][1]["text"] == "內容"
    assert hub.get(line["id"])["last"]["ok"] and hub.get(teams["id"])["stats"]["sent"] == 1


def test_events_threshold_enabled_and_private_content(home):
    hub, post = hub_with()
    quiet = hub.save({"type": "line", "secret": LINE_TOKEN, "targets": [LINE_USER], "events": ["done"],
                      "min_seconds": 30})
    assert hub.dispatch("done", "t", "footer", detail="答案", seconds=5, wait=True) == 0
    assert hub.dispatch("error", "t", "footer", detail="錯", wait=True) == 0       # not subscribed
    assert hub.dispatch("done", "t", "footer", detail="答案", seconds=31, wait=True) == 1
    text = post.sent()[-1][1]["messages"][0]["text"]
    assert "答案" not in text and "網頁查看" in text          # LINE defaults to not carrying content
    hub.save({"id": quiet["id"], "targets": [LINE_USER], "events": ["done"], "min_seconds": 0, "include_text": True})
    hub.dispatch("done", "t", "footer", detail="答案", seconds=1, wait=True)
    assert "答案" in post.sent()[-1][1]["messages"][0]["text"]
    hub.set_enabled(quiet["id"], False)
    assert hub.dispatch("done", "t", "footer", detail="答案", seconds=99, wait=True) == 0


def test_failure_is_recorded_scrubbed(home):
    hub, post = hub_with(status=401)
    saved = hub.save({"type": "telegram", "secret": TG_TOKEN, "targets": ["1234"], "events": ["done"]})
    ok, detail = hub.test(saved["id"])
    assert not ok and TG_TOKEN.split(":")[1] not in detail
    last = hub.get(saved["id"])["last"]
    assert last["ok"] is False and TG_TOKEN.split(":")[1] not in last["detail"] and "SECRETSIG" not in last["detail"]


def test_telegram_without_targets_explains_pairing(home):
    hub, _ = hub_with()
    saved = hub.save({"type": "telegram", "secret": TG_TOKEN, "targets": [], "events": ["done"]})
    ok, detail = hub.test(saved["id"])
    assert not ok and "授權" in detail


def test_pairing_bridge_records_strangers_silently():
    app = WebApp(CONFIG, harness_factory=factory_with([]))
    api = FakeApi()
    bridge = TelegramBridge(app, {"allowed_chats": [], "pairing": True}, api=api)
    knock = message("hello", chat=777)
    knock["message"]["from"] = {"first_name": "偉業", "username": "jeff"}
    bridge.handle(knock)
    assert api.sent == [] and app.list() == []
    assert bridge.strangers[777]["username"] == "jeff" and "text" not in bridge.strangers[777]


def test_task_events_reach_channels_and_telegram_chats_are_not_doubled(home, monkeypatch):
    monkeypatch.setattr(TelegramBridge, "start", lambda self: None)
    app = WebApp(CONFIG, harness_factory=factory_with([AssistantTurn(content="答案在這裡")]))
    post = FakePost()
    app.channels.post = post
    app.channels.save({"type": "line", "secret": LINE_TOKEN, "targets": [LINE_USER], "events": ["done"],
                       "min_seconds": 0, "include_text": True})
    conv = app.create()
    assert app.send(conv, "幫我做事")
    deadline = time.time() + 5
    while time.time() < deadline and not post.sent():
        time.sleep(0.02)
    url, body, _ = post.sent()[0]
    assert url.endswith("/message/push") and "答案在這裡" in body["messages"][0]["text"]
    assert "任務完成" in body["messages"][0]["text"]
    app.dispose()


def test_web_approval_offered_with_buttons_in_telegram(home, monkeypatch):
    monkeypatch.setattr(TelegramBridge, "start", lambda self: None)
    turns = [AssistantTurn(content="", tool_calls=[{"id": "c1", "name": "mutate", "arguments": "{}"}]),
             AssistantTurn(content="ok")]
    app = WebApp(CONFIG, approval_mode="ask", harness_factory=factory_with(turns))
    app.channels.post = FakePost()
    app.channels.save({"type": "telegram", "secret": TG_TOKEN, "targets": ["4242"], "events": ["approval"]})
    assert app.telegram is not None and app.telegram.allowed == {4242}
    api = FakeApi()
    app.telegram.api = api
    conv = app.create()
    app.send(conv, "改檔案")
    assert api.wait_for("需要你核准")
    buttons = [p for m, p in api.sent if m == "sendMessage" and "reply_markup" in p][0]
    approval_id = buttons["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]
    app.telegram.handle({"callback_query": {"id": "q", "data": f"allow:{approval_id}",
                                            "message": {"chat": {"id": 4242}, "message_id": 9}}})
    deadline = time.time() + 5
    while time.time() < deadline and conv.running:
        time.sleep(0.02)
    assert any(e.get("type") == "approval_resolved" and e["allowed"] for e in conv.events)
    app.dispose()


@pytest.fixture()
def admin_server(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    monkeypatch.delenv("XHARNESS_CHANNELS_KEY_FILE", raising=False)
    monkeypatch.setattr(TelegramBridge, "start", lambda self: None)
    app = WebApp(AUTH_CONFIG, approval_mode="auto", harness_factory=auth_factory)
    app.channels.post = FakePost()
    app.users.add("admin1", "a-long-password", role="admin")
    app.users.add("teacher1", "a-long-password", role="user")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, None))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield AuthClient(httpd.server_address[1]), app
    httpd.shutdown()
    httpd.server_close()
    app.dispose()


def test_api_is_admin_only_and_never_returns_secrets(admin_server, tmp_path):
    client, app = admin_server
    client.sign_in("teacher1", "a-long-password")
    assert client.call("GET", "/api/channels")[0] == 403
    assert client.call("POST", "/api/channels", {"action": "save", "channel": {}})[0] == 403
    client.sign_in("admin1", "a-long-password")
    status, saved = client.call("POST", "/api/channels", {"action": "save", "channel": {
        "type": "teams", "name": "IT 群組", "secret": TEAMS_URL, "events": ["done", "error"]}})
    assert status == 200 and saved["secret_set"]
    status, page = client.call("GET", "/api/channels")
    assert status == 200 and page["available"] is True and page["channels"][0]["name"] == "IT 群組"
    assert "SECRETSIG" not in json.dumps(page)
    assert client.call("POST", "/api/channels", {"action": "test", "id": saved["id"]})[1]["ok"] is True
    status, refused = client.call("POST", "/api/channels", {"action": "save", "channel": {
        "type": "teams", "secret": "https://10.0.0.1/hook", "events": ["done"]}})
    assert status == 400 and "https" in refused["error"]
    assert client.call("POST", "/api/channels", {"action": "delete", "id": saved["id"]})[1]["ok"] is True
    audit = (tmp_path / "access-audit.jsonl").read_text(encoding="utf-8")
    assert "channel-save" in audit and "channel-delete" in audit and "SECRETSIG" not in audit


def test_config_bot_can_be_moved_to_the_page(tmp_path, monkeypatch):
    monkeypatch.setenv("XHARNESS_HOME", str(tmp_path))
    monkeypatch.setattr(TelegramBridge, "start", lambda self: None)
    token_file = tmp_path / "telegram-token"
    token_file.write_text(TG_TOKEN)
    config = ResolvedConfig(provider=CONFIG.provider, provider_name="fake",
                            channels={"telegram": {"token_file": str(token_file), "allowed_chats": [4242]}})
    app = WebApp(config, harness_factory=factory_with([]))
    app.channels.post = FakePost()
    app.start_channels()
    assert app.channel_overview()["config_telegram"] == {"chats": 1}
    saved = app.channels.import_config({"allowed_chats": [4242]}, TG_TOKEN)
    assert saved["targets"] == ["4242"] and app.telegram is not None and app.telegram.pairing
    assert app.channel_overview()["config_telegram"] is None
    app.dispose()


def test_security_check_and_report_cover_channels(home):
    from xharness import seccheck
    from xharness.report import _channels_row

    hub, _ = hub_with()
    hub.save({"type": "teams", "name": "IT", "secret": TEAMS_URL, "events": ["done"], "include_text": True})
    items = {i["id"]: i for i in seccheck.run(CONFIG)["items"]}
    assert items["channels-encrypted"]["state"] == "pass"
    assert items["channels-file"]["state"] == "pass" and items["channels-key"]["state"] == "pass"
    assert items["channels-content"]["state"] == "warn" and "IT" in items["channels-content"]["detail"]
    row = _channels_row(CONFIG)
    assert row["state"] == "on" and row["safe"] is False and "SECRETSIG" not in json.dumps(row)
    data = json.loads((home / "channels.json").read_text())
    data["channels"][0]["secret"] = TEAMS_URL       # someone pasted a plain secret into the file
    (home / "channels.json").write_text(json.dumps(data))
    os.chmod(home / "channels.json", 0o644)
    items = {i["id"]: i for i in seccheck.run(CONFIG)["items"]}
    assert items["channels-encrypted"]["state"] == "fail" and items["channels-file"]["state"] == "fail"
    assert "SECRETSIG" not in json.dumps(items)
