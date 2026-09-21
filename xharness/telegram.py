"""Telegram channel: chat with a node from the phone, approve tool calls with
buttons, and receive notifications pushed by other systems.

Runs inside `xharness web` so every Telegram chat is an ordinary conversation:
it shows up in the fleet view, obeys the same approval policy and token brake,
and lands in the same session log.

Security posture:
- the bot token is read from a file (or env var) at start and never logged
- only chat ids listed in `allowed_chats` are served; anything else is ignored
  (not even an error reply, so the bot does not confirm it exists)
- approvals stay a human decision: they arrive as inline buttons and the
  callback must come from an allowed chat
- outbound `/api/notify` is a POST behind the web bearer token and CSRF header
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from typing import Any, Callable

TELEGRAM_API = "https://api.telegram.org"
CHAT_GUIDANCE = (
    "This conversation comes from a Telegram chat on the operator's phone. "
    "Greetings, questions and small talk get a direct short answer in the user's language, with no tools. "
    "Use tools only when the message is an actual task that needs them; never explore the working "
    "directory on your own initiative."
)
MESSAGE_LIMIT = 4000  # Telegram caps at 4096; keep headroom for the "(1/3)" marker
POLL_TIMEOUT_SECONDS = 30

Api = Callable[[str, dict[str, Any]], Any]


def load_token(settings: dict[str, Any]) -> str:
    """`token_file` first (chmod 600), then `token_env`; the token is never put in the config itself."""
    path = settings.get("token_file")
    if path:
        with open(os.path.expanduser(str(path)), encoding="utf-8") as handle:
            token = handle.read().strip()
        if token:
            return token
    env = settings.get("token_env")
    if env and os.environ.get(str(env)):
        return os.environ[str(env)].strip()
    raise RuntimeError("telegram: no bot token; set [channels.telegram] token_file (preferred) or token_env")


def make_api(token: str) -> Api:
    def call(method: str, params: dict[str, Any]) -> Any:
        data = json.dumps(params).encode("utf-8")
        request = urllib.request.Request(
            f"{TELEGRAM_API}/bot{token}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=POLL_TIMEOUT_SECONDS + 15) as response:  # nosec B310 - fixed https host (TELEGRAM_API)
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(f"telegram {method}: {payload.get('description', 'error')}")
        return payload.get("result")

    return call


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split on paragraph, then line, then hard boundary; numbered when more than one part."""
    text = text.strip() or "（空白回覆）"
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            parts.append(rest)
            break
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    total = len(parts)
    return [f"({i}/{total})\n{part}" for i, part in enumerate(parts, 1)]


class TelegramBridge:
    """One conversation per chat; long-polls getUpdates in a daemon thread."""

    def __init__(
        self,
        app: Any,
        settings: dict[str, Any],
        api: Api | None = None,
        cwd: str | None = None,
    ) -> None:
        self.app = app
        self.allowed = {int(chat) for chat in settings.get("allowed_chats") or []}
        if not self.allowed:
            raise RuntimeError("telegram: allowed_chats is empty; list the chat ids that may talk to this node")
        self.api = api or make_api(load_token(settings))
        self.cwd = cwd or os.getcwd()
        self.chats: dict[int, Any] = {}  # chat id -> Conversation
        self.pending: dict[str, tuple[int, str]] = {}  # approval id -> (chat, conv id)
        self.offset = 0
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    # ----- lifecycle -----

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="xharness-telegram", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                updates = self.api("getUpdates", {"offset": self.offset, "timeout": POLL_TIMEOUT_SECONDS,
                                                  "allowed_updates": ["message", "callback_query"]})
                backoff = 1.0
            except Exception as error:  # noqa: BLE001 - network hiccups must not kill the bridge
                print(f"[telegram] poll failed: {type(error).__name__}: {str(error)[:120]}", file=sys.stderr)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            for update in updates or []:
                self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                try:
                    self.handle(update)
                except Exception as error:  # noqa: BLE001 - one bad update must not stop the loop
                    print(f"[telegram] update failed: {type(error).__name__}: {error}", file=sys.stderr)

    # ----- inbound -----

    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._callback(update["callback_query"])
            return
        message = update.get("message") or {}
        chat = int((message.get("chat") or {}).get("id", 0))
        text = str(message.get("text") or "").strip()
        if chat not in self.allowed or not text:
            return  # silently: the bot must not confirm its existence to strangers
        if text.startswith("/"):
            self._command(chat, text)
            return
        conv = self._conversation(chat)
        if not self.app.send(conv, text):
            self.send(chat, "上一個任務還在跑；先 /stop 或等它結束。")
            return
        threading.Thread(target=self._relay, args=(chat, conv), name=f"xharness-telegram-{chat}", daemon=True).start()

    def _command(self, chat: int, text: str) -> None:
        command = text.split()[0].split("@")[0].lower()
        conv = self.chats.get(chat)
        if command in ("/start", "/help"):
            self.send(chat, "xHarness 節點。直接打字就是任務。\n/new 開新對話　/stop 停止　/usage 用量　/status 節點狀態")
        elif command == "/new":
            self.chats.pop(chat, None)
            self.send(chat, "已開新對話。")
        elif command == "/stop":
            ok = bool(conv) and self.app.stop(conv)
            self.send(chat, "已要求停止。" if ok else "沒有在跑的任務。")
        elif command == "/usage":
            usage = conv.usage() if conv else None
            self.send(chat, json.dumps(usage, ensure_ascii=False) if usage else "尚無用量。")
        elif command == "/status":
            report = self.app.fleet(include_nodes=False)
            summary = report["summary"]
            self.send(chat, f"節點 {report['node']}（v{report['version']}）：模型 {report['model']}、"
                            f"對話 {summary['conversations']}、跑中 {summary['running']}、等審批 {summary['waiting']}")
        else:
            self.send(chat, "不認得這個指令；/help 看清單。")

    def _conversation(self, chat: int) -> Any:
        with self.lock:
            conv = self.chats.get(chat)
            if conv is None:
                conv = self.app.create(extra_system=CHAT_GUIDANCE)
                self.chats[chat] = conv
            return conv

    def _callback(self, query: dict[str, Any]) -> None:
        message = query.get("message") or {}
        chat = int((message.get("chat") or {}).get("id", 0))
        data = str(query.get("data") or "")
        query_id = str(query.get("id") or "")
        if chat not in self.allowed or ":" not in data:
            return
        verb, approval_id = data.split(":", 1)
        target = self.pending.pop(approval_id, None)
        if target is None or target[0] != chat:
            self._answer(query_id, "這個審批已處理或不屬於此對話。")
            return
        conv = self.app.get(target[1])
        allow = verb == "allow"
        ok = bool(conv) and self.app.approve(conv, approval_id, allow)
        verdict = ("已允許，執行中" if allow else "已拒絕") if ok else "審批已失效"
        self._answer(query_id, verdict)
        # Replace the buttons with the verdict so the decision is visible in the chat itself.
        if message.get("message_id"):
            try:
                self.api("editMessageText", {"chat_id": chat, "message_id": message["message_id"],
                                             "text": f"{message.get('text') or ''}\n\n{verdict}"})
            except Exception as error:  # noqa: BLE001 - cosmetic; the approval itself already went through
                print(f"[telegram] edit failed: {error}", file=sys.stderr)

    def _answer(self, query_id: str, text: str) -> None:
        if query_id:
            self.api("answerCallbackQuery", {"callback_query_id": query_id, "text": text})

    # ----- conversation -> chat -----

    def _relay(self, chat: int, conv: Any) -> None:
        """Follow one task: forward tool starts and approvals, then the answer."""
        after = 0
        while True:
            events = conv.wait(after, timeout=POLL_TIMEOUT_SECONDS)
            for event in events:
                after = event["seq"]
                kind = event.get("type")
                if kind == "tool_start":
                    self.send(chat, f"[工具] {event.get('name')} {str(event.get('args') or '')[:160]}")
                elif kind == "tool_end":
                    self.send(chat, f"[工具{'完成' if event.get('ok') else '失敗'}] {event.get('name')}，等模型下一步…")
                elif kind == "approval_request":
                    self.pending[event["id"]] = (chat, conv.id)
                    self.api("sendMessage", {
                        "chat_id": chat,
                        "text": f"需要你決定：\n{event.get('summary')}",
                        "reply_markup": {"inline_keyboard": [[
                            {"text": "允許", "callback_data": f"allow:{event['id']}"},
                            {"text": "拒絕", "callback_data": f"deny:{event['id']}"},
                        ]]},
                    })
                elif kind == "done":
                    self.send(chat, str(event.get("answer") or ""))
                    return
                elif kind == "error":
                    self.send(chat, f"任務失敗：{event.get('message')}")
                    return
            if not conv.running and not events:
                return

    # ----- outbound -----

    def send(self, chat: int, text: str) -> None:
        for part in split_message(text):
            self.api("sendMessage", {"chat_id": chat, "text": part, "disable_web_page_preview": True})

    def notify(self, text: str, chats: list[int] | None = None) -> int:
        """Push a notification to allowed chats (all by default). Returns chats reached."""
        targets = [c for c in (chats or sorted(self.allowed)) if c in self.allowed]
        for chat in targets:
            self.send(chat, text)
        return len(targets)


def telegram_settings(config: Any) -> dict[str, Any] | None:
    channels = getattr(config, "channels", None) or {}
    settings = channels.get("telegram") if isinstance(channels, dict) else None
    return settings if settings and settings.get("enabled", True) else None
