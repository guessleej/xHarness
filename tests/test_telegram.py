"""Telegram channel: allowed chats only, approvals by button, long answers split, notify endpoint."""
import time

import pytest

from xharness.config import ResolvedConfig
from xharness.llm import AssistantTurn
from xharness.telegram import TelegramBridge, split_message
from xharness.web import WebApp

from test_web import factory_with

CHAT = 4242
CONFIG = ResolvedConfig(provider={"base_url": "http://localhost:1/v1", "model": "fake"}, provider_name="fake")


class FakeApi:
    def __init__(self):
        self.sent = []  # (method, params)

    def __call__(self, method, params):
        self.sent.append((method, params))
        return {"ok": True}

    def texts(self, chat=CHAT):
        return [p["text"] for m, p in self.sent if m == "sendMessage" and p["chat_id"] == chat]

    def wait_for(self, needle, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(needle in t for t in self.texts()):
                return True
            time.sleep(0.02)
        return False


def bridge_with(turns, approval_mode="auto"):
    app = WebApp(CONFIG, approval_mode=approval_mode, harness_factory=factory_with(turns))
    api = FakeApi()
    bridge = TelegramBridge(app, {"allowed_chats": [CHAT]}, api=api)
    return app, api, bridge


def message(text, chat=CHAT):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def test_requires_allowed_chats():
    app = WebApp(CONFIG, harness_factory=factory_with([]))
    with pytest.raises(RuntimeError):
        TelegramBridge(app, {"allowed_chats": []}, api=FakeApi())


def test_stranger_is_ignored_silently():
    app, api, bridge = bridge_with([AssistantTurn(content="hi")])
    bridge.handle(message("hello", chat=999))
    assert api.sent == [] and app.list() == []


def test_task_round_trip_delivers_answer():
    app, api, bridge = bridge_with([AssistantTurn(content="答案：四十二")])
    bridge.handle(message("問題"))
    assert api.wait_for("答案：四十二")
    assert len(app.list()) == 1 and app.list()[0]["preview"] == "問題"


def test_approval_arrives_as_buttons_and_callback_resolves_it():
    turns = [
        AssistantTurn(content="", tool_calls=[{"id": "c1", "name": "mutate", "arguments": "{}"}]),
        AssistantTurn(content="做完了"),
    ]
    app, api, bridge = bridge_with(turns, approval_mode="prompt")
    bridge.handle(message("改東西"))
    deadline = time.time() + 5
    while time.time() < deadline and not bridge.pending:
        time.sleep(0.02)
    assert bridge.pending, "approval never reached telegram"
    keyboard = [p for m, p in api.sent if m == "sendMessage" and "reply_markup" in p]
    assert keyboard and keyboard[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("allow:")
    approval_id = next(iter(bridge.pending))
    bridge.handle({"update_id": 2, "callback_query": {"id": "q1", "data": f"allow:{approval_id}",
                                                       "message": {"chat": {"id": CHAT}}}})
    assert api.wait_for("做完了")
    assert ("answerCallbackQuery", {"callback_query_id": "q1", "text": "已允許"}) in api.sent


def test_callback_from_other_chat_cannot_approve():
    app, api, bridge = bridge_with([], approval_mode="prompt")
    bridge.pending["abcd"] = (CHAT, "conv")
    bridge.handle({"update_id": 3, "callback_query": {"id": "q2", "data": "allow:abcd", "message": {"chat": {"id": 999}}}})
    assert "abcd" in bridge.pending  # untouched: the stranger's callback is dropped before lookup


def test_commands_new_and_help():
    app, api, bridge = bridge_with([AssistantTurn(content="x")])
    bridge.handle(message("/help"))
    bridge.handle(message("問"))
    assert api.wait_for("x")
    bridge.handle(message("/new"))
    assert CHAT not in bridge.chats and any("已開新對話" in t for t in api.texts())


def test_split_message_numbers_parts_and_respects_limit():
    parts = split_message("段落一\n\n" + "字" * 120 + "\n\n段落三", limit=80)
    assert len(parts) > 1 and all(len(p) <= 80 + 8 for p in parts)
    assert parts[0].startswith("(1/") and split_message("短") == ["短"]


def test_notify_reaches_allowed_chats_only():
    app, api, bridge = bridge_with([])
    app.telegram = bridge
    assert app.notify("訓練完成", chats=[CHAT, 999]) == 1
    assert api.texts() == ["訓練完成"]
    assert WebApp(CONFIG, harness_factory=factory_with([])).notify("沒通道") == 0
