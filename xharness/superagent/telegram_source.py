"""The Telegram group Source: the node's bot listens in the groups an admin
names, and the resident agent answers in the topic where it was asked.

The bot is the one already mounted for notifications and approvals (a bot has
exactly one reader of its updates), so this Source takes group messages from
that bridge instead of polling. Direct messages to the bot stay ordinary
conversations with the node.

Threads: a forum topic is one Thread; elsewhere a reply chain is one Thread,
rooted at the first message. Actors are Telegram user ids (stable; a username
can change). A message addresses the agent when it @mentions the bot or
replies to the bot. To receive messages that do not address the bot, the
bot's privacy mode must be off (BotFather /setprivacy) or the bot an admin.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .model import Doc, key, now_iso

KIND = "telegram"
IM_WORD_LIMIT = 100


def check_config(config: Doc) -> Doc:
    chats = []
    for item in config.get("chats") or []:
        if isinstance(item, (int, str)):
            item = {"id": item}
        try:
            chat = int(item.get("id"))
        except (TypeError, ValueError):
            raise ValueError("chats: Telegram chat ids are numbers (groups are negative)") from None
        chats.append({"id": chat, "name": str(item.get("name", ""))[:60], "all": bool(item.get("all"))})
    return {"chats": chats}


def words(text: str) -> int:
    """Rough IM length: CJK characters count about two to a word, other text by spaces."""
    cjk = len(re.findall(r"[㐀-鿿豈-﫿]", text))
    rest = re.sub(r"[㐀-鿿豈-﫿]", " ", text)
    return (cjk + 1) // 2 + len(rest.split())


class TelegramGroupSource:
    kind = KIND

    def __init__(self, source_id: str, config: Doc, store: Any, receive: Callable[[Doc], str],
                 bridge: Callable[[], Any]) -> None:
        self.id = source_id
        self.config = {**check_config(config), "interval": 30}
        self.store = store
        self.receive = receive
        self.bridge = bridge
        self.username = ""
        self.bot_id = 0
        self.health: Doc = {"state": "starting", "detail": "", "at": now_iso()}

    def _chat(self, chat: int) -> Doc | None:
        return next((c for c in self.config["chats"] if c["id"] == chat), None)

    def scope(self, meta: Doc) -> Doc:
        chat = self._chat(int(meta.get("chat", 0) or 0))
        if chat is None:
            return {"listened": False, "reason": "otherChat"}
        return {"listened": True, "observed": chat["all"], "row": str(chat["id"])}

    def poll(self) -> int:
        """Nothing to poll: make sure the bridge hands group messages here."""
        bridge = self.bridge()
        if bridge is None:
            self.health = {"state": "error", "detail": "no Telegram bot is mounted on this node", "at": now_iso()}
            return 0
        if bridge.intercept != self.take:
            bridge.intercept = self.take
        if not self.username:
            try:
                me = bridge.api("getMe", {})
                self.username, self.bot_id = str(me.get("username") or ""), int(me.get("id") or 0)
            except Exception as error:  # noqa: BLE001 - shown as the Source's health
                self.health = {"state": "error", "detail": f"getMe failed: {type(error).__name__}", "at": now_iso()}
                return 0
        self.health = {"state": "ready", "detail": f"@{self.username} in {len(self.config['chats'])} group(s)", "at": now_iso()}
        return 0

    def _root(self, chat: int, message: Doc) -> str:
        if message.get("is_topic_message") and message.get("message_thread_id"):
            return f"topic-{message['message_thread_id']}"
        parent = message.get("reply_to_message") or {}
        name = f"tg-root:{self.id}:{chat}:{message['message_id']}"
        root = self.store.kv(f"tg-root:{self.id}:{chat}:{parent['message_id']}") if parent else None
        root = root or (str(parent["message_id"]) if parent else str(message["message_id"]))
        self.store.set_kv(name, root)
        return root

    def take(self, message: Doc) -> bool:
        """Called by the bridge for every message; True when it belonged to a group this Source listens to."""
        chat = int((message.get("chat") or {}).get("id", 0) or 0)
        if not chat or self._chat(chat) is None or (message.get("chat") or {}).get("type") not in ("group", "supergroup"):
            return False
        text = str(message.get("text") or message.get("caption") or "")
        sender = message.get("from") or {}
        if not text.strip() or int(sender.get("id", 0) or 0) == self.bot_id:
            return True
        root = self._root(chat, message)
        parent = message.get("reply_to_message") or {}
        mentioned = bool(self.username) and re.search(rf"@{re.escape(self.username)}\b", text, re.I) is not None
        replied_to_bot = int((parent.get("from") or {}).get("id", 0) or 0) == self.bot_id and self.bot_id != 0
        version = str(message.get("edit_date") or "")
        at = message.get("edit_date") or message.get("date") or 0
        trigger = {
            "id": key(self.id, str(chat), str(message["message_id"]), version), "source": self.id,
            "threads": [{"source": self.id, "id": f"{chat}:{root}"}],
            "actor": {"source": self.id, "id": str(sender.get("id", ""))}, "human": not sender.get("is_bot"),
            "at": _iso(at), "text": text,
            "channel": {"source": self.id, "thread": f"{self.id}:{chat}:{root}",
                        "address": {"chat": str(chat), "reply_to": str(message["message_id"]),
                                    **({"topic": str(message["message_thread_id"])} if message.get("is_topic_message") else {})}},
            "meta": {"chat": chat, "row": str(chat), "username": sender.get("username", ""),
                     "name": " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p)},
        }
        if mentioned or replied_to_bot:
            trigger["addressed"] = "mention"
        self.receive(trigger)
        return True

    def send(self, message: Doc) -> str:
        bridge = self.bridge()
        if bridge is None:
            raise RuntimeError("no Telegram bot is mounted")
        address = message["channel"]["address"]
        params: Doc = {"chat_id": int(address["chat"]), "text": message["text"][:4000], "disable_web_page_preview": True}
        if address.get("reply_to"):
            params["reply_parameters"] = {"message_id": int(address["reply_to"]), "allow_sending_without_reply": True}
        if address.get("topic"):
            params["message_thread_id"] = int(address["topic"])
        try:
            bridge.api("sendMessage", params)
        except Exception as error:  # noqa: BLE001
            text = str(error).lower()
            if "chat not found" in text or "kicked" in text or "forbidden" in text:
                return "gone"
            raise
        return "sent"

    def link(self, address: Doc) -> str:
        chat = self._chat(int(address.get("chat", 0) or 0)) or {}
        return f"Telegram {chat.get('name') or address.get('chat')}"


def _iso(seconds: Any) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(seconds), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return now_iso()
