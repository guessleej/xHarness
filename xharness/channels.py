"""Notification channels an admin sets up on the web page: Telegram, LINE and
Microsoft Teams. When a task finishes, fails or waits for an approval, the
channels that asked for that event get a message.

Telegram is also the two-way channel (telegram.py): the web-managed bot
replaces [channels.telegram] in the config file once it is saved here. LINE
and Teams are outbound only; both would need a public HTTPS address to talk
back, which a node on an internal network does not have.

Security posture:
- the bot token, LINE channel access token and Teams webhook URL are stored
  AES-256-GCM encrypted ("enc:v1:" prefix) in $XHARNESS_HOME/channels.json
  (0600); the key is a separate 0600 file, or /run/secrets/xharness_channels_key
- no API ever returns a stored secret; the page only learns that one is set
- outbound hosts are fixed: api.telegram.org, api.line.me, and for Teams only
  Microsoft webhook hosts, so the form cannot be used to reach internal addresses
- error text is scrubbed before it is stored or shown (a Telegram error can
  repeat the URL, which carries the token)
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .redact import scrub
from .session import harness_home

CHANNELS_FILE = "channels.json"
KEY_FILE = "channels.key"
KEY_SECRET_PATH = "/run/secrets/xharness_channels_key"
PREFIX = "enc:v1:"

TYPES = {"telegram": "Telegram", "line": "LINE", "teams": "Microsoft Teams"}
#: What can trigger a message, in the order the page lists them.
EVENTS = {
    "done": "任務完成",
    "error": "任務失敗",
    "approval": "需要核准",
    "external": "其他系統推送（/api/notify）",
}
DEFAULT_EVENTS = ["done", "error", "approval", "external"]
#: A "hi" that answers in four seconds is not worth a phone buzz.
DEFAULT_MIN_SECONDS = 30
TEAMS_HOSTS = (".webhook.office.com", ".logic.azure.com", ".powerplatform.com", ".powerautomate.com")
TELEGRAM_TOKEN = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
LINE_TARGET = re.compile(r"^[UCR][0-9a-f]{32}$")
HTTP_TIMEOUT = 15
TEXT_LIMIT = {"telegram": 4000, "line": 4900, "teams": 6000}

Post = Callable[[str, dict[str, Any] | None, dict[str, str]], tuple[int, Any]]


class ChannelError(ValueError):
    """A problem the admin can fix, in words the page can show as is."""


# ----- encryption ------------------------------------------------------------------

def _aesgcm() -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as error:
        raise ChannelError(
            "這台伺服器缺少加密套件，無法安全保存金鑰。請在執行 xharness 的環境安裝："
            "pip install 'xharness[secure]'（或 pip install cryptography），再重新啟動。"
        ) from error
    return AESGCM


def crypto_available() -> bool:
    try:
        _aesgcm()
    except ChannelError:
        return False
    return True


def key_file() -> str:
    return os.environ.get("XHARNESS_CHANNELS_KEY_FILE") or os.path.join(harness_home(), KEY_FILE)


def _write_private(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = f"{path}.tmp"
    # 0600 from the first byte: never world-readable, not even before a chmod.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


def _key() -> bytes:
    """The mounted secret wins; otherwise a key file made on first use."""
    for path in (os.environ.get("XHARNESS_CHANNELS_KEY_FILE"), KEY_SECRET_PATH):
        if path and os.path.isfile(path):
            with open(path, "rb") as handle:
                raw = handle.read().strip()
            key = base64.b64decode(raw) if len(raw) != 32 else raw
            if len(key) != 32:
                raise ChannelError(f"金鑰檔 {path} 不是 32 位元組（AES-256）的金鑰。")
            return key
    path = key_file()
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            key = base64.b64decode(handle.read().strip())
        if len(key) != 32:
            raise ChannelError(f"金鑰檔 {path} 損毀；刪除它會讓已存的頻道全部無法解密，請先備份。")
        return key
    key = secrets.token_bytes(32)
    _write_private(path, base64.b64encode(key))
    return key


def encrypt(text: str) -> str:
    nonce = secrets.token_bytes(12)
    sealed = _aesgcm()(_key()).encrypt(nonce, text.encode("utf-8"), b"xharness-channel")
    return PREFIX + base64.b64encode(nonce + sealed).decode("ascii")


def decrypt(value: str) -> str:
    if not value.startswith(PREFIX):
        raise ChannelError("頻道金鑰不是加密格式；請重新輸入一次。")
    blob = base64.b64decode(value[len(PREFIX):])
    try:
        return _aesgcm()(_key()).decrypt(blob[:12], blob[12:], b"xharness-channel").decode("utf-8")
    except ChannelError:
        raise
    except Exception as error:  # noqa: BLE001 - InvalidTag: wrong key, or the file was edited
        raise ChannelError("頻道金鑰解不開：金鑰檔換過或被改過。請重新輸入這個頻道的金鑰。") from error


# ----- outbound HTTP ----------------------------------------------------------------

def http_post(url: str, body: dict[str, Any] | None, headers: dict[str, str]) -> tuple[int, Any]:
    """POST JSON (GET when body is None); returns (status, parsed body or text)."""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # nosec B310 - hosts checked by the caller
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return status, text[:300]


def _clip(text: str, limit: int) -> str:
    text = text.strip() or "（空白）"
    return text if len(text) <= limit else text[: limit - 20].rstrip() + "\n…（內容過長，已截斷）"


PLAIN_STATUS = {
    400: "對方說請求格式不對",
    401: "金鑰或網址無效，可能已被撤銷或刪除",
    403: "沒有權限：機器人被封鎖，或對方還沒加官方帳號好友",
    404: "找不到對象：網址或接收對象錯了",
    429: "送太多次，被對方暫時限流",
}


def _why(status: int, payload: Any) -> str:
    """Plain words first, then the service's own reason (scrubbed)."""
    detail: Any = payload
    if isinstance(payload, dict):
        detail = payload.get("description") or payload.get("message") or payload.get("error") or ""
        if isinstance(detail, dict):   # Azure: {"error": {"code": ..., "message": ...}}
            detail = detail.get("message") or detail.get("code") or ""
    plain = PLAIN_STATUS.get(status) or ("對方服務暫時故障，稍後再試" if status >= 500 else f"HTTP {status}")
    return scrub(f"{plain}（{status}：{str(detail).strip()[:160]}）" if detail else plain)[:240]


# ----- the store --------------------------------------------------------------------

def channels_file() -> str:
    return os.path.join(harness_home(), CHANNELS_FILE)


def _now() -> float:
    return time.time()


def load_saved() -> list[dict[str, Any]]:
    """The saved channels as stored (secrets still sealed), for the security check and the report."""
    return ChannelHub().raw()


class ChannelHub:
    """The saved channels, the senders, and the event fan-out."""

    def __init__(self, path: str | None = None, post: Post | None = None) -> None:
        self._path = path
        self.post = post or http_post
        self.lock = threading.Lock()
        #: Called with the hub after the Telegram channel changes, to remount the bridge.
        self.on_telegram_change: Callable[[], None] | None = None
        #: Set by the web app: offers an approval with buttons in a Telegram chat.
        self.telegram_offer: Callable[[int, Any, str, str], bool] | None = None

    @property
    def path(self) -> str:
        # Resolved on each use: $XHARNESS_HOME may be set after the hub is made (tests, embedders).
        return self._path or channels_file()

    # --- file ---
    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"channels": []}
        return data if isinstance(data, dict) and isinstance(data.get("channels"), list) else {"channels": []}

    def _write(self, data: dict[str, Any]) -> None:
        _write_private(self.path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

    def raw(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self._read()["channels"])

    def get(self, channel_id: str) -> dict[str, Any] | None:
        return next((c for c in self.raw() if c.get("id") == channel_id), None)

    def telegram(self) -> dict[str, Any] | None:
        """The saved Telegram channel, if any (enabled or not)."""
        return next((c for c in self.raw() if c.get("type") == "telegram"), None)

    def telegram_settings(self) -> dict[str, Any] | None:
        """Bridge settings for the saved Telegram channel, in the shape of [channels.telegram]."""
        channel = self.telegram()
        if not channel or not channel.get("enabled", True):
            return None
        return {"token": decrypt(channel["secret"]), "allowed_chats": [int(t) for t in channel.get("targets") or []]}

    @staticmethod
    def public(channel: dict[str, Any]) -> dict[str, Any]:
        """What the page may see: everything except the secret."""
        out = {k: v for k, v in channel.items() if k != "secret"}
        out["secret_set"] = bool(channel.get("secret"))
        out["type_label"] = TYPES.get(str(channel.get("type")), str(channel.get("type")))
        return out

    def list_public(self) -> list[dict[str, Any]]:
        return [self.public(c) for c in self.raw()]

    # --- validation ---
    def _check(self, kind: str, secret: str, targets: list[str]) -> dict[str, Any]:
        """Validate and, for Telegram and LINE, ask the service who the token belongs to."""
        if kind == "telegram":
            if not TELEGRAM_TOKEN.match(secret):
                raise ChannelError("Telegram 機器人權杖格式不對：應該像 123456789:AAH…，從 @BotFather 複製整串。")
            for target in targets:
                if not re.fullmatch(r"-?\d{3,20}", target):
                    raise ChannelError(f"Telegram 對象「{target}」不是 chat id（一串數字，群組是負數）。")
            status, payload = self.post(f"https://api.telegram.org/bot{secret}/getMe", None, {})
            if status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
                raise ChannelError("Telegram 不接受這個權杖：" + _why(status, payload) + "。請到 @BotFather 確認。")
            bot = payload.get("result") or {}
            return {"account": "@" + str(bot.get("username") or "")}
        if kind == "line":
            if len(secret) < 20 or re.search(r"\s", secret):
                raise ChannelError("LINE 頻道存取權杖格式不對：到 LINE Developers 的 Messaging API 分頁按 Issue 取得長效權杖。")
            if not targets:
                raise ChannelError("LINE 至少要一個接收對象（你的 User ID，U 開頭 33 碼）。")
            for target in targets:
                if not LINE_TARGET.match(target):
                    raise ChannelError(f"LINE 對象「{target}」格式不對：User ID 是 U 開頭、後接 32 個 0-9a-f。")
            status, payload = self.post("https://api.line.me/v2/bot/info", None, {"Authorization": f"Bearer {secret}"})
            if status != 200 or not isinstance(payload, dict):
                raise ChannelError("LINE 不接受這個權杖：" + _why(status, payload) + "。請到 LINE Developers 重新發行。")
            return {"account": str(payload.get("displayName") or payload.get("basicId") or "")}
        if kind == "teams":
            parsed = urllib.parse.urlsplit(secret)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not any(host.endswith(suffix) for suffix in TEAMS_HOSTS):
                raise ChannelError("Teams 網址要是 Teams「工作流程」產生的 https 網址（主機名稱以 logic.azure.com、"
                                   "powerplatform.com 或 webhook.office.com 結尾）。")
            return {"account": host}
        raise ChannelError("頻道類型只能是 telegram、line 或 teams。")

    # --- edits ---
    def save(self, form: dict[str, Any], who: str = "") -> dict[str, Any]:
        """Create (no id) or update a channel. A blank secret on update keeps the stored one."""
        channel_id = str(form.get("id") or "")
        name = str(form.get("name") or "").strip()[:40]
        events = [e for e in (form.get("events") or []) if e in EVENTS]
        try:
            min_seconds = max(0, min(int(form.get("min_seconds", DEFAULT_MIN_SECONDS)), 86400))
        except (TypeError, ValueError) as error:
            raise ChannelError("「多久以上的任務才通知」要填秒數。") from error
        targets = [str(t).strip() for t in (form.get("targets") or []) if str(t).strip()]
        secret = str(form.get("secret") or "").strip()
        with self.lock:
            data = self._read()
            existing = next((c for c in data["channels"] if c.get("id") == channel_id), None) if channel_id else None
            if channel_id and existing is None:
                raise ChannelError("找不到這個頻道，可能已被刪除；請重新整理。")
            kind = str(existing["type"] if existing else form.get("type") or "")
            if kind not in TYPES:
                raise ChannelError("頻道類型只能是 telegram、line 或 teams。")
            if not existing and kind == "telegram" and any(c.get("type") == "telegram" for c in data["channels"]):
                raise ChannelError("已經有一個 Telegram 機器人；一個節點只接一個機器人，請編輯現有的那個。")
            if not existing and not secret:
                raise ChannelError("請填入金鑰（Telegram 權杖、LINE 權杖或 Teams 網址）。")
            if not events:
                raise ChannelError("至少勾一種要通知的事件。")
        # The service check talks to the network, so it runs outside the lock.
        plain = secret or decrypt(existing["secret"])
        info = self._check(kind, plain, targets)
        with self.lock:
            data = self._read()
            if channel_id:
                existing = next((c for c in data["channels"] if c.get("id") == channel_id), None)
                if existing is None:
                    raise ChannelError("找不到這個頻道，可能已被刪除；請重新整理。")
                channel = existing
            else:
                channel = {"id": secrets.token_hex(4), "type": kind, "created": _now(), "enabled": True, "last": None}
                data["channels"].append(channel)
            channel.update({
                "name": name or TYPES[kind],
                "targets": targets,
                "events": events,
                "min_seconds": min_seconds,
                "include_text": bool(form.get("include_text", kind == "telegram")),
                "account": info.get("account", ""),
                "updated": _now(),
                "updated_by": who,
            })
            if secret or "secret" not in channel:
                channel["secret"] = encrypt(plain)
            self._write(data)
            result = self.public(channel)
        if kind == "telegram":
            self._telegram_changed()
        return result

    def set_enabled(self, channel_id: str, enabled: bool) -> dict[str, Any]:
        with self.lock:
            data = self._read()
            channel = next((c for c in data["channels"] if c.get("id") == channel_id), None)
            if channel is None:
                raise ChannelError("找不到這個頻道。")
            channel["enabled"] = bool(enabled)
            channel["updated"] = _now()
            self._write(data)
            result = self.public(channel)
        if channel["type"] == "telegram":
            self._telegram_changed()
        return result

    def delete(self, channel_id: str) -> bool:
        with self.lock:
            data = self._read()
            before = len(data["channels"])
            kind = next((c.get("type") for c in data["channels"] if c.get("id") == channel_id), None)
            data["channels"] = [c for c in data["channels"] if c.get("id") != channel_id]
            if len(data["channels"]) == before:
                return False
            self._write(data)
        if kind == "telegram":
            self._telegram_changed()
        return True

    def add_target(self, channel_id: str, target: str) -> dict[str, Any]:
        """Authorise one more Telegram chat (from the pairing list)."""
        target = str(target).strip()
        if not re.fullmatch(r"-?\d{3,20}", target):
            raise ChannelError("chat id 應該是一串數字。")
        with self.lock:
            data = self._read()
            channel = next((c for c in data["channels"] if c.get("id") == channel_id), None)
            if channel is None or channel.get("type") != "telegram":
                raise ChannelError("找不到這個 Telegram 頻道。")
            if target not in channel.setdefault("targets", []):
                channel["targets"].append(target)
            channel["updated"] = _now()
            self._write(data)
            result = self.public(channel)
        self._telegram_changed()
        return result

    def import_config(self, settings: dict[str, Any], token: str, who: str = "") -> dict[str, Any]:
        """Move [channels.telegram] from the config file into the encrypted store."""
        return self.save({"type": "telegram", "name": "Telegram（由設定檔搬入）", "secret": token, "include_text": True,
                          "targets": [str(c) for c in settings.get("allowed_chats") or []],
                          "events": DEFAULT_EVENTS, "min_seconds": DEFAULT_MIN_SECONDS}, who)

    def _telegram_changed(self) -> None:
        if self.on_telegram_change is not None:
            try:
                self.on_telegram_change()
            except Exception as error:  # noqa: BLE001 - the save itself already succeeded
                print(f"[channels] telegram remount failed: {scrub(error)}", file=sys.stderr)

    def _record(self, channel_id: str, ok: bool, detail: str) -> None:
        with self.lock:
            data = self._read()
            channel = next((c for c in data["channels"] if c.get("id") == channel_id), None)
            if channel is None:
                return
            channel["last"] = {"at": _now(), "ok": ok, "detail": scrub(detail)[:240]}
            stats = channel.setdefault("stats", {"sent": 0, "failed": 0})
            stats["sent" if ok else "failed"] = int(stats.get("sent" if ok else "failed", 0)) + 1
            self._write(data)

    # --- sending ---
    def send(self, channel: dict[str, Any], text: str, title: str = "xHarness") -> tuple[bool, str]:
        """Deliver to every target of one channel; returns (all ok, detail)."""
        kind = channel["type"]
        try:
            secret = decrypt(channel["secret"])
            body = _clip(text, TEXT_LIMIT[kind])
            if kind == "telegram":
                targets = channel.get("targets") or []
                if not targets:
                    return False, "還沒有授權任何對象：先在 Telegram 傳訊息給機器人，再到頁面按「授權」。"
                for target in targets:
                    status, payload = self.post(f"https://api.telegram.org/bot{secret}/sendMessage",
                                                {"chat_id": int(target), "text": f"{title}\n{body}",
                                                 "disable_web_page_preview": True}, {})
                    if status != 200:
                        return False, _why(status, payload)
                return True, f"已送到 {len(targets)} 個對象"
            if kind == "line":
                for target in channel.get("targets") or []:
                    status, payload = self.post("https://api.line.me/v2/bot/message/push",
                                                {"to": target, "messages": [{"type": "text", "text": f"{title}\n{body}"}]},
                                                {"Authorization": f"Bearer {secret}"})
                    if status != 200:
                        return False, _why(status, payload)
                return True, f"已送到 {len(channel.get('targets') or [])} 個對象"
            card = {
                "type": "message",
                "attachments": [{
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
                            {"type": "TextBlock", "text": body, "wrap": True},
                        ],
                    },
                }],
            }
            status, payload = self.post(secret, card, {})
            if status not in (200, 202):
                return False, _why(status, payload)
            return True, "已送到 Teams 頻道"
        except ChannelError as error:
            return False, str(error)
        except Exception as error:  # noqa: BLE001 - a network failure is reported, never raised into a task
            return False, scrub(f"{type(error).__name__}: {error}")[:240]

    def test(self, channel_id: str) -> tuple[bool, str]:
        channel = self.get(channel_id)
        if channel is None:
            raise ChannelError("找不到這個頻道。")
        ok, detail = self.send(channel, "這是測試訊息。看到這則，代表通知設定正確。", "xHarness 測試通知")
        self._record(channel_id, ok, detail)
        return ok, detail

    def wants(self, channel: dict[str, Any], event: str, seconds: float | None) -> bool:
        if not channel.get("enabled", True) or event not in (channel.get("events") or []):
            return False
        if event == "done" and seconds is not None:
            return seconds >= int(channel.get("min_seconds", DEFAULT_MIN_SECONDS))
        return True

    def dispatch(self, event: str, title: str, text: str, *, detail: str = "", seconds: float | None = None,
                 skip_telegram: bool = False, approval: tuple[Any, str] | None = None,
                 wait: bool = False) -> int:
        """Fan an event out to every channel that asked for it, off the caller's thread.

        `text` always goes; `detail` (the answer, the error, the command waiting for
        approval) only to channels with include_text, since it leaves this machine.
        Returns how many channels it went to. `approval` is (conversation, approval id):
        a Telegram channel then gets Allow/Deny buttons instead of plain text.
        """
        chosen = [c for c in self.raw() if self.wants(c, event, seconds)
                  and not (skip_telegram and c.get("type") == "telegram")]

        def deliver(channel: dict[str, Any]) -> None:
            if approval is not None and channel["type"] == "telegram" and self.telegram_offer is not None:
                # Deciding needs the command itself, and these chats are the operator's own.
                conv, approval_id = approval
                reached = sum(bool(self.telegram_offer(int(t), conv, approval_id, f"{title}\n{detail}\n\n{text}"))
                              for t in channel.get("targets") or [])
                if reached:
                    self._record(channel["id"], True, f"已送核准按鈕到 {reached} 個對象")
                    return
            if detail and channel.get("include_text"):
                body = f"{detail}\n\n{text}"
            elif detail:
                body = f"（內容未附上，請到網頁查看）\n{text}"
            else:
                body = text
            ok, why = self.send(channel, body, title)
            self._record(channel["id"], ok, why)
            if not ok:
                print(f"[channels] {channel.get('name')}: {why}", file=sys.stderr)

        threads = [threading.Thread(target=deliver, args=(c,), name="xharness-channel", daemon=True) for c in chosen]
        for thread in threads:
            thread.start()
        if wait:
            for thread in threads:
                thread.join(HTTP_TIMEOUT * 2)
        return len(chosen)

