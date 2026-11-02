"""The web server as the resident agent's host: its conversations are ordinary
conversations (on the page, in the fleet view, under the same approval policy,
sandbox and token brake), and a write it wants to make waits for a person in a
conversation of its own -- approved on the page or with a Telegram button.

The main Session gets only its routing tools. Workers keep the node's work
tools minus what is not theirs: the desktop, notifications, other people's
conversations and the main Session's tools.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from ..session import harness_home
from ..tools import Tool
from .runtime import Superagent

#: Never given to a worker: the person's screen, outbound notices, and tools that reach other conversations.
WORKER_DENY = ("desktop_look", "desktop_act", "send_message", "list_agents", "interrupt_agent", "report", "discussion",
               "discussion_join", "hall_open", "schedule_add", "schedule_cancel")
TURN_LIMIT_SECONDS = 3600


class WebHandle:
    def __init__(self, app: Any, conv: Any) -> None:
        self.app = app
        self.conv = conv
        self.id = conv.id

    def run(self, text: str) -> str:
        conv = self.conv
        deadline = time.time() + TURN_LIMIT_SECONDS
        while True:
            start = len(conv.events)
            if self.app.send(conv, text):
                break
            with conv.cond:
                conv.cond.wait(timeout=5)
            if time.time() > deadline:
                raise TimeoutError("the conversation stayed busy")
        with conv.cond:
            while conv.running and time.time() < deadline:
                conv.cond.wait(timeout=5)
        for event in reversed(conv.events[start:]):
            if event.get("type") == "done":
                return str(event.get("answer") or "")
            if event.get("type") == "error":
                raise RuntimeError(str(event.get("message") or "the turn failed"))
        return ""

    def cancel(self) -> None:
        self.app.stop(self.conv)

    def recent_calls(self, n: int = 12) -> list[str]:
        calls = [f"{e.get('name')}:{str(e.get('args', ''))[:200]}" for e in self.conv.events if e.get("type") == "tool_start"]
        return calls[-n:]

    def transcript(self) -> str:
        lines = []
        for event in self.conv.events[-80:]:
            kind = event.get("type")
            if kind == "tool_start":
                lines.append(f"call {event.get('name')} {str(event.get('args', ''))[:300]}")
            elif kind == "tool_end":
                lines.append(f"result {'ok' if event.get('ok') else 'error'} {str(event.get('output', ''))[:300]}")
            elif kind in ("done", "user"):
                lines.append(f"{kind}: {str(event.get('answer') or event.get('text') or '')[:500]}")
        return "\n".join(lines)


class WebHost:
    def __init__(self, app: Any) -> None:
        self.app = app
        self._writes: Any = None
        self._judge: WebHandle | None = None
        self._lock = threading.Lock()

    def open_conversation(self, name: str, cwd: str, system: str, tools: list[Tool], model: str = "",
                          role: str = "worker") -> WebHandle:
        conv = self.app.create(extra_system=system, provider=model or None, cwd=cwd)
        registry = conv.harness.ctx.get("tools")
        if role == "main":
            registry.remove([t.name for t in registry.list()])
        else:
            registry.remove([t.name for t in registry.list() if t.name in WORKER_DENY or t.always_ask])
        for tool in tools:
            registry.register(tool)
        conv.channel = "superagent"
        self.app.set_title(conv, name[:80])
        return WebHandle(self.app, conv)

    def _writes_conversation(self) -> Any:
        with self._lock:
            if self._writes is None or self.app.get(self._writes.id) is None:
                self._writes = self.app.create(extra_system=None)
                self.app.set_title(self._writes, "常駐代理：對外寫入待核准")
            return self._writes

    def approve(self, summary: str) -> bool:
        from ..web import APPROVAL_TIMEOUT_SECONDS, Approval
        conv = self._writes_conversation()
        approval_id = secrets.token_hex(4)
        approval = Approval(summary=summary)
        conv.approvals[approval_id] = approval
        conv.push({"type": "approval_request", "id": approval_id, "call": 0, "summary": summary})
        self.app.announce(conv, "approval", summary, approval_id)
        approval.event.wait(APPROVAL_TIMEOUT_SECONDS)
        decision = bool(approval.decision)
        conv.push({"type": "approval_resolved", "id": approval_id, "allowed": decision})
        return decision

    def judge(self, question: str) -> str:
        """The Watchdog's own conversation: no tools, so it can only read what it is given and answer."""
        with self._lock:
            if self._judge is None or self.app.get(self._judge.id) is None:
                self._judge = self.open_conversation("常駐代理：看門狗", ".", "You check whether an agent's work makes progress. "
                                                     "You have no tools; answer from what you are given.", [], role="main")
        return self._judge.run(question)

    def telegram(self) -> Any:
        return self.app.telegram

    def notify(self, event: str, text: str) -> None:
        try:
            self.app.channels.dispatch("external", "常駐代理", text)
        except Exception:  # noqa: BLE001 - a notice must never stop the agent
            pass


def mount(app: Any) -> Superagent:
    agent = Superagent(harness_home(), WebHost(app))
    agent.start()
    return agent
