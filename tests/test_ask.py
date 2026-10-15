"""ask_user: the model asks the person instead of guessing.

Covers the tool itself, every front end that can answer it (Web UI, the hub
forwarding for a node, Telegram, a terminal), the cases where nobody can
(headless runs, sub-tasks), and the guards on how the page renders it.
"""

import json
import re
import time

import pytest

from xharness.config import ResolvedConfig
from xharness.context import Harness
from xharness.llm import AssistantTurn
from xharness.subagent import CHILD_TOOLS
from xharness.telemetry import telemetry_plugin
from xharness.tools import ToolContext, tools_plugin
from xharness.tools.ask import (
    MAX_ANSWER_CHARS,
    NOBODY,
    UNANSWERED,
    ask_tool_plugin,
    clean_options,
    resolve_answer,
    _ask,
)
from xharness.web import WebApp

CONFIG = ResolvedConfig(provider={"base_url": "http://localhost:1/v1", "model": "fake"}, provider_name="fake")
OPTIONS = [{"label": "保留原檔", "description": "另存新檔"}, {"label": "直接覆蓋"}]
ARGS = {"question": "要覆蓋原檔嗎？", "options": OPTIONS}


def ctx(ask=None):
    return ToolContext(cwd=".", approve=lambda _s: True, ask=ask)


# --- the tool -----------------------------------------------------------------

def test_options_must_number_two_to_four():
    assert isinstance(clean_options([{"label": "only"}]), str)
    assert isinstance(clean_options([{"label": str(i)} for i in range(5)]), str)
    assert len(clean_options([{"label": "a"}, {"label": "b"}])) == 2


def test_options_need_distinct_non_empty_labels():
    assert isinstance(clean_options([{"label": ""}, {"label": "b"}]), str)
    assert isinstance(clean_options([{"label": "a"}, {"label": "a"}]), str)
    assert isinstance(clean_options([{"label": "x" * 200}, {"label": "b"}]), str)


def test_bare_string_options_are_tolerated():
    assert clean_options(["是", "否"]) == [{"label": "是", "description": ""}, {"label": "否", "description": ""}]


def test_a_number_picks_that_option():
    options = clean_options(OPTIONS)
    assert resolve_answer("2", options) == "直接覆蓋"
    assert resolve_answer(" 1 ", options) == "保留原檔"


def test_out_of_range_number_is_kept_as_text():
    assert resolve_answer("7", clean_options(OPTIONS)) == "7"


def test_empty_reply_is_no_answer():
    assert resolve_answer("   ", clean_options(OPTIONS)) is None
    assert resolve_answer(None, clean_options(OPTIONS)) is None


def test_long_replies_are_capped():
    assert len(resolve_answer("x" * (MAX_ANSWER_CHARS + 50), clean_options(OPTIONS))) == MAX_ANSWER_CHARS


def test_chosen_option_is_reported_as_chosen():
    result = _ask(ARGS, ctx(ask=lambda q, o: "保留原檔"))
    assert not result.is_error and "chose: 保留原檔" in result.output


def test_own_words_are_reported_as_such():
    result = _ask(ARGS, ctx(ask=lambda q, o: "先備份再覆蓋"))
    assert "own words: 先備份再覆蓋" in result.output


def test_nobody_attached_says_so_without_waiting():
    started = time.time()
    result = _ask(ARGS, ctx(ask=None))
    assert result.output == NOBODY and not result.is_error
    assert time.time() - started < 0.5


def test_unanswered_question_tells_the_model_not_to_repeat_it():
    assert _ask(ARGS, ctx(ask=lambda q, o: None)).output == UNANSWERED


def test_bad_arguments_are_errors():
    assert _ask({"question": "", "options": OPTIONS}, ctx()).is_error
    assert _ask({"question": "q?", "options": [{"label": "a"}]}, ctx()).is_error
    assert _ask({"question": "x" * 600, "options": OPTIONS}, ctx()).is_error


def test_description_steers_against_inventing_answers():
    from xharness.tools.ask import DESCRIPTION
    assert "ask rather than invent" in DESCRIPTION


def test_children_do_not_question_the_person():
    assert "ask_user" in CHILD_TOOLS


def test_every_agent_gets_the_tool():
    from xharness.presets import build_harness
    harness = build_harness(CONFIG, no_session=True)
    try:
        assert harness.ctx.get("tools").get("ask_user") is not None
    finally:
        harness.dispose()


# --- Web UI and fleet ---------------------------------------------------------

class RecordingAdapter:
    """Scripted turns; remembers what the model was shown on each call."""

    model = "fake"

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    def stream(self, messages, tools, on_delta=None):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.turns.pop(0) if self.turns else AssistantTurn(content="(done)")


def ask_turns(final="好的"):
    return [
        AssistantTurn(content="", tool_calls=[{"id": "q1", "name": "ask_user", "arguments": json.dumps(ARGS)}]),
        AssistantTurn(content=final),
    ]


def make_app(turns):
    adapter = RecordingAdapter(turns)

    def factory(resume, user=None):
        harness = Harness()
        harness.use("tools", tools_plugin)
        harness.use("tool-ask", ask_tool_plugin)
        harness.use("telemetry", telemetry_plugin())
        harness.ctx.provide("llm", adapter)
        return harness

    return WebApp(CONFIG, approval_mode="auto", harness_factory=factory), adapter


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def tool_result_text(adapter):
    for messages in adapter.seen:
        for message in messages:
            if message.get("role") == "tool":
                return message["content"]
    return ""


def test_web_question_waits_then_answer_reaches_the_model():
    app, adapter = make_app(ask_turns())
    conv = app.create()
    app.send(conv, "整理這個檔案")
    assert wait_until(lambda: conv.summary()["pending_questions"])
    summary = conv.summary()
    assert summary["state"] == "waiting"
    question = summary["pending_questions"][0]
    assert question["question"] == "要覆蓋原檔嗎？" and [o["label"] for o in question["options"]] == ["保留原檔", "直接覆蓋"]

    assert app.answer(conv, question["id"], "保留原檔")
    assert wait_until(lambda: not conv.running)
    assert "chose: 保留原檔" in tool_result_text(adapter)
    kinds = [e["type"] for e in conv.events]
    assert "question" in kinds and "question_resolved" in kinds
    resolved = next(e for e in conv.events if e["type"] == "question_resolved")
    assert resolved["answer"] == "保留原檔"
    app.dispose()


def test_a_question_is_answered_once():
    app, _ = make_app(ask_turns())
    conv = app.create()
    app.send(conv, "go")
    assert wait_until(lambda: conv.summary()["pending_questions"])
    qid = conv.summary()["pending_questions"][0]["id"]
    assert app.answer(conv, qid, "直接覆蓋") is True
    assert app.answer(conv, qid, "保留原檔") is False
    assert app.answer(conv, "no-such-id", "x") is False
    app.dispose()


def test_blank_answer_is_refused():
    app, _ = make_app(ask_turns())
    conv = app.create()
    app.send(conv, "go")
    assert wait_until(lambda: conv.summary()["pending_questions"])
    assert app.answer(conv, conv.summary()["pending_questions"][0]["id"], "   ") is False
    app.stop(conv)
    app.dispose()


def test_stop_leaves_the_question_unanswered():
    app, adapter = make_app(ask_turns())
    conv = app.create()
    app.send(conv, "go")
    assert wait_until(lambda: conv.summary()["pending_questions"])
    assert app.stop(conv)
    assert wait_until(lambda: not conv.running)
    assert not conv.summary()["pending_questions"]


def test_answers_route_over_http():
    import threading
    from http.server import ThreadingHTTPServer
    from test_web import Client
    from xharness.web import make_handler

    app, adapter = make_app(ask_turns())
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, None))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = Client(server.server_address[1])
    try:
        status, conv = client.request("POST", "/api/conversations", {})
        client.request("POST", f"/api/conversations/{conv['id']}/messages", {"text": "go"})
        live = app.get(conv["id"])
        assert wait_until(lambda: live.summary()["pending_questions"])
        qid = live.summary()["pending_questions"][0]["id"]
        status, payload = client.request("POST", f"/api/conversations/{conv['id']}/answers", {"id": qid, "answer": "2"})
        assert status == 200 and payload["ok"]
        assert wait_until(lambda: not live.running)
        assert "chose: 直接覆蓋" in tool_result_text(adapter)   # "2" picked the second option
        status, _ = client.request("POST", f"/api/conversations/{conv['id']}/answers", {"id": qid, "answer": "x"})
        assert status == 404
    finally:
        server.shutdown()
        app.dispose()


def test_hub_may_forward_answers():
    from xharness.fleet import FORWARDABLE
    assert "answers" in FORWARDABLE


def test_fleet_table_flags_waiting_questions():
    from xharness.fleet import format_table
    report = {"ok": True, "name": "n1", "url": "http://n1", "latency_ms": 1, "summary": {}, "model": "m",
              "items": [{"state": "waiting", "usage": {}, "pending_questions": [{"id": "a"}], "preview": "p"}]}
    assert "asking=1" in format_table([report])


# --- Telegram -----------------------------------------------------------------

class FakeApi:
    def __init__(self):
        self.sent = []

    def __call__(self, method, params):
        self.sent.append((method, params))
        return {"ok": True}

    def last_keyboard(self):
        for method, params in reversed(self.sent):
            if method == "sendMessage" and "reply_markup" in params:
                return params
        return None


CHAT = 4242


def telegram_with(turns):
    from xharness.telegram import TelegramBridge
    app, adapter = make_app(turns)
    api = FakeApi()
    return app, adapter, api, TelegramBridge(app, {"allowed_chats": [CHAT]}, api=api)


def test_telegram_question_arrives_as_buttons():
    app, _, api, bridge = telegram_with(ask_turns())
    bridge.handle({"update_id": 1, "message": {"chat": {"id": CHAT}, "text": "整理檔案"}})
    assert wait_until(lambda: api.last_keyboard() is not None)
    sent = api.last_keyboard()
    assert "要覆蓋原檔嗎？" in sent["text"]
    buttons = [row[0] for row in sent["reply_markup"]["inline_keyboard"]]
    assert [b["text"] for b in buttons] == ["保留原檔", "直接覆蓋"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)   # Telegram's limit
    app.stop(next(iter(bridge.chats.values())))
    app.dispose()


def test_telegram_button_answers_the_question():
    app, adapter, api, bridge = telegram_with(ask_turns(final="完成"))
    bridge.handle({"update_id": 1, "message": {"chat": {"id": CHAT}, "text": "整理檔案"}})
    assert wait_until(lambda: api.last_keyboard() is not None)
    data = api.last_keyboard()["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
    bridge.handle({"update_id": 2, "callback_query": {"id": "cb", "data": data,
                                                      "message": {"chat": {"id": CHAT}, "message_id": 7, "text": "q"}}})
    conv = next(iter(bridge.chats.values()))
    assert wait_until(lambda: not conv.running)
    assert "chose: 直接覆蓋" in tool_result_text(adapter)
    app.dispose()


def test_telegram_text_reply_answers_instead_of_starting_a_task():
    app, adapter, api, bridge = telegram_with(ask_turns(final="完成"))
    bridge.handle({"update_id": 1, "message": {"chat": {"id": CHAT}, "text": "整理檔案"}})
    assert wait_until(lambda: api.last_keyboard() is not None)
    bridge.handle({"update_id": 2, "message": {"chat": {"id": CHAT}, "text": "先備份再覆蓋"}})
    conv = next(iter(bridge.chats.values()))
    assert wait_until(lambda: not conv.running)
    assert "own words: 先備份再覆蓋" in tool_result_text(adapter)
    assert len(app.list()) == 1          # no second conversation was started
    app.dispose()


def test_telegram_stale_button_is_refused():
    app, _, api, bridge = telegram_with(ask_turns())
    bridge.handle({"update_id": 2, "callback_query": {"id": "cb", "data": "ans:deadbeef:0",
                                                      "message": {"chat": {"id": CHAT}, "message_id": 7}}})
    assert any(m == "answerCallbackQuery" for m, _ in api.sent)
    app.dispose()


# --- terminal -----------------------------------------------------------------

def test_terminal_prompt_returns_the_raw_reply(monkeypatch, capsys):
    from xharness.cli import _prompt_question
    monkeypatch.setattr("builtins.input", lambda _prompt: " 2 ")
    assert _prompt_question("要覆蓋原檔嗎？", clean_options(OPTIONS)) == "2"
    printed = capsys.readouterr().out
    assert "1. 保留原檔 -- 另存新檔" in printed and "2. 直接覆蓋" in printed


def test_terminal_eof_is_no_answer(monkeypatch):
    from xharness.cli import _prompt_question

    def eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert _prompt_question("q?", clean_options(OPTIONS)) is None


# --- page guards --------------------------------------------------------------

def _script():
    from xharness.web import INDEX_HTML
    return INDEX_HTML


def test_question_text_is_never_rendered_as_html():
    """The question and options are model output: textContent only."""
    page = _script()
    body = page.split("function renderQuestion(ev){", 1)[1].split("function settleQuestion", 1)[0]
    assert "innerHTML" not in body
    fleet = page.split("(it.pending_questions||[]).forEach", 1)[1].split("const acts=", 1)[0]
    assert "innerHTML" not in fleet


def test_free_text_answer_is_ime_safe():
    page = _script()
    helper = page.split("function imeSafeEnter(input,onEnter){", 1)[1].split("function renderQuestion", 1)[0]
    assert "compositionstart" in helper and "isComposing" in helper and "keyCode===229" in helper


def test_approval_and_question_cards_carry_no_colour_bar():
    page = _script()
    assert not re.search(r"\.msg\.(approval|question):before", page)
