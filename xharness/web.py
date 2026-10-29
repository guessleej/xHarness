"""Local Web UI: a standard-library HTTP server that runs conversations in
threads and streams them to the browser with Server-Sent Events.

Security posture (see docs/ssdlc.md):
- binds 127.0.0.1 by default; binding elsewhere requires a bearer token
- every request must carry a loopback Host header unless a token is set
  (defeats DNS rebinding); every POST must carry X-XHarness-Client
  (defeats cross-site form posts; fetch with a custom header needs CORS,
  which is never granted)
- tokens travel only in the Authorization header; the SSE stream, which
  cannot set headers, uses a 60-second single-use ticket instead
"""

from __future__ import annotations

import difflib
import inspect
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .agent import Agent, AgentOptions
from .config import ResolvedConfig
from .context import Harness
from .fleet import forward, load_nodes, node_name, poll_all, poll_usage_all
from .identity import User, UserStore
from .usage import choose_bucket, history, parse_since
from .memory import MemoryStore, default_memory_dir
from .presets import build_harness
from .quota import QuotaGuard
from .redact import scrub
from . import multipart, workspace
from .report import collect as collect_report
from . import seccheck
from .sandbox import resolve_sandbox
from .commands import CommandHost, CommandResult, dispatch as dispatch_command, generate_title, listing as command_listing
from .commands import MAX_FEEDBACK_NOTE, providers_for, routing_on, switch_model
from .goal import run_goal
from .mentions import expand as expand_mentions, list_files
from .hall import HallRegistry
from .vision import describer_from_config, save_images
from .session import (SessionLog, clean_title, export_markdown, messages_from_events, search_sessions,
                      session_summary, sessions_dir)
from .telegram import TelegramBridge, load_token, telegram_settings
from .channels import EVENTS as CHANNEL_EVENTS, TYPES as CHANNEL_TYPES, ChannelError, ChannelHub, crypto_available
from .telemetry import BudgetExceeded

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
#: Callers that are not a signed-in person: the single-operator install with
#: no [auth] at all, and a hub forwarding an action with the node token.
OPERATOR = "operator"
NODE = "node"
APPROVAL_TIMEOUT_SECONDS = 600
#: Longer than an approval: answering takes thought, a yes/no does not.
QUESTION_TIMEOUT_SECONDS = 900
TICKET_TTL_SECONDS = 60
MAX_BODY_BYTES = 1_000_000
#: A message may carry up to four 5 MB images as base64; read only after the
#: caller is authenticated, so nobody unknown can make the server read 30 MB.
MAX_MESSAGE_BYTES = 30_000_000
CSRF_HEADER = "X-XHarness-Client"
#: Change previews: files larger than this are named, not diffed, and a diff
#: longer than this is cut. Enough to judge an edit, not a code review tool.
MAX_CHANGE_SOURCE_BYTES = 512_000
MAX_DIFF_LINES = 400
MAX_DIFF_LINE_CHARS = 400
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#bf181f"/>'
    '<path d="M19 19 L45 45 M45 19 L19 45" stroke="#ffffff" stroke-width="9" stroke-linecap="round"/>'
    "</svg>"
)


def preview_change(name: str, raw_args: str, cwd: str) -> dict[str, Any] | None:
    """What a write or edit is about to do to a file, as a unified diff.

    Called when the tool starts, before its approval: the person deciding sees
    the lines that will change, not just "write: notes.md". Returns None when
    there is nothing to show (another tool, bad arguments, or an edit whose
    old_string is not in the file -- the tool will refuse it anyway).
    """
    if name not in ("write", "edit"):
        return None
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        return None
    path = str(args.get("path") or "") if isinstance(args, dict) else ""
    if not path:
        return None
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    shown = os.path.relpath(full, cwd) if os.path.realpath(full).startswith(os.path.realpath(cwd) + os.sep) else full
    change: dict[str, Any] = {"path": shown, "created": False, "added": 0, "removed": 0, "diff": [], "truncated": False}
    try:
        if os.path.getsize(full) > MAX_CHANGE_SOURCE_BYTES:
            change["note"] = "檔案太大，不顯示差異"
            return change
        with open(full, encoding="utf-8", errors="replace") as handle:
            before = handle.read()
    except FileNotFoundError:
        before, change["created"] = "", True
    except OSError:
        return None
    if name == "write":
        after = str(args.get("content") or "")
    else:
        old, new = str(args.get("old_string") or ""), str(args.get("new_string") or "")
        if not old or old == new or old not in before:
            return None   # the tool refuses these; there is nothing to preview
        after = before.replace(old, new) if args.get("replace_all") else before.replace(old, new, 1)
    # [2:] drops the ---/+++ file headers; the path is shown separately.
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=3))[2:]
    change["added"] = sum(1 for line in lines if line.startswith("+"))
    change["removed"] = sum(1 for line in lines if line.startswith("-"))
    change["truncated"] = len(lines) > MAX_DIFF_LINES
    # The same masking as every other surface: a diff of an .env is still an .env.
    change["diff"] = [scrub(line[:MAX_DIFF_LINE_CHARS]) for line in lines[:MAX_DIFF_LINES]]
    return change


def is_admin(principal: Any) -> bool:
    """Operators and hubs see everything; among signed-in people only admins do."""
    return principal in (OPERATOR, NODE) or bool(getattr(principal, "is_admin", False))


def principal_name(principal: Any) -> str | None:
    """The user name to file work under, or None for a shared (unattributed) store."""
    return getattr(principal, "name", None)


@dataclass
class Approval:
    summary: str
    event: threading.Event = field(default_factory=threading.Event)
    decision: bool | None = None


@dataclass
class Question:
    """A question from the ask_user tool, waiting for the person."""

    question: str
    options: list[dict[str, str]]
    event: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None
    settled: bool = False

    def public(self, key: str) -> dict[str, Any]:
        return {"id": key, "question": self.question, "options": self.options}


class Conversation:
    def __init__(
        self,
        conv_id: str,
        harness: Harness,
        agent: Agent,
        session_id: str,
        user: str | None = None,
    ) -> None:
        self.id = conv_id
        self.harness = harness
        self.agent = agent
        self.session_id = session_id
        self.user = user
        self.events: list[dict[str, Any]] = []
        self.cond = threading.Condition()
        self.running = False
        self.approvals: dict[str, Approval] = {}
        self.questions: dict[str, Question] = {}
        self.preview = ""
        self.created = time.time()
        self.started_at: float | None = None
        self.last_activity = time.time()
        self.children: dict[str, str] = {}  # active subagents: name -> task
        self.turns = 0
        self.title = ""
        #: Requests sent so far, counting resumed history: a reply is rated by
        #: the number of the request it answered.
        self.requests = 0
        self.ratings: dict[int, str] = {}
        self.provider = ""
        #: How the person reached this server (scheme://host:port), for hall invites.
        self.origin = ""
        #: "telegram" when a Telegram chat drives it: that chat already sees every step.
        self.channel = ""

    def push(self, event: dict[str, Any]) -> None:
        with self.cond:
            event["seq"] = len(self.events) + 1
            self.events.append(event)
            self.last_activity = time.time()
            if event.get("type") == "tool_start":
                self.turns += 1
            self.cond.notify_all()

    def wait(self, after: int, timeout: float) -> list[dict[str, Any]]:
        with self.cond:
            if len(self.events) <= after:
                self.cond.wait(timeout)
            return self.events[after:]

    def usage(self) -> dict[str, Any] | None:
        telemetry = self.harness.ctx.optional("telemetry")
        return telemetry.report() if telemetry else None

    def summary(self) -> dict[str, Any]:
        pending = [
            {"id": key, "summary": approval.summary}
            for key, approval in self.approvals.items()
            if approval.decision is None
        ]
        asking = [q.public(key) for key, q in self.questions.items() if not q.settled]
        if self.running:
            state = "waiting" if (pending or asking) else "running"
        else:
            state = "idle"
        quota = self.harness.ctx.optional("quota")
        return {
            "id": self.id,
            "session": self.session_id,
            "user": self.user,
            "quota": quota.report() if quota is not None and quota.active else None,
            "running": self.running,
            "state": state,
            "preview": self.preview,
            "title": self.title,
            "provider": self.provider,
            "model": getattr(self.harness.ctx.optional("llm"), "model", ""),
            "requests": self.requests,
            "plan_mode": bool(getattr(self.agent, "plan_mode", False)),
            "goal": (self.harness.ctx.optional("goal").public() if self.harness.ctx.optional("goal") else None),
            "jobs": [job.public() for job in (self.harness.ctx.optional("jobs").jobs.values()
                                              if self.harness.ctx.optional("jobs") else [])],
            "reminders": [r.public() for r in (self.harness.ctx.optional("schedule").pending()
                                               if self.harness.ctx.optional("schedule") else [])],
            "ratings": {str(k): v for k, v in self.ratings.items()},
            "usage": self.usage(),
            "created": self.created,
            "started_at": self.started_at,
            "last_activity": self.last_activity,
            "elapsed": round(time.time() - self.started_at, 1) if self.running and self.started_at else None,
            "children": [{"name": name, "task": task[:120]} for name, task in self.children.items()],
            "agents": self.harness.ctx.optional("agents").public() if self.harness.ctx.optional("agents") else [],
            "tool_calls": self.turns,
            "pending_approvals": pending,
            "pending_questions": asking,
        }


class WebApp:
    """Conversation manager independent of HTTP, so it can be tested directly."""

    def __init__(
        self,
        config: ResolvedConfig,
        approval_mode: str = "prompt",
        harness_factory: Callable[..., Harness] | None = None,
    ) -> None:
        self.config = config
        self.approval_mode = approval_mode
        self.harness_factory = harness_factory or (
            lambda resume, user=None: build_harness(config, resume=resume, user=user)
        )
        # Tests and embedders may pass a one-argument factory; keep working with both.
        try:
            self._factory_takes_user = len(inspect.signature(self.harness_factory).parameters) >= 2
        except (TypeError, ValueError):
            self._factory_takes_user = False
        self.conversations: dict[str, Conversation] = {}
        self.lock = threading.Lock()
        self.tickets: dict[str, float] = {}
        self.node_name = node_name(config.fleet)
        self.halls = HallRegistry()
        self.nodes = {node.name: node for node in load_nodes(config.fleet)}
        self.telegram: TelegramBridge | None = None
        self._telegram_lock = threading.Lock()
        self.channels = ChannelHub()
        self.channels.on_telegram_change = self._mount_telegram
        self.channels.telegram_offer = self._offer_approval
        self.users = UserStore(getattr(config, "auth", {}) or {})
        self.file_rules = workspace.FileRules.from_config(getattr(config, "files", None))
        #: Recorded for the security self-check, which has to know how we are exposed.
        self.bound_host: str | None = None
        self.has_token = False

    def tick_reminders(self) -> int:
        """Send due reminders to idle conversations; returns how many fired."""
        with self.lock:
            convs = list(self.conversations.values())
        fired = 0
        for conv in convs:
            scheduler = conv.harness.ctx.optional("schedule")
            if scheduler is None or conv.running:
                continue
            due = scheduler.take_due()
            if not due:
                continue
            text = "\n".join(f"[提醒 {r.id}] {r.message}" for r in due)
            note = ("This request is a reminder you set earlier with schedule_add; nobody typed it just now. "
                    "Do what it says, then report briefly, in the language and script of the reminder text.")
            if self.send(conv, text, note=note):
                fired += len(due)
            else:   # became busy in between: put them back for the next tick
                for r in due:
                    scheduler.add(r.message, r.due, r.every_minutes, r.remaining, record=False)
        return fired

    def start_reminders(self, interval: float = 15.0) -> None:
        def loop() -> None:
            while not getattr(self, "_closing", False):
                try:
                    self.tick_reminders()
                except Exception as error:  # noqa: BLE001 - the ticker must outlive a bad reminder
                    print(f"[xharness] reminders: {scrub(error)}", file=sys.stderr)
                time.sleep(interval)

        threading.Thread(target=loop, name="xharness-reminders", daemon=True).start()

    def start_channels(self) -> None:
        """Mount the Telegram bot: the one saved on the web page wins over [channels.telegram].

        A bad [channels.telegram] in the config file is fatal on purpose; a bad saved
        channel is only logged, since an admin can fix it on the page.
        """
        if self.channels.telegram() is None:
            settings = telegram_settings(self.config)
            if settings:
                self.telegram = TelegramBridge(self, settings)
                self.telegram.start()
                print(f"telegram channel on for {len(self.telegram.allowed)} chat(s)", file=sys.stderr)
            return
        self._mount_telegram()

    def _mount_telegram(self) -> None:
        """(Re)start the bridge from the saved channel; called again after every edit to it."""
        with self._telegram_lock:
            old, self.telegram = self.telegram, None
            if old is not None:
                old.stop()
            try:
                settings = self.channels.telegram_settings()
            except ChannelError as error:
                print(f"[xharness] telegram channel not mounted: {error}", file=sys.stderr)
                return
            if not settings:
                return
            bridge = TelegramBridge(self, {**settings, "pairing": True})
            if old is not None:   # keep the chats' conversations and any pairing requests
                bridge.chats, bridge.strangers = old.chats, old.strangers
            bridge.start()
            self.telegram = bridge
            print(f"telegram channel (web) on for {len(bridge.allowed)} chat(s)", file=sys.stderr)

    def _offer_approval(self, chat: int, conv: Any, approval_id: str, text: str) -> bool:
        bridge = self.telegram
        return bool(bridge) and bridge.offer_approval(chat, conv, approval_id, text)

    def notify(self, text: str, chats: list[int] | None = None) -> int:
        """Push a message from another system; returns chats plus channels reached.

        With `chats`, only those Telegram chats of the mounted bot; otherwise every
        channel that asked for "external" (and the config-file bot's chats, if that
        is the bot in use).
        """
        bridge = self.telegram
        if chats is not None:
            return bridge.notify(text, chats) if bridge else 0
        reached = bridge.notify(text) if bridge and self.channels.telegram() is None else 0
        return reached + self.channels.dispatch("external", "xHarness 通知", text, wait=True)

    def channel_overview(self) -> dict[str, Any]:
        """The notification page: saved channels (never their secrets) and who asked to pair."""
        config_bot = telegram_settings(self.config)
        bridge = self.telegram
        strangers = ([s for s in sorted(bridge.strangers.values(), key=lambda s: -s["at"]) if s["chat"] not in bridge.allowed]
                     if bridge is not None and bridge.pairing else [])
        return {
            "available": crypto_available(),
            "types": CHANNEL_TYPES,
            "events": CHANNEL_EVENTS,
            "channels": self.channels.list_public(),
            "config_telegram": ({"chats": len(config_bot.get("allowed_chats") or [])}
                                if config_bot and self.channels.telegram() is None else None),
            "telegram_running": bridge is not None,
            "strangers": strangers,
        }

    def announce(self, conv: Conversation, event: str, detail: str, approval_id: str | None = None) -> int:
        """Tell the notification channels that a task finished, failed or is waiting for a person."""
        seconds = time.time() - conv.started_at if conv.started_at else None
        head = {"done": "任務完成", "error": "任務失敗", "approval": "需要你核准"}[event]
        subject = conv.title or conv.preview or "對話"
        lines = ["・".join(part for part in (f"節點 {self.node_name}", conv.user or "",
                                                f"{int(seconds)} 秒" if seconds is not None else "") if part)]
        url = self.host_url(conv)
        if url:
            lines.append(("到網頁核准：" if event == "approval" else "網頁：") + url)
        try:
            return self.channels.dispatch(event, f"{head}：{subject[:60]}", "\n".join(lines),
                                          detail=scrub(detail).strip()[:800] or "（沒有文字）", seconds=seconds,
                                          skip_telegram=conv.channel == "telegram",
                                          approval=(conv, approval_id) if approval_id else None)
        except Exception as error:  # noqa: BLE001 - a notification must never break the task
            print(f"[xharness] notification failed: {scrub(error)}", file=sys.stderr)
            return 0

    def create(
        self,
        resume: str | None = None,
        extra_system: str | None = None,
        user: User | None = None,
        provider: str | None = None,
    ) -> Conversation:
        if resume:
            # A session still open in a live conversation is that conversation: a second
            # one would append to the same log and the two transcripts would interleave.
            with self.lock:
                live = next((c for c in self.conversations.values() if c.session_id == resume
                             and c.user == principal_name(user)), None)
            if live is not None:
                return live
        harness = self.harness_factory(resume, user) if self._factory_takes_user else self.harness_factory(resume)
        session = harness.ctx.optional("session")
        session_id = session.id if session else "-"
        conv_id = secrets.token_hex(6)
        initial = messages_from_events(SessionLog.load(resume, principal_name(user))) if resume else []
        holder: dict[str, Conversation] = {}
        cwd = workspace.ensure(principal_name(user))
        describer = describer_from_config(self.config)
        #: Tool calls run one at a time, so the latest started call is the one
        #: an approval, a change preview or a tool_end belongs to.
        calls = {"current": 0}

        def tool_start(name: str, args: str) -> None:
            calls["current"] += 1
            conv = holder["conv"]
            conv.push({"type": "tool_start", "call": calls["current"], "name": name, "args": args[:2000]})
            try:
                # Without accounts there is no per-user workspace and the agent
                # works in the server's directory.
                change = preview_change(name, args, cwd or os.getcwd())
            except Exception as error:  # noqa: BLE001 - a preview is cosmetic and must never stop the task
                print(f"[xharness] change preview failed: {error}", file=sys.stderr)
                change = None
            if change:
                conv.push({"type": "change", "call": calls["current"], "tool": name, **change})

        def tool_end(name: str, output: str, is_error: bool) -> None:
            holder["conv"].push(
                {"type": "tool_end", "call": calls["current"], "name": name, "ok": not is_error, "output": output[:2000]}
            )

        def ask(question: str, options: list[dict[str, str]]) -> str | None:
            conv = holder["conv"]
            question_id = secrets.token_hex(4)
            pending = Question(question=question, options=options)
            conv.questions[question_id] = pending
            conv.push({"type": "question", **pending.public(question_id)})
            pending.event.wait(QUESTION_TIMEOUT_SECONDS)
            pending.settled = True
            conv.push({"type": "question_resolved", "id": question_id, "answer": pending.answer})
            return pending.answer

        def prompt(summary: str) -> bool:
            conv = holder["conv"]
            approval_id = secrets.token_hex(4)
            approval = Approval(summary=summary)
            conv.approvals[approval_id] = approval
            conv.push({"type": "approval_request", "id": approval_id, "call": calls["current"], "summary": summary})
            self.announce(conv, "approval", summary, approval_id)
            approval.event.wait(APPROVAL_TIMEOUT_SECONDS)
            decision = bool(approval.decision)
            conv.push({"type": "approval_resolved", "id": approval_id, "allowed": decision})
            return decision

        agent = Agent(
            harness.ctx,
            AgentOptions(
                cwd=cwd,
                # The operator's AGENTS.md lives where the server was started;
                # with per-user workspaces nobody would see it otherwise.
                site_instructions_dir=os.getcwd(),
                system_prompt=self.config.system_prompt,
                system_suffix=extra_system,
                max_turns=self.config.max_turns or AgentOptions.max_turns,
                approval_mode=self.approval_mode,
                project_instructions=self.config.project_instructions,
                prompt=prompt,
                ask=ask,
                initial_messages=initial,
                compaction=getattr(self.config, "compaction", None) or {"auto": True, "threshold": 0.8},
                describe_image=describer[1] if describer else None,
                describer_name=describer[0] if describer else "",
                on_delta=lambda text: holder["conv"].push({"type": "delta", "text": text}),
                on_tool_start=tool_start,
                on_tool_end=tool_end,
            ),
        )
        conv = Conversation(conv_id, harness, agent, session_id, principal_name(user))
        holder["conv"] = conv

        def child_start(payload: Any) -> None:
            conv.children[str(payload.get("name"))] = str(payload.get("task") or "")
            conv.push({"type": "subagent_start", "name": payload.get("name"), "task": str(payload.get("task") or "")[:200]})

        def child_end(payload: Any) -> None:
            conv.children.pop(str(payload.get("name")), None)
            conv.push({"type": "subagent_end", "name": payload.get("name"), "ok": bool(payload.get("ok"))})

        harness.ctx.on("subagent/start", child_start)
        harness.ctx.on("subagent/end", child_end)
        # Team-against-team halls: hosted here, joined from anywhere that can reach this address.
        harness.ctx.provide("halls", self.halls)
        harness.ctx.provide("host_url", lambda: self.host_url(conv))
        harness.ctx.provide("node_name", self.node_name)
        harness.ctx.on("router/decision", lambda info: conv.push({"type": "route", **info}))
        for state in ("invite", "joined"):
            harness.ctx.on(f"discussion/{state}", lambda info, state=state: conv.push({"type": "discussion", "state": state, **info}))
        live = harness.ctx.optional("agents")
        if live is not None:
            harness.ctx.on("agents/changed", lambda _info: conv.push({"type": "agents", "items": live.public()}))
        for state in ("start", "turn", "end"):
            harness.ctx.on(f"discussion/{state}", lambda info, state=state: conv.push(
                {"type": "discussion", "state": state, **{k: v for k, v in info.items() if k != "ts"}}))
        harness.ctx.on("jobs/changed", lambda job: conv.push({"type": "job", **job}))
        harness.ctx.on("workflow/start", lambda info: conv.push({"type": "workflow", "state": "start", "steps": info["steps"]}))
        harness.ctx.on("workflow/end", lambda info: conv.push({"type": "workflow", "state": "end", "ok": info["ok"]}))
        scheduler = harness.ctx.optional("schedule")
        if scheduler is not None:
            scheduler.deliverable = True    # this server delivers them (see tick_reminders)
            scheduler.on_change = lambda items: conv.push({"type": "reminders", "items": items})
            if resume:
                scheduler.restore(SessionLog.load(resume, principal_name(user)))
        harness.ctx.on("agent/compacted", lambda result: conv.push({
            "type": "compacted", "reason": result["reason"], "replaced": result["replaced"],
            "kept": result["kept"], "summary": result["summary"]}))
        conv.provider = "auto" if harness.ctx.optional("router") is not None else self.config.provider_name
        if provider and provider != conv.provider:
            outcome = switch_model(self._command_host(conv, user), provider)
            if not outcome.ok:
                raise ValueError(outcome.text)
            conv.provider = provider
        if initial:
            from .agent import SUMMARY_ACK, SUMMARY_PREFIX

            first = next((m for m in initial if m["role"] == "user" and m.get("content")
                          and not str(m["content"]).startswith(SUMMARY_PREFIX)), None)
            conv.preview = str(first["content"])[:80] if first else ""
            conv.title = session_summary(resume, principal_name(user))["title"] if resume else ""
            for message in initial:
                text = str(message.get("content") or "")
                if message["role"] not in ("user", "assistant") or not text or text == SUMMARY_ACK:
                    continue
                if text.startswith(SUMMARY_PREFIX):
                    conv.push({"type": "compacted", "reason": "history", "replaced": 0, "kept": 0,
                               "summary": text[len(SUMMARY_PREFIX):]})
                    continue
                if message["role"] == "user":
                    conv.requests += 1
                conv.push({"type": "history", "role": message["role"], "text": text,
                           "turn": conv.requests if message["role"] == "assistant" else None})
        with self.lock:
            self.conversations[conv_id] = conv
        return conv

    def get(self, conv_id: str) -> Conversation | None:
        with self.lock:
            return self.conversations.get(conv_id)

    def visible(self, conv: Conversation, viewer: Any) -> bool:
        """A signed-in person sees their own conversations; admins see everyone's."""
        if not self.users.enabled or is_admin(viewer):
            return True
        return conv.user == principal_name(viewer)

    def list(self, viewer: Any = OPERATOR) -> list[dict[str, Any]]:
        with self.lock:
            convs = sorted(self.conversations.values(), key=lambda c: c.created, reverse=True)
        return [conv.summary() for conv in convs if self.visible(conv, viewer)]

    def save_images(self, conv: Conversation, items: Any) -> list[str]:
        if not isinstance(items, list) or not items:
            return []
        return save_images([i for i in items if isinstance(i, dict)], conv.agent.options.cwd or os.getcwd())

    def send(self, conv: Conversation, text: str, images: list[str] | None = None,
             goal: dict[str, Any] | None = None, note: str | None = None) -> bool:
        with conv.cond:
            if conv.running:
                return False
            conv.running = True
            conv.started_at = time.time()
        conv.preview = text[:80]
        conv.requests += 1
        turn = conv.requests
        conv.push({"type": "user", "text": text, "turn": turn, "images": len(images or []), "goal": bool(goal),
                   "reminder": bool(note)})

        def run() -> None:
            try:
                cwd = conv.agent.options.cwd or os.getcwd()
                attached, files, pictures = expand_mentions(text, cwd)
                if files:
                    conv.push({"type": "attached", "files": files})
                if goal is not None:
                    answer = run_goal(conv.agent, text, int(goal.get("rounds") or 0) or 5, on_event=conv.push)
                else:
                    extra = "\n\n".join(part for part in (note, attached) if part) or None
                    answer = conv.agent.run(text, extra_context=extra, images=(images or []) + pictures)
                conv.push({"type": "done", "answer": answer, "usage": conv.usage(), "turn": turn})
                self.announce(conv, "done", answer)
                if not conv.title and (getattr(self.config, "sessions", None) or {}).get("auto_title", True):
                    self._name(conv, text)
            except Exception as error:  # noqa: BLE001 - surfaced to the browser
                # An exception often repeats the URL or header that failed, which
                # is exactly where a token would be.
                detail = scrub(f"{type(error).__name__}: {error}")
                conv.push({"type": "error", "message": detail, "usage": conv.usage()})
                self.announce(conv, "error", detail)
                session = conv.harness.ctx.optional("session")
                if session:
                    kind = "budget-stop" if isinstance(error, BudgetExceeded) else "error"
                    session.append({"type": kind, "message": detail[:500]})
            finally:
                with conv.cond:
                    conv.running = False
                    conv.children.clear()
                    conv.cond.notify_all()

        threading.Thread(target=run, name=f"xharness-web-{conv.id}", daemon=True).start()
        return True

    def _name(self, conv: Conversation, request: str) -> None:
        """Title the conversation from its first request, with its own model."""
        try:
            title = generate_title(conv.harness.ctx, request)
        except Exception as error:  # noqa: BLE001 - a title is a convenience
            print(f"[xharness] title failed: {scrub(error)}", file=sys.stderr)
            return
        if title and not conv.title:
            self.set_title(conv, title)

    def set_title(self, conv: Conversation, title: str) -> None:
        conv.title = clean_title(title)
        session = conv.harness.ctx.optional("session")
        if session:
            session.append({"type": "title", "title": conv.title})
        conv.push({"type": "title", "title": conv.title})

    def rate(self, conv: Conversation, turn: int | None, rating: str | None, note: str = "") -> int | None:
        """Rate the reply to request number `turn` (default: the latest). The
        rating is a record for people, kept out of what the model sees."""
        turn = turn or conv.requests
        if not turn or turn > conv.requests or rating not in ("up", "down", None):
            return None
        session = conv.harness.ctx.optional("session")
        if session:
            session.append({"type": "feedback", "turn": turn, "rating": rating, "note": str(note or "")[:MAX_FEEDBACK_NOTE]})
        if rating:
            conv.ratings[turn] = rating
        else:
            conv.ratings.pop(turn, None)
        return turn

    def _command_host(self, conv: Conversation, user: Any) -> CommandHost:
        allowed = tuple(getattr(user, "allowed_providers", ()) or ())
        return CommandHost(
            agent=conv.agent, ctx=conv.harness.ctx, config=self.config, running=conv.running,
            allowed_providers=allowed,
            model_switch=bool((getattr(self.config, "web", None) or {}).get("model_switch", True)),
            set_title=lambda title: self.set_title(conv, title),
            get_title=lambda: conv.title,
            rate=lambda rating, note: self.rate(conv, None, rating, note),
        )

    def command(self, conv: Conversation, line: str, user: Any) -> CommandResult | None:
        result = dispatch_command(self._command_host(conv, user), line, surface="web")
        if result is not None:
            session = conv.harness.ctx.optional("session")
            if session:
                session.append({"type": "command", "line": line[:200], "ok": result.ok})
            if result.ok and "plan_mode" in result.data:
                conv.push({"type": "plan", "on": result.data["plan_mode"]})
            if result.ok and result.data.get("provider"):
                conv.provider = result.data["provider"]
                conv.push({"type": "model", "provider": conv.provider, "model": result.data.get("model")})
        return result

    def models(self, user: Any) -> dict[str, Any]:
        allowed = tuple(getattr(user, "allowed_providers", ()) or ())
        return {
            "default": "auto" if routing_on(self.config) else self.config.provider_name,
            "switchable": bool((getattr(self.config, "web", None) or {}).get("model_switch", True)),
            "providers": providers_for(self.config, allowed),
            # A model that describes images for models that cannot see (see vision.py).
            "describer": str((getattr(self.config, "vision", None) or {}).get("provider") or ""),
        }

    def skills(self, user: Any) -> list[dict[str, str]]:
        from .skills import SkillStore

        store = SkillStore(getattr(self.config, "skills", None))
        return [{"name": s.name, "description": s.description, "source": s.source}
                for s in store.discover(workspace.ensure(principal_name(user)) or os.getcwd())]

    def host_url(self, conv: Conversation) -> str:
        """Where other conversations reach this node: [web] public_url, else how the person reached it."""
        configured = str((getattr(self.config, "web", None) or {}).get("public_url") or "").rstrip("/")
        return configured or conv.origin

    def roles(self, user: Any) -> list[dict[str, Any]]:
        from .roles import RoleStore

        store = RoleStore(getattr(self.config, "agents", None), providers=self.config.providers,
                          allowed=tuple(getattr(user, "allowed_providers", ()) or ()))
        return [r.public() for r in store.discover(workspace.ensure(principal_name(user)) or os.getcwd())]

    def agents_action(self, conv: Conversation, body: dict[str, Any]) -> int:
        """Stop one agent (stop: id), a discussion (room: id) or all of this conversation's agents."""
        live = conv.harness.ctx.optional("agents")
        if live is None:
            return 0
        if body.get("all"):
            return live.stop_all()
        if body.get("room"):
            return live.stop_room(str(body["room"]))
        return int(live.stop(str(body.get("stop") or "")))

    def approve(self, conv: Conversation, approval_id: str, allow: bool) -> bool:
        approval = conv.approvals.get(approval_id)
        if approval is None or approval.decision is not None:
            return False
        approval.decision = allow
        approval.event.set()
        return True

    def answer(self, conv: Conversation, question_id: str, text: str) -> bool:
        """Settle a pending question. The raw reply goes to the tool, which maps a
        bare option number to its label and caps the length."""
        pending = conv.questions.get(question_id)
        if pending is None or pending.settled or not str(text or "").strip():
            return False
        pending.answer = str(text)
        pending.settled = True
        pending.event.set()
        return True

    def stop(self, conv: Conversation) -> bool:
        """Operator brake: stop before the next model call, deny pending approvals
        and leave pending questions unanswered."""
        if not conv.running:
            return False
        conv.agent.stop()
        live = conv.harness.ctx.optional("agents")
        if live is not None:
            live.stop_all()     # children, workflow steps and discussion participants stop with it
        for approval in conv.approvals.values():
            if approval.decision is None:
                approval.decision = False
                approval.event.set()
        for pending in conv.questions.values():
            if not pending.settled:
                pending.settled = True
                pending.event.set()
        conv.push({"type": "stop_requested"})
        return True

    def fleet(self, include_nodes: bool = True, viewer: Any = OPERATOR) -> dict[str, Any]:
        items = self.list(viewer)
        model = str(self.config.provider["model"])
        for item in items:
            item["model"] = model
        usage_total = sum((item["usage"] or {}).get("total_tokens", 0) for item in items)
        report: dict[str, Any] = {
            "node": self.node_name,
            "version": __version__,
            "model": model,
            "summary": {
                "conversations": len(items),
                "running": sum(1 for item in items if item["state"] == "running"),
                "waiting": sum(1 for item in items if item["state"] == "waiting"),
                # Every agent working, not only subagents: workflow steps and discussion participants too.
                "children": sum(max(len(item["children"]), sum(1 for a in item.get("agents") or [] if a["status"] == "running"))
                                for item in items),
                "total_tokens": usage_total,
            },
            "items": items,
        }
        if include_nodes and self.nodes:
            report["nodes"] = poll_all(list(self.nodes.values()))
        return report

    def forward_action(self, node: str, conv_id: str, action: str, body: dict[str, Any]) -> tuple[int, Any]:
        target = self.nodes.get(node)
        if target is None:
            return 404, {"error": "no such node"}
        return forward(target, conv_id, action, body)

    def tools(self, user: Any = None) -> dict[str, Any]:
        """Every tool an agent here would get, grouped by source; the probe harness is
        built once. A restricted tier is shown its own list, not everyone's."""
        with self.lock:
            convs = list(self.conversations.values())
        harness = convs[0].harness if convs else getattr(self, "_probe", None)
        if harness is None:
            harness = self.harness_factory(None)
            self._probe = harness
        registry = harness.ctx.get("tools")
        items = [
            {
                "name": tool.name,
                "description": tool.description,
                "mutating": bool(tool.mutating),
                "source": "mcp" if tool.name.startswith("mcp__") else "builtin",
                "server": tool.name.split("__")[1] if tool.name.startswith("mcp__") and tool.name.count("__") >= 2 else None,
            }
            for tool in registry.list()
        ]
        servers = []
        for name in (getattr(self.config, "mcp_servers", None) or {}):
            count = sum(1 for item in items if item["server"] == name)
            servers.append({"name": name, "tools": count, "ok": count > 0})
        denied = set(getattr(user, "denied_tools", ()) or ())
        if denied:
            items = [item for item in items if item["name"] not in denied]
        sandbox = harness.ctx.optional("sandbox")
        runner = harness.ctx.optional("hooks")
        hooks = [{"event": h.event, "matcher": h.matcher or "*",
                  "source": "設定檔" if h.source == "config" else os.path.basename(h.source)}
                 for h in (runner.hooks if runner else [])]
        lsp = harness.ctx.optional("lsp")
        return {"tools": items, "mcp_servers": servers, "sandbox": sandbox.name if sandbox else None,
                "denied": sorted(denied), "hooks": hooks,
                "notify": bool(runner and runner.notify_argv),
                "language_servers": [{"name": n, "extensions": e} for n, _c, e in (lsp.servers if lsp else [])]}

    def memory_index(self) -> list[dict[str, Any]]:
        """Read-only view of what the agent remembers, straight from disk."""
        directory = str(self.config.memory.get("dir") or default_memory_dir())
        if not os.path.isdir(directory):
            return []
        store = MemoryStore(
            directory,
            stale_days=int(self.config.memory.get("stale_days", 90)),
            expire_days=int(self.config.memory.get("expire_days", 0)),
        )
        return [
            {
                "name": memory.name,
                "kind": memory.kind,
                "topic": memory.topic,
                "description": memory.description,
                "updated": memory.updated,
                "verified": memory.verified,
                "age_days": store.freshness(memory)[0],
                "stale": store.is_stale(memory),
            }
            for memory in store.list()
        ]

    def issue_ticket(self, owner: str | None = None) -> str:
        """Single-use, 60s credential for the SSE stream, which cannot set headers."""
        ticket = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.tickets = {key: value for key, value in self.tickets.items() if value[0] > now}
            self.tickets[ticket] = (now + TICKET_TTL_SECONDS, owner)
        return ticket

    def redeem_ticket(self, ticket: str) -> tuple[bool, str | None]:
        with self.lock:
            entry = self.tickets.pop(ticket, None)
        if entry is None or entry[0] <= time.time():
            return False, None
        return True, entry[1]

    def store_upload(self, user: Any, filename: str, data: bytes) -> dict[str, Any]:
        """Land one uploaded file in its owner's uploads directory and audit it."""
        name = principal_name(user)
        if not name:
            raise workspace.UploadRefused("這個伺服器沒有啟用帳號，無法接收上傳")
        workspace.ensure(name)
        record = workspace.store(name, filename, data, self.file_rules)
        self.users.audit("upload", name, True, f"{record['name']} ({record['bytes']} bytes)")
        return record

    def files(self, user: Any) -> dict[str, Any]:
        name = principal_name(user)
        items = workspace.listing(name) if name else []
        return {
            "files": items,
            "used_bytes": sum(item["bytes"] for item in items),
            "max_total_bytes": self.file_rules.max_total_bytes,
            "max_file_bytes": self.file_rules.max_file_bytes,
            "extensions": list(self.file_rules.extensions),
            "workspace": bool(name),
        }

    def delete_file(self, user: Any, filename: str) -> bool:
        name = principal_name(user)
        if not name:
            return False
        removed = workspace.remove(name, filename)
        if removed:
            self.users.audit("upload-delete", name, True, workspace.safe_name(filename))
        return removed

    def usage_report(self, since: str = "30d") -> dict[str, Any]:
        """The six-block service usage report; admin-only at the route level."""
        nodes = poll_all(list(self.nodes.values())) if self.nodes else []
        return collect_report(self.config, since=since, users=self.users, nodes=nodes)

    def security_check(self) -> dict[str, Any]:
        return seccheck.run(
            self.config,
            bound_host=self.bound_host,
            has_token=self.has_token,
            users=self.users,
        )

    def dispose(self) -> None:
        self._closing = True
        with self.lock:
            convs = list(self.conversations.values())
        for conv in convs:
            conv.harness.dispose()
        probe = getattr(self, "_probe", None)
        if probe is not None:
            probe.dispose()
        if self.telegram is not None:
            self.telegram.stop()


def make_handler(app: WebApp, token: str | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"xharness/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            # Polling GETs from the UI would flood the terminal; log writes and problems only.
            if self.command == "GET" and str(code).startswith("2"):
                return
            super().log_request(code, size)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            sys.stderr.write(f"[web] {self.address_string()} {format % args}\n")

        # --- helpers -------------------------------------------------------
        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            # Revalidate on every load: after an upgrade a cached page kept running the old interface.
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self) -> bool:
            raw = (self.headers.get("Host") or "").strip().lower()
            host = raw.split("]")[0] + "]" if raw.startswith("[") else raw.split(":")[0]
            return host in LOOPBACK_HOSTS

        def _bearer_ok(self) -> bool:
            header = self.headers.get("Authorization") or ""
            return header.startswith("Bearer ") and secrets.compare_digest(header[7:], token or "")

        def _presented(self) -> str:
            header = self.headers.get("Authorization") or ""
            return header[7:] if header.startswith("Bearer ") else ""

        def _guard(self, sse_ticket: str | None = None) -> bool:
            """Decide the caller and record it on self.principal, or answer 401/403."""
            self.principal: Any = None
            if app.users.enabled:
                presented = self._presented()
                user = app.users.resolve(presented) if presented else None
                if user is not None:
                    self.principal = user
                    return True
                if token and presented and secrets.compare_digest(presented, token):
                    self.principal = NODE  # a hub forwarding an action, not a person
                    return True
                if sse_ticket:
                    ok, owner = app.redeem_ticket(sse_ticket)
                    if ok:
                        self.principal = app.users.profile(owner) if owner else NODE
                        if self.principal is not None:
                            return True
                self._json(401, {"error": "unauthorized", "auth": "users"})
                return False
            if token:
                if self._bearer_ok():
                    self.principal = OPERATOR
                    return True
                if sse_ticket and app.redeem_ticket(sse_ticket)[0]:
                    self.principal = OPERATOR
                    return True
                self._json(401, {"error": "unauthorized", "auth": "token"})
                return False
            if not self._host_ok():
                self._json(403, {"error": "forbidden host"})
                return False
            self.principal = OPERATOR
            return True

        def _hall(self, method: str, hall_id: str, action: str, body: dict[str, Any], query: dict[str, list[str]]) -> None:
            """Halls authenticate with their own capabilities, not with this server's accounts:
            the invite's hall secret to join, then the team's own token. Nothing else is reachable this way."""
            auth = self.headers.get("Authorization") or ""
            bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            status, payload = app.halls.handle(method, hall_id, action, bearer, body, query)
            self._json(status, payload)

        def _admin_only(self) -> bool:
            if is_admin(getattr(self, "principal", None)):
                return True
            self._json(403, {"error": "administrator only"})
            return False

        def _body(self, limit: int = MAX_BODY_BYTES) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                self._json(413, {"error": "body too large"})
                return None
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {"error": "invalid JSON"})
                return None
            return data if isinstance(data, dict) else {}

        # --- routes --------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            url = urlsplit(self.path)
            parts = [p for p in url.path.split("/") if p]
            query = parse_qs(url.query)
            if not parts:
                # The loopback-Host check defeats DNS rebinding for an install with
                # no credential at all. A token or a sign-in is a stronger answer to
                # the same attack -- the page itself holds nothing, and every /api
                # route still demands one -- so requiring both would only mean a
                # node with accounts could never serve its own UI.
                if not token and not app.users.enabled and not self._host_ok():
                    self._json(403, {"error": "forbidden host"})
                    return
                self._html(INDEX_HTML)
                return
            if parts in (["favicon.svg"], ["favicon.ico"]):
                data = FAVICON_SVG.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            if parts[:1] != ["api"]:
                self._json(404, {"error": "not found"})
                return
            if len(parts) == 4 and parts[1] == "halls":
                self._hall("GET", parts[2], parts[3], {}, query)
                return
            ticket = (query.get("ticket") or [None])[0]
            if not self._guard(sse_ticket=ticket):
                return
            if parts[1:] == ["meta"]:
                try:
                    resolved = resolve_sandbox(getattr(app.config, "sandbox", None))
                    sandbox = resolved.name if resolved else None
                except (RuntimeError, ValueError):
                    sandbox = None
                principal = getattr(self, "principal", None)
                self._json(200, {
                    "version": __version__,
                    "model": app.config.provider["model"],
                    "base_url": app.config.provider["base_url"],
                    "approval": app.approval_mode,
                    "sandbox": sandbox,
                    "auth": bool(token),
                    "identity": app.users.enabled,
                    "user": principal.public() if isinstance(principal, User) else None,
                    "admin": is_admin(principal),
                    "node": app.node_name,
                })
                return
            if parts[1:] == ["conversations"]:
                self._json(200, app.list(self.principal))
                return
            if parts[1:] == ["sessions"]:
                owner = principal_name(self.principal)
                needle = (query.get("q") or [""])[0].strip()
                if needle:
                    self._json(200, search_sessions(needle[:100], owner))
                    return
                self._json(200, [
                    {**entry, **session_summary(entry["id"], owner)}
                    for entry in SessionLog.list(owner)[:50]
                ])
                return
            if len(parts) == 4 and parts[1] == "sessions" and parts[3] == "export":
                text = export_markdown(parts[2], principal_name(self.principal))
                if text is None:
                    self._json(404, {"error": "session not found"})
                    return
                data = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="xharness-{parts[2]}.md"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if parts[1:] == ["commands"]:
                self._json(200, command_listing("web"))
                return
            if parts[1:] == ["workspace", "files"]:
                root = workspace.ensure(principal_name(self.principal)) or os.getcwd()
                self._json(200, list_files(root, (query.get("q") or [""])[0][:100]))
                return
            if parts[1:] == ["models"]:
                self._json(200, app.models(self.principal))
                return
            if parts[1:] == ["skills"]:
                self._json(200, app.skills(self.principal))
                return
            if parts[1:] == ["agents", "roles"]:
                self._json(200, app.roles(self.principal))
                return
            if parts[1:] == ["me"]:
                principal = self.principal
                payload: dict[str, Any] = {
                    "user": principal.public() if isinstance(principal, User) else None,
                    "admin": is_admin(principal),
                    "identity": app.users.enabled,
                }
                if isinstance(principal, User) and (principal.quota_tokens_per_day or principal.quota_tokens_per_month):
                    # No telemetry here: this is the on-disk figure between tasks.
                    payload["quota"] = QuotaGuard(
                        principal.name,
                        principal.quota_tokens_per_day,
                        principal.quota_tokens_per_month,
                    ).report()
                self._json(200, payload)
                return
            if parts[1:] == ["usage-report"]:
                if not self._admin_only():
                    return
                self._json(200, app.usage_report((query.get("since") or ["30d"])[0]))
                return
            if parts[1:] == ["security-check"]:
                if not self._admin_only():
                    return
                self._json(200, app.security_check())
                return
            if parts[1:] == ["users"]:
                if not self._admin_only():
                    return
                self._json(200, {"users": app.users.accounts(), "backend": app.users.backend,
                                 "roles": app.users.roles(), "sessions": app.users.sessions()})
                return
            if parts[1:] == ["files"]:
                self._json(200, app.files(self.principal))
                return
            if parts[1:] == ["channels"]:
                if not self._admin_only():
                    return
                self._json(200, app.channel_overview())
                return
            if parts[1:] == ["access-audit"]:
                if not self._admin_only():
                    return
                self._json(200, app.users.audit_tail(int((query.get("limit") or ["100"])[0] or 100)))
                return
            if parts[1:] == ["memory"]:
                self._json(200, app.memory_index())
                return
            if parts[1:] == ["tools"]:
                self._json(200, app.tools(self.principal))
                return
            if parts[1:] == ["usage"]:
                since = (query.get("since") or ["7d"])[0]
                bucket = (query.get("bucket") or [None])[0]
                scope = None if is_admin(self.principal) else sessions_dir(principal_name(self.principal))
                self._json(200, history(directory=scope, since=since, bucket=bucket))
                return
            if parts[1:] == ["fleet", "usage"]:
                since = (query.get("since") or ["7d"])[0]
                bucket = (query.get("bucket") or [None])[0] or choose_bucket(parse_since(since))
                self._json(200, {
                    "node": app.node_name,
                    # Everyone's usage for an admin (and a hub); a signed-in person sees their own.
                    "history": history(directory=None if is_admin(self.principal) else sessions_dir(principal_name(self.principal)),
                                       since=since, bucket=bucket),
                    "nodes": poll_usage_all(list(app.nodes.values()), since, bucket) if app.nodes else [],
                })
                return
            if parts[1:] == ["fleet"]:
                # A hub asking us for our snapshot must not make us poll our own nodes
                # (X-XHarness-Client: hub), which would fan out recursively.
                self._json(200, app.fleet(
                    include_nodes=self.headers.get(CSRF_HEADER) != "hub",
                    viewer=self.principal,
                ))
                return
            if len(parts) == 4 and parts[1] == "conversations" and parts[3] == "events":
                conv = app.get(parts[2])
                if conv is None or not app.visible(conv, self.principal):
                    self._json(404, {"error": "no such conversation"})
                    return
                after = int((query.get("after") or ["0"])[0] or 0)
                last = self.headers.get("Last-Event-ID")
                if last and last.isdigit():
                    after = max(after, int(last))
                self._stream(conv, after)
                return
            self._json(404, {"error": "not found"})

        def _stream(self, conv: Conversation, after: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    events = conv.wait(after, timeout=15)
                    if not events:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    for event in events:
                        after = event["seq"]
                        payload = json.dumps(event, ensure_ascii=False)
                        self.wfile.write(f"id: {after}\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            if parts[:1] != ["api"]:
                self._json(404, {"error": "not found"})
                return
            if not self.headers.get(CSRF_HEADER):
                self._json(403, {"error": f"missing {CSRF_HEADER} header"})
                return
            # Uploads are multipart, not JSON, so they are handled before the
            # JSON body reader (which would reject them) and read with their
            # own, larger size limit.
            if parts[1:] == ["files"] and (self.headers.get("Content-Type") or "").lower().startswith("multipart/"):
                if not self._guard():
                    return
                self._upload()
                return
            is_message = len(parts) == 4 and parts[1] == "conversations" and parts[3] == "messages"
            if is_message and not self._guard():
                return
            body = self._body(MAX_MESSAGE_BYTES if is_message else MAX_BODY_BYTES)
            if body is None:
                return
            if parts[1:] == ["login"]:
                self._login(body)
                return
            if len(parts) == 4 and parts[1] == "halls":
                self._hall("POST", parts[2], parts[3], body, {})
                return
            if not is_message and not self._guard():
                return
            if parts[1:] == ["logout"]:
                app.users.revoke(self._presented())
                self._json(200, {"ok": True})
                return
            if parts[1:] == ["tickets"]:
                ticket = app.issue_ticket(principal_name(self.principal))
                self._json(200, {"ticket": ticket, "ttl": TICKET_TTL_SECONDS})
                return
            if parts[1:] == ["users"]:
                if not self._admin_only():
                    return
                self._manage_user(body)
                return
            if parts[1:] == ["channels"]:
                if not self._admin_only():
                    return
                self._manage_channel(body)
                return
            if parts[1:] == ["files"]:
                name = str(body.get("name") or "")
                if str(body.get("action") or "delete") != "delete" or not name:
                    self._json(400, {"error": "action must be delete, with a name"})
                    return
                ok = app.delete_file(self.principal, name)
                self._json(200 if ok else 404, {"ok": ok})
                return
            if parts[1:] == ["notify"]:
                text = str(body.get("text") or "").strip()
                if not text:
                    self._json(400, {"error": "text is required"})
                    return
                chats = body.get("chats")
                try:
                    reached = app.notify(text[:8000], [int(c) for c in chats] if isinstance(chats, list) else None)
                except Exception as error:  # noqa: BLE001 - channel failures are reported, not raised
                    self._json(502, {"error": f"{type(error).__name__}: {error}"})
                    return
                self._json(200 if reached else 503, {"reached": reached})
                return
            if parts[1:] == ["conversations"]:
                resume = body.get("resume")
                owner = self.principal if isinstance(self.principal, User) else None
                try:
                    conv = app.create(str(resume) if resume else None, user=owner,
                                      provider=str(body["provider"]) if body.get("provider") else None)
                except Exception as error:  # noqa: BLE001 - reported to the caller
                    self._json(400, {"error": str(error)})
                    return
                self._json(201, conv.summary())
                return
            if len(parts) == 4 and parts[1] == "conversations":
                conv = app.get(parts[2])
                if conv is None or not app.visible(conv, self.principal):
                    self._json(404, {"error": "no such conversation"})
                    return
                if parts[3] == "messages":
                    host = self.headers.get("Host") or ""
                    if host:
                        scheme = "https" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else "http"
                        conv.origin = f"{scheme}://{host}"
                    text = str(body.get("text") or "").strip()
                    if not text:
                        self._json(400, {"error": "text is required"})
                        return
                    goal = body.get("goal") if isinstance(body.get("goal"), dict) else None
                    try:
                        images = app.save_images(conv, body.get("images") or [])
                    except ValueError as error:
                        self._json(400, {"error": str(error)})
                        return
                    if not app.send(conv, text, images=images, goal=goal):
                        self._json(409, {"error": "conversation is busy"})
                        return
                    self._json(202, {"ok": True})
                    return
                if parts[3] == "approvals":
                    ok = app.approve(conv, str(body.get("id") or ""), bool(body.get("allow")))
                    self._json(200 if ok else 404, {"ok": ok})
                    return
                if parts[3] == "answers":
                    ok = app.answer(conv, str(body.get("id") or ""), str(body.get("answer") or ""))
                    self._json(200 if ok else 404, {"ok": ok})
                    return
                if parts[3] == "stop":
                    ok = app.stop(conv)
                    self._json(200 if ok else 409, {"ok": ok})
                    return
                if parts[3] == "commands":
                    result = app.command(conv, str(body.get("line") or ""), self.principal)
                    if result is None:
                        self._json(404, {"error": "unknown command"})
                        return
                    self._json(200, {"ok": result.ok, "text": result.text, "action": result.action, "data": result.data})
                    return
                if parts[3] == "model":
                    result = app.command(conv, "/model " + str(body.get("provider") or ""), self.principal)
                    ok = bool(result and result.ok)
                    self._json(200 if ok else 400, {"ok": ok, "text": result.text if result else "", **(result.data if result else {})})
                    return
                if parts[3] == "reminders":
                    scheduler = conv.harness.ctx.optional("schedule")
                    done = scheduler.cancel(str(body.get("cancel") or "")) if scheduler else None
                    self._json(200 if done else 404, {"ok": bool(done)})
                    return
                if parts[3] == "jobs":
                    manager = conv.harness.ctx.optional("jobs")
                    job = manager.stop(str(body.get("stop") or "")) if manager else None
                    self._json(200 if job else 404, {"ok": bool(job), "job": job.public() if job else None})
                    return
                if parts[3] == "agents":
                    count = app.agents_action(conv, body if isinstance(body, dict) else {})
                    self._json(200 if count else 404, {"ok": bool(count), "stopped": count})
                    return
                if parts[3] == "feedback":
                    rating = body.get("rating")
                    try:
                        turn = int(body.get("turn") or 0) or None
                    except (TypeError, ValueError):
                        turn = None
                    rated = app.rate(conv, turn, rating if rating in ("up", "down") else None, str(body.get("note") or ""))
                    self._json(200 if rated else 400, {"ok": bool(rated), "turn": rated})
                    return
            if len(parts) == 6 and parts[1] == "nodes" and parts[3] == "conversations":
                if not self._admin_only():  # steering another machine is an operator action
                    return
                status, payload = app.forward_action(parts[2], parts[4], parts[5], body)
                self._json(status, payload if payload is not None else {})
                return
            self._json(404, {"error": "not found"})

        def _upload(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            # One request may carry several files; allow the per-file limit plus
            # room for the encoding overhead, and refuse anything larger outright.
            ceiling = app.file_rules.max_file_bytes * 4 + 1_000_000
            if length <= 0 or length > ceiling:
                self._json(413, {"error": "上傳內容過大"})
                return
            raw = self.rfile.read(length)
            try:
                parts = multipart.files(multipart.parse(raw, self.headers.get("Content-Type") or ""))
            except multipart.MultipartError as error:
                self._json(400, {"error": f"上傳格式無法解析：{error}"})
                return
            if not parts:
                self._json(400, {"error": "沒有收到檔案"})
                return
            stored, refused = [], []
            for part in parts:
                try:
                    stored.append(app.store_upload(self.principal, part.filename or "upload", part.data))
                except workspace.UploadRefused as error:
                    refused.append({"name": part.filename, "reason": str(error)})
                except OSError as error:
                    refused.append({"name": part.filename, "reason": f"寫入失敗：{error}"})
            self._json(200 if stored else 400, {"stored": stored, "refused": refused})

        # --- identity routes ------------------------------------------
        def _login(self, body: dict[str, Any]) -> None:
            if not app.users.enabled:
                self._json(400, {"error": "this server does not use accounts"})
                return
            user, detail = app.users.authenticate(str(body.get("user") or ""), str(body.get("password") or ""))
            if user is None:
                # One message for every refusal: a different wording for "no such
                # user" would tell an attacker which names exist.
                self._json(401, {"error": detail if "try again" in detail else "登入失敗，請確認帳號與密碼"})
                return
            self._json(200, {"token": app.users.issue(user), "user": user.public()})

        def _manage_user(self, body: dict[str, Any]) -> None:
            action = str(body.get("action") or "add")
            name = str(body.get("user") or "")
            try:
                if action == "add":
                    created = app.users.add(
                        name,
                        str(body.get("password") or ""),
                        str(body.get("role") or "user"),
                        str(body.get("display") or ""),
                    )
                    self._json(201, created.public())
                    return
                if action == "password":
                    ok = app.users.set_password(name, str(body.get("password") or ""))
                    self._json(200 if ok else 404, {"ok": ok})
                    return
                if action == "disable":
                    ok = app.users.remove(name)
                    self._json(200 if ok else 404, {"ok": ok})
                    return
            except ValueError as error:
                self._json(400, {"error": str(error)})
                return
            self._json(400, {"error": "action must be add, password or disable"})

        def _manage_channel(self, body: dict[str, Any]) -> None:
            action = str(body.get("action") or "")
            channel_id = str(body.get("id") or "")
            who = principal_name(self.principal) or "operator"
            hub = app.channels
            try:
                if action == "save":
                    form = body.get("channel") if isinstance(body.get("channel"), dict) else {}
                    saved = hub.save({**form, "id": channel_id}, who)
                    app.users.audit("channel-save", who, True, f"{saved['type']} {saved['name']}")
                    self._json(200, saved)
                    return
                if action in ("enable", "disable"):
                    saved = hub.set_enabled(channel_id, action == "enable")
                    app.users.audit(f"channel-{action}", who, True, f"{saved['type']} {saved['name']}")
                    self._json(200, saved)
                    return
                if action == "delete":
                    gone = hub.get(channel_id)
                    ok = hub.delete(channel_id)
                    app.users.audit("channel-delete", who, ok, f"{gone['type']} {gone.get('name')}" if gone else channel_id)
                    self._json(200 if ok else 404, {"ok": ok})
                    return
                if action == "test":
                    ok, detail = hub.test(channel_id)
                    app.users.audit("channel-test", who, ok, detail)
                    self._json(200, {"ok": ok, "detail": detail})
                    return
                if action == "authorize":
                    saved = hub.add_target(channel_id, str(body.get("chat") or ""))
                    app.users.audit("channel-authorize", who, True, f"telegram chat {body.get('chat')}")
                    self._json(200, saved)
                    return
                if action == "import":
                    settings = telegram_settings(app.config)
                    if not settings or hub.telegram() is not None:
                        raise ChannelError("沒有可搬入的設定檔 Telegram，或網頁上已經有一個。")
                    saved = hub.import_config(settings, load_token(settings), who)
                    app.users.audit("channel-import", who, True, "telegram from config file")
                    self._json(200, saved)
                    return
            except (ChannelError, RuntimeError, OSError) as error:
                app.users.audit(f"channel-{action or 'unknown'}", who, False, str(error))
                self._json(400, {"error": scrub(error)})
                return
            self._json(400, {"error": "action must be save, enable, disable, delete, test, authorize or import"})

    return Handler


def start_server(
    config: ResolvedConfig,
    host: str = "127.0.0.1",
    port: int = 3080,
    token: str | None = None,
    approval_mode: str = "prompt",
    harness_factory: Callable[..., Harness] | None = None,
    app: WebApp | None = None,
) -> tuple[ThreadingHTTPServer, WebApp, str]:
    """Bind the server and mount channels without serving yet; `serve` and the desktop
    window share this. Pass `app` to put a second listener on an existing instance."""
    bare = host.split("%")[0]
    if bare == "::1":
        bare = "[::1]"
    # Accounts are an authentication mechanism in their own right: with [auth]
    # configured, every /api call still has to present a credential, so a node
    # that people sign in to does not additionally need a shared token.
    if bare not in LOOPBACK_HOSTS and not token and not (getattr(config, "auth", None) or {}):
        raise PermissionError(
            f"refusing to bind {host} without --token; a non-loopback address exposes the harness to the network "
            "(configure [auth] to require sign-in instead)"
        )
    if app is None:
        app = WebApp(config, approval_mode=approval_mode, harness_factory=harness_factory)
        app.bound_host = bare
        app.has_token = bool(token)
        app.start_channels()
        app.start_reminders()
    elif bare not in LOOPBACK_HOSTS:
        # A second listener on the same app (the desktop window also serving as a
        # fleet node): the security page must report the exposed binding, not the
        # loopback one the window uses.
        app.bound_host = bare
        app.has_token = bool(token)
    server = ThreadingHTTPServer((host, port), make_handler(app, token))
    server.daemon_threads = True
    url = f"http://{host}:{server.server_address[1]}/"
    return server, app, url


def serve(
    config: ResolvedConfig,
    host: str = "127.0.0.1",
    port: int = 3080,
    token: str | None = None,
    approval_mode: str = "prompt",
    open_browser: bool = True,
    harness_factory: Callable[..., Harness] | None = None,
) -> int:
    try:
        server, app, url = start_server(config, host, port, token, approval_mode, harness_factory)
    except Exception as error:  # noqa: BLE001 - bind refusals and channel misconfiguration are startup errors, shown plainly
        print(f"xharness: {error}", file=sys.stderr)
        return 2
    print(f"xHarness {__version__} web UI at {url}  (model: {config.provider['model']}, approval: {approval_mode})", file=sys.stderr)
    if token:
        print("bearer token required for /api; paste it into the UI when asked", file=sys.stderr)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.dispose()
    return 0


INDEX_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>xHarness</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<script>
/* Theme before first paint, so a dark-mode user never sees a white flash. */
(function(){var t=null;try{t=localStorage.getItem('xh-theme')}catch(e){}
if(!t&&window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches)t='dark';
document.documentElement.dataset.theme=t||'light';})();
</script>
<style>
/* ---- tokens: every colour on the page comes from here -------------------- */
:root{color-scheme:light;
  --brand:#bf181f;--brand-hi:#e0484f;--brand-dk:#9c1218;
  --bg:#f4f7f9;--surface:#ffffff;--surface-2:#eef2f5;--surface-3:#e3e9ee;
  --ink:#16202a;--ink-2:#3d4b56;--ink-3:#6b7a85;
  --tint:rgba(191,24,31,.08);--tint-2:rgba(191,24,31,.16);
  --ok:#2e7d32;--ok-tint:rgba(46,125,50,.12);--warn:#a85f05;--warn-tint:rgba(217,119,6,.13);
  --add-bg:rgba(46,125,50,.11);--add-ink:#1f6a24;--del-bg:rgba(191,24,31,.08);--del-ink:#9c1218;
  --sep:rgba(22,32,42,.07);
  --shadow:0 12px 32px rgba(22,32,42,.11);--shadow-sm:0 4px 14px rgba(22,32,42,.07);--shadow-xs:0 1px 3px rgba(22,32,42,.07);
  --topbar:rgba(244,247,249,.72);--topbar-scrolled:rgba(244,247,249,.94);
  --hero:linear-gradient(158deg,#0d1922,#13242f 56%,#1a3040);
  --code-bg:#0d1922;--code-ink:#dbe4ea;
  --font:"Microsoft JhengHei","PingFang TC","Noto Sans TC",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --top:58px;--col:880px;--ease:cubic-bezier(.22,.8,.3,1);
}
:root[data-theme="dark"]{color-scheme:dark;
  --brand:#e0484f;--brand-dk:#bf181f;
  --bg:#0e1a22;--surface:#15242e;--surface-2:#1b2d38;--surface-3:#233844;
  --ink:#e8eef3;--ink-2:#b7c3cc;--ink-3:#8593a0;
  --tint:rgba(224,72,79,.14);--tint-2:rgba(224,72,79,.24);
  --ok:#7bc47f;--ok-tint:rgba(46,125,50,.24);--warn:#e0a052;--warn-tint:rgba(217,119,6,.2);
  --add-bg:rgba(46,125,50,.22);--add-ink:#9ad99d;--del-bg:rgba(224,72,79,.17);--del-ink:#ffa2a5;
  --sep:rgba(255,255,255,.07);
  --shadow:0 12px 32px rgba(0,0,0,.4);--shadow-sm:0 4px 14px rgba(0,0,0,.3);--shadow-xs:0 1px 3px rgba(0,0,0,.35);
  --topbar:rgba(14,26,34,.7);--topbar-scrolled:rgba(14,26,34,.93);
  --code-bg:#08121a;
}
*{box-sizing:border-box}
/* `hidden` must win over any display set below (1.5.1 lost a day to this). */
[hidden]{display:none!important}
html,body{height:100%;margin:0}
body{font-family:var(--font);background:var(--bg);color:var(--ink);line-height:1.6;overflow:hidden;-webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit}
a{color:var(--brand)}
svg.i{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes drift{0%,100%{transform:translate3d(-4%,0,0) scale(1)}50%{transform:translate3d(6%,4%,0) scale(1.14)}}
@keyframes blink{50%{opacity:0}}

/* ---- top bar: glass, sticky, one row that never wraps --------------------- */
.top{position:fixed;inset:0 0 auto 0;height:var(--top);z-index:40;display:flex;align-items:center;gap:6px;padding:0 14px;
  background:var(--topbar);backdrop-filter:saturate(1.4) blur(16px);-webkit-backdrop-filter:saturate(1.4) blur(16px);
  transition:background .25s,box-shadow .25s}
.top[data-scrolled]{background:var(--topbar-scrolled);box-shadow:var(--shadow-sm)}
.iconbtn{position:relative;width:38px;height:38px;border-radius:11px;border:0;background:transparent;color:var(--ink-2);
  display:grid;place-items:center;cursor:pointer;flex:none;transition:background .2s,color .2s}
.iconbtn:hover{background:var(--tint);color:var(--ink)}
.iconbtn.on{background:var(--tint);color:var(--brand)}
.badge{position:absolute;top:2px;right:1px;min-width:17px;height:17px;padding:0 5px;border-radius:99px;background:var(--brand);
  color:#fff;font-size:11px;font-weight:700;line-height:17px;text-align:center}
.badge[hidden]{display:none}
.logo{display:flex;align-items:center;gap:9px;font-weight:800;font-size:17px;letter-spacing:.2px;color:var(--ink);text-decoration:none;margin:0 10px 0 4px;flex:none}
.logo .mark{width:28px;height:28px;border-radius:9px;background:var(--brand);display:grid;place-items:center}
.logo .mark svg{width:15px;height:15px;stroke:#fff;stroke-width:3.4;stroke-linecap:round;fill:none}
.tabs{display:flex;gap:2px;min-width:0}
.tab{font-size:14px;font-weight:600;padding:8px 13px;border-radius:10px;border:0;cursor:pointer;background:transparent;color:var(--ink-2);white-space:nowrap;
  display:flex;align-items:center;gap:7px;transition:background .2s,color .2s}
.tab:hover{background:var(--tint);color:var(--ink)}
.tab.on{background:var(--tint);color:var(--brand)}
.tab .cnt{font-size:11px;font-weight:700;background:var(--brand);color:#fff;border-radius:99px;padding:0 6px;line-height:17px}
.tab .cnt[hidden]{display:none}
.grow{flex:1}
.modelchip{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink-2);padding:6px 12px;border-radius:99px;background:var(--surface-2);
  max-width:280px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-right:4px}
.live{width:8px;height:8px;border-radius:50%;background:var(--ok);flex:none;box-shadow:0 0 0 3px var(--ok-tint)}
.who-wrap{position:relative;flex:none}
.whobtn{display:flex;align-items:center;gap:8px;font-size:14px;padding:4px 10px 4px 4px;border-radius:999px;border:0;cursor:pointer;background:transparent;white-space:nowrap}
.whobtn:hover{background:var(--tint)}
.avatar{width:30px;height:30px;border-radius:50%;background:var(--brand);color:#fff;display:grid;place-items:center;font-size:13px;font-weight:700;flex:none}
.menu{position:absolute;right:0;top:calc(100% + 8px);min-width:264px;background:var(--surface);border-radius:16px;box-shadow:var(--shadow);padding:8px;display:none;z-index:50}
.menu.open{display:block;animation:rise .2s var(--ease)}
.menu .mhead{padding:10px 12px 8px}
.menu .mname{font-weight:700}
.menu .mrole{font-size:13px;color:var(--ink-3)}
.menu .mrow{padding:7px 12px;font-size:13.5px;color:var(--ink-2);display:flex;justify-content:space-between;gap:14px}
.menu .mrow span:last-child{color:var(--ink);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:150px}
.menu .mitem{width:100%;display:flex;align-items:center;gap:10px;text-align:left;font-size:14px;padding:9px 12px;border-radius:10px;border:0;cursor:pointer;background:transparent}
.menu .mitem:hover{background:var(--tint)}
.menu .mitem[hidden]{display:none}
.menu .msep{height:1px;background:var(--sep);margin:6px 8px}
.meter{height:5px;border-radius:99px;background:var(--surface-3);overflow:hidden;margin-top:6px}
.meter i{display:block;height:100%;background:var(--brand);border-radius:99px}

/* ---- the app frame: drawer | work | review -------------------------------- */
.app{position:fixed;inset:var(--top) 0 0 0;display:flex}
/* Both panels float over the page as rounded cards and start closed: a docked
   full-height column is the fixed-sidebar layout this product does not use,
   and it left the page lopsided (panel on one side, empty band on the other). */
.drawer,.review{position:fixed;top:calc(var(--top) + 10px);bottom:12px;z-index:35;background:var(--surface);border-radius:20px;
  box-shadow:var(--shadow);display:flex;flex-direction:column;min-height:0;transition:transform .34s var(--ease),visibility .34s}
.drawer{left:12px;width:min(340px,calc(100vw - 24px));transform:translateX(calc(-100% - 24px));visibility:hidden}
.review{right:12px;width:min(480px,calc(100vw - 24px));transform:translateX(calc(100% + 24px));visibility:hidden}
body.drawer-open .drawer,body.review-open .review{transform:none;visibility:visible}
.work{flex:1;min-width:0;display:flex;min-height:0}
/* On a wide screen an open panel moves the column aside instead of covering it;
   the column stays centred in the space that is left. */
.conv{transition:padding .34s var(--ease)}
@media (min-width:1280px){
  body.drawer-open.view-chat .conv{padding-left:364px}
  body.review-open.view-chat .conv{padding-right:504px}
}
.pane-head{display:flex;align-items:center;gap:8px;padding:14px 14px 8px 18px}
.pane-head h2{margin:0;font-size:16px;flex:1}
.pane-body{flex:1;overflow-y:auto;padding:4px 12px 18px;min-height:0}

/* ---- drawer: tasks --------------------------------------------------------- */
.newtask{display:flex;align-items:center;gap:8px;margin:0 12px 10px;padding:10px 14px;border-radius:12px;border:0;cursor:pointer;
  background:var(--brand);color:#fff;font-weight:700;font-size:14px;box-shadow:var(--shadow-xs);transition:background .2s}
.newtask:hover{background:var(--brand-dk)}
.search{display:flex;align-items:center;gap:8px;margin:0 12px 6px;padding:8px 12px;border-radius:11px;background:var(--surface-2);color:var(--ink-3)}
.search input{flex:1;min-width:0;border:0;outline:none;background:transparent;font:inherit;font-size:14px;color:var(--ink)}
.grp{font-size:12px;font-weight:700;letter-spacing:.06em;color:var(--ink-3);padding:14px 10px 4px}
.task{display:flex;gap:10px;align-items:flex-start;padding:9px 10px;border-radius:11px;cursor:pointer;transition:background .15s}
.task:hover{background:var(--surface-2)}
.task.active{background:var(--tint)}
.task .dot{margin-top:8px}
.task .tt{font-size:14px;line-height:1.45;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.task .tm{font-size:12px;color:var(--ink-3)}
.task .tb{flex:1;min-width:0}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ink-3);opacity:.5;flex:none;display:inline-block}
.dot.running{background:var(--brand);opacity:1;animation:blink 1.1s steps(2) infinite}
.dot.waiting{background:var(--warn);opacity:1}
.dot.idle{background:var(--ok);opacity:1}
.muted{color:var(--ink-3);font-size:13.5px;padding:10px}

/* ---- conversation column ---------------------------------------------------- */
.conv{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0}
.conv-head{display:flex;align-items:center;gap:10px;padding:10px 24px;min-height:54px;width:100%;max-width:var(--col);margin:0 auto}
.conv-head .title{font-weight:700;font-size:15px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.conv-head .use{font-size:12.5px;color:var(--ink-3);white-space:nowrap}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:700;padding:3px 10px;border-radius:99px;white-space:nowrap;background:var(--surface-2);color:var(--ink-2)}
.pill.running{background:var(--tint);color:var(--brand)}
.pill.waiting{background:var(--warn-tint);color:var(--warn)}
.pill.idle{background:var(--ok-tint);color:var(--ok)}
.pill.error{background:var(--tint);color:var(--brand)}
.pill.applied{background:var(--ok-tint);color:var(--ok)}
.pill.pending{background:var(--warn-tint);color:var(--warn)}
.pill.skipped,.pill.denied{background:var(--surface-3);color:var(--ink-3)}
.ghostbtn{display:inline-flex;align-items:center;gap:7px;font-size:13.5px;font-weight:600;padding:7px 12px;border-radius:10px;border:0;cursor:pointer;
  background:transparent;color:var(--ink-2);white-space:nowrap;position:relative;transition:background .2s,color .2s}
.ghostbtn:hover{background:var(--tint);color:var(--ink)}
.ghostbtn.on{background:var(--tint);color:var(--brand)}
.ghostbtn .cnt{font-size:11px;font-weight:700;background:var(--ink-2);color:var(--surface);border-radius:99px;padding:0 6px;line-height:17px}
.ghostbtn.on .cnt,.ghostbtn .cnt.hot{background:var(--brand);color:#fff}
.scroller{flex:1;overflow-y:auto;min-height:0}
.thread{max-width:var(--col);margin:0 auto;padding:6px 24px 28px}
body.landing .thread{display:none}
body:not(.landing) .landing{display:none}

/* messages */
.m-user{display:flex;justify-content:flex-end;margin:20px 0 14px;animation:rise .35s var(--ease)}
.m-user>div{max-width:82%;display:flex;flex-direction:column;align-items:flex-end}
.m-user .bubble{max-width:100%;background:var(--surface-3);border-radius:18px 18px 6px 18px;padding:10px 15px;white-space:pre-wrap;word-break:break-word;font-size:15px}
.m-ai{margin:14px 0;font-size:15px;line-height:1.75;word-break:break-word;animation:rise .35s var(--ease)}
.m-ai>:first-child{margin-top:0}.m-ai>:last-child{margin-bottom:0}
.m-ai p{margin:.5em 0}
.m-ai h3,.m-ai h4,.m-ai h5,.m-ai h6{margin:1em 0 .4em;line-height:1.35}
.m-ai h3{font-size:18px}.m-ai h4{font-size:16px}.m-ai h5,.m-ai h6{font-size:15px}
.m-ai ul,.m-ai ol{margin:.4em 0;padding-left:1.4em}
.m-ai li{margin:.2em 0}
.m-ai code{font-family:var(--mono);font-size:.88em;background:var(--surface-2);padding:1px 6px;border-radius:6px}
.m-ai pre{background:var(--code-bg);color:var(--code-ink);border-radius:12px;padding:12px 14px;overflow:auto;margin:.7em 0;font:13px/1.6 var(--mono)}
.m-ai pre code{background:transparent;padding:0;font-size:inherit;color:inherit}
.tablewrap{overflow-x:auto;margin:.7em 0}
.m-ai table{border-collapse:collapse;font-size:14px;min-width:60%}
.m-ai th{text-align:left;font-weight:700;color:var(--ink-2);padding:7px 12px;background:var(--surface-2)}
.m-ai td{padding:7px 12px;border-bottom:1px solid var(--sep)}
.cursor{display:inline-block;width:8px;height:1.05em;vertical-align:-2px;background:var(--brand);animation:blink 1s steps(2) infinite;border-radius:2px;margin-left:2px}
.m-note{margin:12px 0;font-size:13.5px;color:var(--ink-3);display:flex;align-items:center;gap:8px;animation:rise .35s var(--ease)}
.m-error{margin:14px 0;padding:12px 15px;border-radius:14px;background:var(--tint);color:var(--brand);font-size:14px;white-space:pre-wrap;word-break:break-word}

/* steps: what the agent did, one line per tool call */
.steps{margin:12px 0;border-radius:15px;background:var(--surface);box-shadow:var(--shadow-xs);animation:rise .3s var(--ease)}
.steps-head{width:100%;display:flex;align-items:center;gap:10px;padding:10px 14px;border:0;background:transparent;font-size:13.5px;color:var(--ink-2);cursor:pointer;text-align:left}
.steps-head .chev{transition:transform .2s;color:var(--ink-3)}
.steps.closed .steps-head .chev{transform:rotate(-90deg)}
.steps-body{padding:0 6px 8px}
.steps.closed .steps-body{display:none}
.step-row{display:flex;align-items:center;gap:10px;padding:6px 8px;border-radius:10px;cursor:pointer;font-size:14px}
.step-row:hover{background:var(--surface-2)}
.ic{width:28px;height:28px;border-radius:9px;background:var(--surface-2);display:grid;place-items:center;color:var(--ink-2);flex:none}
.ic svg.i{width:15px;height:15px}
.step .lb{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}
.step .lb code{font-family:var(--mono);font-size:12.5px;color:var(--ink-2);margin-left:6px}
.step .tm{font-size:12px;color:var(--ink-3);white-space:nowrap}
.delta{font:12px var(--mono);white-space:nowrap}
.delta .a{color:var(--add-ink)}.delta .d{color:var(--del-ink);margin-left:6px}
.spin{width:14px;height:14px;border-radius:50%;border:2px solid var(--surface-3);border-top-color:var(--brand);animation:spin .8s linear infinite;flex:none}
.okmark{color:var(--ok)}.badmark{color:var(--brand)}
.step-detail{display:none;margin:2px 8px 10px 46px}
.step.open .step-detail{display:block}
.step-detail .cap{font-size:12px;color:var(--ink-3);margin:6px 0 4px}
.pre{background:var(--code-bg);color:var(--code-ink);border-radius:10px;padding:10px 12px;font:12.5px/1.55 var(--mono);white-space:pre-wrap;word-break:break-word;max-height:300px;overflow:auto;margin:0}

/* diffs */
.diff{font:12.5px/1.6 var(--mono);border-radius:11px;background:var(--surface-2);overflow:auto;max-height:340px}
.diff div{padding:0 12px;white-space:pre;min-width:max-content}
.diff .a{background:var(--add-bg);color:var(--add-ink)}
.diff .d{background:var(--del-bg);color:var(--del-ink)}
.diff .h{color:var(--ink-3);background:var(--surface-3)}
.diff .more{color:var(--ink-3);font-family:var(--font);padding:6px 12px}

/* approvals and questions: a tinted block that asks for a decision */
.ask{margin:14px 0;border-radius:16px;background:var(--warn-tint);padding:14px 16px;animation:rise .3s var(--ease)}
.ask .ask-t{display:flex;align-items:center;gap:8px;font-weight:700;font-size:14.5px;color:var(--warn)}
.ask .ask-s{margin-top:6px;font:13px/1.55 var(--mono);white-space:pre-wrap;word-break:break-word;color:var(--ink)}
.ask .ask-q{margin-top:6px;font-size:15px;white-space:pre-wrap;word-break:break-word;color:var(--ink)}
.ask .diff{margin-top:10px;max-height:240px;background:var(--surface)}
.ask .acts{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center}
.ask.done{background:var(--surface-2);padding:10px 14px}
.ask.done .ask-t{color:var(--ink-2);font-weight:600;font-size:13.5px}
.ask.done .diff,.ask.done .acts,.ask.done .qopts,.ask.done .qfree{display:none}
.ask.done.keep .qopts{display:grid}
.btn{display:inline-flex;align-items:center;gap:7px;font-size:14px;font-weight:600;padding:8px 15px;border-radius:11px;border:0;cursor:pointer;
  background:var(--surface);color:var(--ink);box-shadow:var(--shadow-xs);transition:background .2s,transform .15s}
.btn:hover{transform:translateY(-1px)}
.btn.primary{background:var(--brand);color:#fff}
.btn.primary:hover{background:var(--brand-dk)}
.btn.sm{padding:6px 11px;font-size:13px;border-radius:9px}
.btn.quiet{background:transparent;box-shadow:none;color:var(--ink-2)}
.btn.quiet:hover{background:var(--tint);color:var(--ink)}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.linkbtn{border:0;background:transparent;color:var(--ink-2);font-size:13px;cursor:pointer;padding:4px 6px;border-radius:7px}
.linkbtn:hover{color:var(--brand);background:var(--tint)}
.qopts{display:grid;gap:8px;margin-top:12px}
.qopt{display:flex;flex-direction:column;gap:2px;text-align:left;padding:10px 14px;border-radius:12px;border:0;cursor:pointer;background:var(--surface);
  box-shadow:var(--shadow-xs);transition:transform .15s}
.qopt:hover:not(:disabled){transform:translateY(-1px)}
.qopt:disabled{cursor:default;opacity:.55}
.qopt.chosen{opacity:1;background:var(--brand);color:#fff}
.qopt.chosen .qd{color:rgba(255,255,255,.85)}
.qopt .ql{font-weight:700;font-size:15px}
.qopt .qd{font-size:13px;color:var(--ink-3)}
.qfree{display:flex;gap:8px;margin-top:10px}
.qfree input,.field{flex:1;min-width:0;font:inherit;font-size:14px;padding:9px 12px;border-radius:10px;background:var(--surface);color:var(--ink);border:1px solid var(--surface-3);outline:none}
.qfree input:focus,.field:focus{border-color:var(--brand)}
.qerr{font-size:13px;color:var(--brand);margin-top:6px}

/* ---- composer ---------------------------------------------------------------- */
.composer-wrap{padding:0 24px 14px}
.composer{max-width:calc(var(--col) - 48px);margin:0 auto;background:var(--surface);border-radius:20px;box-shadow:var(--shadow);padding:8px 8px 8px 16px;transition:box-shadow .2s}
.composer.focus{box-shadow:0 0 0 3px var(--tint-2),var(--shadow)}
.composer.over{box-shadow:0 0 0 3px var(--brand),var(--shadow)}
.composer textarea{display:block;width:100%;border:0;outline:none;background:transparent;resize:none;font:inherit;font-size:15px;line-height:1.55;color:var(--ink);
  min-height:30px;max-height:220px;padding:8px 0 4px}
.composer textarea::placeholder{color:var(--ink-3)}
.cbar{display:flex;align-items:center;gap:6px;margin-top:2px}
.queued{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0}
.queued:empty{display:none}
.qfile{display:flex;align-items:center;gap:6px;font-size:12.5px;padding:3px 6px 3px 10px;border-radius:99px;background:var(--surface-2);color:var(--ink-2)}
.qfile button{border:0;background:transparent;color:var(--ink-3);cursor:pointer;width:20px;height:20px;border-radius:50%;display:grid;place-items:center}
.qfile button:hover{color:var(--brand);background:var(--tint)}
.modepill{font-size:12.5px;color:var(--ink-3);padding:4px 10px;border-radius:99px;background:var(--surface-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sendbtn{width:38px;height:38px;border-radius:12px;border:0;cursor:pointer;background:var(--brand);color:#fff;display:grid;place-items:center;flex:none;transition:background .2s,transform .15s}
.sendbtn:hover{background:var(--brand-dk)}
.sendbtn:disabled{opacity:.4;cursor:not-allowed}
.sendbtn.stop{background:var(--surface-3);color:var(--ink)}
.sendbtn.stop:hover{background:var(--tint-2)}
.sendbtn svg.i{width:17px;height:17px;stroke-width:2.2}
.hint{max-width:calc(var(--col) - 48px);margin:6px auto 0;font-size:12px;color:var(--ink-3);text-align:center;min-height:18px}

/* slash commands: a list above the composer, and their results in the thread */
.composer{position:relative}
.cmdpop{position:absolute;left:0;right:0;bottom:calc(100% + 8px);background:var(--surface);border-radius:16px;box-shadow:var(--shadow);padding:6px;
  max-height:300px;overflow-y:auto;z-index:30;animation:rise .18s var(--ease)}
.cmdpop .ci{display:flex;align-items:baseline;gap:10px;padding:8px 10px;border-radius:10px;cursor:pointer;font-size:14px}
.cmdpop .ci.on{background:var(--tint)}
.cmdpop .cn{font-family:var(--mono);font-weight:700;color:var(--brand);white-space:nowrap}
.cmdpop .ca{font-family:var(--mono);font-size:12.5px;color:var(--ink-3);white-space:nowrap}
.cmdpop .ch{color:var(--ink-2);font-size:13.5px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.m-cmd{margin:14px 0;border-radius:14px;background:var(--surface-2);padding:10px 14px;animation:rise .3s var(--ease)}
.m-cmd .cl{font:12.5px var(--mono);color:var(--ink-3);margin-bottom:4px}
.m-cmd .ct{white-space:pre-wrap;word-break:break-word;font-size:14px}
.m-cmd.bad .ct{color:var(--brand)}
.summary-box{margin:8px 0 0;font-size:14px;color:var(--ink-2);background:var(--surface-2);border-radius:12px;padding:10px 14px}
/* under each reply: copy and rate */
.replybar{display:flex;align-items:center;gap:2px;margin:-4px 0 8px;opacity:.75;transition:opacity .2s}
.replybar:hover,.replybar.rated{opacity:1}
.rb{width:30px;height:30px;border-radius:9px;border:0;background:transparent;color:var(--ink-3);display:grid;place-items:center;cursor:pointer}
.rb:hover{background:var(--surface-2);color:var(--ink)}
.rb.on{color:var(--brand);background:var(--tint)}
.rb svg.i{width:15px;height:15px}
.rnote{display:flex;gap:6px;flex:1;max-width:420px;margin-left:6px}
.rnote input{flex:1;min-width:0;font:inherit;font-size:13px;padding:5px 10px;border-radius:9px;background:var(--surface);color:var(--ink);border:1px solid var(--surface-3);outline:none}
.rnote input:focus{border-color:var(--brand)}
.rmsg{font-size:12.5px;color:var(--ink-3);margin-left:6px}
.modelsel{font:inherit;font-size:12.5px;color:var(--ink-2);background:var(--surface-2);border:0;border-radius:99px;padding:4px 26px 4px 10px;max-width:230px;cursor:pointer;
  appearance:none;-webkit-appearance:none;background-image:linear-gradient(45deg,transparent 50%,currentColor 50%),linear-gradient(135deg,currentColor 50%,transparent 50%);
  background-position:calc(100% - 13px) 52%,calc(100% - 9px) 52%;background-size:4px 4px;background-repeat:no-repeat}
.planbtn{font-size:12.5px;font-weight:600;padding:4px 11px;border-radius:99px;border:0;cursor:pointer;background:var(--surface-2);color:var(--ink-2);white-space:nowrap}
.planbtn:hover{background:var(--tint);color:var(--ink)}
.planbtn.on{background:var(--warn-tint);color:var(--warn)}
body.plan-on .composer{box-shadow:0 0 0 3px var(--warn-tint),var(--shadow)}
.goalwrap{position:sticky;top:0;z-index:5;max-width:var(--col);margin:0 auto;padding:0 24px}
.goalwrap:has(.goalbar[hidden]){display:none}
.goalbar{margin:8px 0;padding:10px 14px;border-radius:14px;background:var(--tint);display:flex;align-items:center;gap:10px;font-size:14px;backdrop-filter:blur(8px)}
.goalbar .gt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.goalbar .gr{font-size:12.5px;color:var(--brand);font-weight:700;white-space:nowrap}
.goalbar.done{background:var(--ok-tint)}.goalbar.done .gr{color:var(--ok)}
.goalbar.blocked,.goalbar.incomplete,.goalbar.stopped,.goalbar.failed{background:var(--warn-tint)}
.goalbar.blocked .gr,.goalbar.incomplete .gr,.goalbar.stopped .gr,.goalbar.failed .gr{color:var(--warn)}
.approve-plan{margin:2px 0 14px}
.imgchip{position:relative;width:52px;height:52px;border-radius:10px;overflow:hidden;background:var(--surface-2);flex:none}
.imgchip img{width:100%;height:100%;object-fit:cover;display:block}
.imgchip button{position:absolute;top:2px;right:2px;width:20px;height:20px;border-radius:50%;border:0;background:rgba(0,0,0,.55);color:#fff;display:grid;place-items:center;cursor:pointer}
.imgchip button svg.i{width:12px;height:12px}
.m-user .att{font-size:12.5px;color:var(--ink-3);text-align:right;margin-top:4px}
.job{background:var(--surface-2);border-radius:14px;padding:10px 12px;margin:10px 2px}
:root{--sp-1:#2563eb;--sp-2:#059669;--sp-3:#7c3aed;--sp-4:#ea580c;--sp-5:#0891b2;--sp-6:#db2777}
:root[data-theme="dark"]{--sp-1:#6b9cf5;--sp-2:#34c49a;--sp-3:#a78bfa;--sp-4:#f59a5c;--sp-5:#3fbfd9;--sp-6:#f06aa8}
.disc{background:var(--surface);border-radius:16px;box-shadow:var(--shadow-sm);margin:14px 0;overflow:hidden;animation:rise .35s var(--ease)}
.disc-h{display:flex;align-items:center;gap:10px;padding:12px 14px;background:var(--surface-2)}
.disc-h .ic{flex:none;width:30px;height:30px;border-radius:10px;display:grid;place-items:center;background:var(--tint);color:var(--brand)}
.disc-h .tt{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.disc-h .tt b{font-size:14.5px;line-height:1.4}
.disc-h .m,.dn .m{font-size:12px;color:var(--ink-3)}
.disc-body{padding:4px 14px 8px;max-height:560px;overflow:auto}
.dturn{display:flex;gap:10px;padding:10px 0}
.dturn+.dturn{border-top:1px solid var(--sep)}
.dturn.caucus{padding-left:14px;opacity:.86}
.dturn .av{flex:none;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:13px;
  color:var(--c);background:color-mix(in srgb,var(--c) 15%,transparent)}
.dturn .db{flex:1;min-width:0}
.dn{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13.5px}
.dn .tag{font-size:12px;font-weight:700;padding:1px 8px;border-radius:99px;background:var(--surface-2);color:var(--ink-2)}
.dn .tag.warn{background:var(--warn-tint);color:var(--warn)}
.dt.m-ai{margin:2px 0 0;font-size:14.5px;animation:none}
.disc-end{padding:12px 14px 14px;background:var(--surface-2)}
.disc-invite{padding:12px 14px;background:var(--warn-tint)}
.disc-invite .cl{font-size:12.5px;font-weight:700;color:var(--warn);margin-bottom:4px}
.disc-invite .m{font-size:13px;color:var(--ink-2);line-height:1.6}
.disc-invite .row{display:flex;align-items:center;gap:10px;margin:8px 0}
.disc-invite code{flex:1;min-width:0;font:12px var(--mono);background:var(--surface);border-radius:8px;padding:6px 8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.disc-end .cl{font-size:12px;font-weight:700;color:var(--ink-3);letter-spacing:.04em;margin-bottom:2px}
.job-h{display:flex;align-items:center;gap:8px}
.job-h .c{flex:1;min-width:0;font:13px var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.job .m{font-size:12px;color:var(--ink-3);margin-top:4px}
.pill.done{background:var(--ok-tint);color:var(--ok)}.pill.failed,.pill.timeout{background:var(--tint);color:var(--brand)}.pill.stopped{background:var(--surface-3);color:var(--ink-3)}
.task .snip{font-size:12.5px;color:var(--ink-2);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ---- landing: what to do when nothing has happened yet ---------------------- */
.landing{max-width:var(--col);margin:0 auto;padding:22px 24px 16px}
.hero{position:relative;overflow:hidden;border-radius:22px;background:var(--hero);color:#e8eef3;padding:28px 30px 24px;isolation:isolate;animation:rise .5s var(--ease)}
.hero .glow{position:absolute;inset:-60% -30% auto auto;width:520px;height:520px;border-radius:50%;z-index:-1;pointer-events:none;
  background:radial-gradient(closest-side,rgba(224,72,79,.26),transparent 70%);animation:drift 26s ease-in-out infinite}
.hero .glow.b{inset:auto auto -70% -20%;background:radial-gradient(closest-side,rgba(57,135,229,.16),transparent 70%);animation-duration:32s;animation-direction:reverse}
.hero .eyebrow{font-size:12.5px;letter-spacing:.08em;color:#ff9da1;font-weight:700}
.hero h1{margin:6px 0 6px;font-size:30px;line-height:1.25;color:#fff;letter-spacing:-.2px}
.hero p{margin:0;color:#b7c3cc;font-size:14.5px}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:20px}
.tile{background:rgba(255,255,255,.07);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border-radius:14px;padding:12px 14px;border:0;text-align:left;color:inherit;cursor:default}
button.tile{cursor:pointer;transition:background .2s}
button.tile:hover{background:rgba(255,255,255,.12)}
.tile .n{font-size:22px;font-weight:800;color:#fff;line-height:1.2}
.tile .l{font-size:12.5px;color:#9fb0bc}
.lgrid{display:grid;grid-template-columns:1.15fr 1fr;gap:14px;margin-top:16px}
.lcard{background:var(--surface);border-radius:18px;box-shadow:var(--shadow-sm);padding:14px 14px 10px;animation:rise .5s var(--ease) both}
.lcard.d1{animation-delay:.07s}.lcard.d2{animation-delay:.14s}
.lcard h2{margin:2px 6px 8px;font-size:14px;color:var(--ink-2)}
.start{display:flex;gap:12px;align-items:center;width:100%;text-align:left;padding:9px 8px;border-radius:12px;border:0;background:transparent;cursor:pointer;transition:background .15s}
.start:hover{background:var(--surface-2)}
.start .ic{background:var(--tint);color:var(--brand)}
.start .sk{font-size:12px;font-weight:700;color:var(--brand);display:block}
.start .sv{font-size:14px;display:block;line-height:1.45}

/* ---- review pane --------------------------------------------------------------- */
.seg{display:flex;gap:2px;background:var(--surface-2);border-radius:11px;padding:3px;margin:0 14px 8px}
.seg button{flex:1;border:0;background:transparent;padding:6px 8px;border-radius:9px;font-size:13px;font-weight:600;color:var(--ink-2);cursor:pointer;white-space:nowrap}
.seg button.on{background:var(--surface);color:var(--ink);box-shadow:var(--shadow-xs)}
.seg button .cnt{font-size:11px;color:var(--ink-3);margin-left:4px}
.chg{background:var(--surface-2);border-radius:14px;padding:10px 12px;margin:10px 2px}
.chg-h{display:flex;align-items:center;gap:8px;cursor:pointer}
.chg-h .p{flex:1;min-width:0;font:13px var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chg .diff{margin-top:8px;background:var(--surface)}
.chg.closed .diff,.chg.closed .note{display:none}
.chg .note{font-size:13px;color:var(--ink-3);margin-top:6px}
.empty{color:var(--ink-3);font-size:14px;line-height:1.7;padding:26px 14px;text-align:center}
.empty b{display:block;color:var(--ink-2);font-size:15px;margin-bottom:4px}
.row{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:11px}
.row:hover{background:var(--surface-2)}
.row .rn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}
.row .rm{font-size:12px;color:var(--ink-3);white-space:nowrap}
.tool{padding:9px 10px;border-radius:11px}
.tool .tn{font:13px var(--mono);display:flex;gap:8px;align-items:center}
.tool .td{font-size:12.5px;color:var(--ink-3);margin-top:2px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.tag{font-family:var(--font);font-size:11.5px;font-weight:700;padding:1px 8px;border-radius:99px;background:var(--warn-tint);color:var(--warn)}
.tag.ro{background:var(--surface-3);color:var(--ink-3)}
.sub{font-size:12px;font-weight:700;letter-spacing:.06em;color:var(--ink-3);padding:14px 10px 4px}
.stale{color:var(--warn)}

/* ---- full pages: fleet and admin -------------------------------------------- */
.page{flex:1;overflow-y:auto;min-height:0}
.page-in{max-width:1180px;margin:0 auto;padding:22px 24px 40px}
.page-head{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:14px}
.page-head h1{margin:0;font-size:26px;line-height:1.25}
.page-head p{margin:2px 0 0;color:var(--ink-3);font-size:14px}
.page-head .grow{min-width:20px}
body:not(.view-chat) .conv,body:not(.view-chat) .review{display:none}
body:not(.view-fleet) #fleet{display:none}
body:not(.view-admin) #admin{display:none}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:6px 0 18px}
.kpi{background:var(--surface);border-radius:16px;padding:14px 16px;box-shadow:var(--shadow-sm)}
.kpi.enter,.fc.enter{animation:rise .4s var(--ease) both}
.kpi .n{font-size:26px;font-weight:800;line-height:1.15}
.kpi .l{font-size:12.5px;color:var(--ink-3);margin-top:2px}
.kpi.hot .n{color:var(--warn)}
.sect{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:22px 0 10px}
.sect h2{margin:0;font-size:18px}
.sect .m{font-size:12.5px;color:var(--ink-3)}
.sect .down{color:var(--brand);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.fc{background:var(--surface);border-radius:18px;padding:16px 18px;box-shadow:var(--shadow-sm);display:flex;flex-direction:column;gap:8px}
.fc.current{background:var(--tint)}
.fc .head{display:flex;align-items:center;gap:8px}
.fc .prev{font-size:15px;line-height:1.45;max-height:4.3em;overflow:hidden}
.fc .meta{font-size:12px;color:var(--ink-3);display:flex;flex-wrap:wrap;gap:4px 12px}
.fc .kids{font-size:12px;color:var(--ink-2)}
.fc .kids span{display:inline-block;background:var(--surface-2);border-radius:999px;padding:2px 8px;margin:2px 4px 0 0}
.fc .appr{font-size:13px;background:var(--warn-tint);border-radius:11px;padding:8px 10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.fc .appr .s{flex:1 1 100%;font-family:var(--mono);word-break:break-word}
.fc .qbox{font-size:13px;background:var(--warn-tint);border-radius:11px;padding:8px 10px}
.fc .qbox .qq{font-weight:700;margin-bottom:6px}
.fc .qbox .qrow{display:flex;gap:6px;flex-wrap:wrap}
.fc .qbox input{font:inherit;font-size:13px;padding:5px 8px;border-radius:8px;flex:1;min-width:120px;background:var(--surface);color:var(--ink);border:1px solid var(--surface-3)}
.fc .acts{display:flex;gap:8px;margin-top:auto;flex-wrap:wrap}
.viz-root{--surface-1:#ffffff;--text-primary:#16202a;--text-secondary:#6b7a85;--grid:rgba(22,32,42,.08);
  --series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100;--series-5:#e87ba4;--series-6:#008300;--series-7:#4a3aa7;--series-8:#e34948;
  background:var(--surface-1);border-radius:18px;padding:14px 16px 10px;box-shadow:var(--shadow-sm)}
:root[data-theme="dark"] .viz-root{--surface-1:#15242e;--text-primary:#e8eef3;--text-secondary:#8593a0;--grid:rgba(255,255,255,.08);
  --series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;--series-6:#008300;--series-7:#9085e9;--series-8:#e66767}
.viz-filters{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.viz-chart{position:relative;width:100%;height:240px}
.viz-chart svg{width:100%;height:100%;display:block;font-family:var(--font)}
.viz-chart .grid line{stroke:var(--grid);stroke-width:1}
.viz-chart .axis text{fill:var(--text-secondary);font-size:12px}
.viz-chart .series path{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.viz-chart .series circle{stroke:var(--surface-1);stroke-width:2}
.viz-chart .crosshair{stroke:var(--text-secondary);stroke-width:1;stroke-dasharray:3 3;opacity:0}
.viz-tip{position:absolute;pointer-events:none;background:var(--surface);color:var(--ink);font-size:12px;line-height:1.5;padding:8px 10px;border-radius:10px;box-shadow:var(--shadow);opacity:0;transition:opacity .12s;min-width:140px}
.viz-tip .t{color:var(--text-secondary);margin-bottom:2px}
.viz-tip .r{display:flex;align-items:center;gap:6px}
.viz-tip .sw,.viz-legend .sw{width:10px;height:10px;border-radius:3px;display:inline-block}
.viz-legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:6px;font-size:13px;color:var(--text-secondary)}
.viz-legend .sw{margin-right:6px;vertical-align:-1px}
.viz-table table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
.viz-table th,.viz-table td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid);color:var(--text-primary)}
.viz-table th:first-child,.viz-table td:first-child{text-align:left;color:var(--text-secondary)}
.chk{display:flex;gap:14px;align-items:flex-start;padding:14px 16px;border-radius:14px;background:var(--surface);box-shadow:var(--shadow-xs);margin-bottom:8px}
.chk .bd{flex:1;min-width:0}
.chk .ti{font-weight:700;font-size:15px}
.chk .de{font-size:14px;color:var(--ink-2);margin-top:2px}
.chk .fx{font-size:13px;color:var(--ink-3);margin-top:6px;font-family:var(--mono);word-break:break-all}
.st{font-size:12.5px;padding:3px 11px;border-radius:999px;white-space:nowrap;font-weight:700}
.st.pass{background:var(--ok-tint);color:var(--ok)}
.st.warn{background:var(--warn-tint);color:var(--warn)}
.st.fail{background:var(--tint);color:var(--brand)}
.rep{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
.rep .b{background:var(--surface);border-radius:16px;padding:16px 18px;box-shadow:var(--shadow-sm)}
.rep .b h3{margin:0 0 8px;font-size:13.5px;color:var(--ink-3);font-weight:700}
.rep .b .n{font-size:24px;font-weight:800;color:var(--brand);line-height:1.2}
.rep .b .m{font-size:13px;color:var(--ink-2);margin-top:6px}
.tbl{width:100%;border-collapse:separate;border-spacing:0 6px;font-size:14px}
.tbl th{text-align:left;font-size:12.5px;color:var(--ink-3);padding:4px 12px;font-weight:700}
.tbl td{padding:10px 12px;background:var(--surface)}
.tbl tr td:first-child{border-radius:11px 0 0 11px}
.tbl tr td:last-child{border-radius:0 11px 11px 0;text-align:right}
.uform{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px}
.uform .field{flex:1 1 150px}
.uform select.field{flex:0 0 140px}
.err{font-size:13.5px;color:var(--brand);min-height:20px;margin:4px 0}
.admseg{display:inline-flex;margin:0}
.chgrid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.chgrid .empty{grid-column:1/-1}
.chcard{background:var(--surface);border-radius:16px;padding:16px 18px;box-shadow:var(--shadow-sm);display:flex;flex-direction:column;gap:7px}
.chcard.off{opacity:.72}
.chcard .hd{display:flex;align-items:center;gap:8px}
.chcard h3{margin:0;font-size:16px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chcard .ln{font-size:13.5px;color:var(--ink-2);word-break:break-word}
.chcard .ln.bad{color:var(--brand)}
.chcard .acts,.chform .acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.chform .acts{justify-content:flex-end;margin-top:10px}
.chpair{background:var(--surface-2);border-radius:12px;padding:10px 12px;font-size:13.5px;color:var(--ink-2);display:flex;flex-direction:column;gap:6px;align-items:flex-start}
.chpair .chprow{display:flex;align-items:center;gap:8px;width:100%}
.chcheck .bd .btn{margin-top:10px}
.chform{background:var(--surface);border-radius:18px;padding:18px 20px;box-shadow:var(--shadow-sm);margin-top:14px}
.chform h3{margin:0 0 4px;font-size:17px}
.chtypes{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));margin:10px 0 14px}
.chtype{text-align:left;border:0;cursor:pointer;background:var(--surface-2);border-radius:14px;padding:12px 14px;font:inherit;color:var(--ink);transition:background .2s,transform .15s}
.chtype:hover:not(:disabled){transform:translateY(-1px)}
.chtype:disabled{cursor:not-allowed;opacity:.6}
.chtype b{display:block;font-size:15px;margin-bottom:2px}.chtype span{font-size:13px;color:var(--ink-2)}
.chtype.on{background:var(--tint-2);box-shadow:var(--shadow-xs)}
.chhow{margin:0 0 16px;padding:12px 16px 12px 34px;background:var(--surface-2);border-radius:12px;font-size:13.5px;color:var(--ink-2);line-height:1.75}
.chrow{display:grid;grid-template-columns:150px 1fr;gap:6px 14px;align-items:start;margin-bottom:12px}
.chrow>label{font-size:13.5px;font-weight:700;color:var(--ink-2);padding-top:9px}
.chrow .field{width:100%;box-sizing:border-box}
.chrow .chhint{font-size:12.5px;color:var(--ink-3);margin-top:4px;line-height:1.6}
.chevents{display:flex;gap:6px 16px;flex-wrap:wrap;padding-top:8px;font-size:14px}
.chevents label,.chinc{display:flex;align-items:center;gap:6px;font-size:14px;padding-top:8px}
.chevents label{padding-top:0}
textarea.field{min-height:64px;resize:vertical;font-family:var(--mono);font-size:13px}

/* ---- sign-in gate: a dark full page, there is nothing behind it ------------- */
.gate{position:fixed;inset:0;z-index:90;display:none;place-items:center;background:var(--hero);padding:16px;isolation:isolate;overflow:hidden}
.gate.on{display:grid}
.gate .glow{position:absolute;width:640px;height:640px;border-radius:50%;inset:-20% auto auto -10%;z-index:-1;background:radial-gradient(closest-side,rgba(224,72,79,.24),transparent 70%);animation:drift 26s ease-in-out infinite}
.gate .card{width:min(420px,100%);background:var(--surface);border-radius:22px;padding:32px 34px;box-shadow:0 24px 60px rgba(0,0,0,.35);animation:rise .5s var(--ease)}
.gate .logo{margin:0 0 14px}
.gate p{margin:0 0 18px;color:var(--ink-3);font-size:14px}
.gate label{display:block;font-size:13px;color:var(--ink-2);margin:12px 0 6px}
.gate input{width:100%;font:inherit;padding:10px 12px;border-radius:11px;background:var(--surface);color:var(--ink);border:1px solid var(--surface-3);outline:none}
.gate input:focus{border-color:var(--brand)}
.gate .err{margin-top:10px}
.gate .btn{width:100%;justify-content:center;margin-top:12px;padding:11px}

/* ---- toasts and the confirm dialog -------------------------------------------- */
.toasts{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:95;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none}
.toast{background:var(--ink);color:var(--bg);font-size:14px;padding:10px 16px;border-radius:12px;box-shadow:var(--shadow);animation:rise .25s var(--ease);max-width:min(560px,92vw)}
.toast.bad{background:var(--brand);color:#fff}
.toast.update{pointer-events:auto;display:flex;align-items:center;gap:12px;position:fixed;top:calc(var(--top) + 10px);left:50%;transform:translateX(-50%);z-index:95}
.toast.update button{font:inherit;font-weight:700;border:0;border-radius:9px;padding:5px 12px;background:var(--brand);color:#fff;cursor:pointer}
.dlg-back{position:fixed;inset:0;z-index:92;background:rgba(8,16,22,.45);display:none;place-items:center;padding:16px}
.dlg-back.on{display:grid}
.dlg{width:min(420px,100%);background:var(--surface);border-radius:18px;box-shadow:var(--shadow);padding:22px 22px 18px;animation:rise .25s var(--ease)}
.dlg h3{margin:0 0 6px;font-size:17px}
.dlg p{margin:0;color:var(--ink-2);font-size:14px}
.dlg .acts{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
.btn.danger{background:var(--brand);color:#fff}

/* ---- narrow screens -------------------------------------------------------------- */
@media (max-width:899px){
  .modelchip{display:none}
}
@media (max-width:640px){
  .top{padding:0 8px;gap:2px}
  .logo{margin:0 4px}
  .logo .word{display:none}
  .tab{padding:8px 10px}
  #who-name{display:none}
  .whobtn{padding:4px}
  .conv-head{padding:8px 12px}
  .conv-head .use{display:none}
  .thread,.landing{padding-left:14px;padding-right:14px}
  .composer-wrap{padding:0 10px 10px}
  .hero{padding:22px 20px 18px}
  .hero h1{font-size:23px}
  .tiles{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
  .tile{padding:10px}
  .tile .n{font-size:18px}
  .lgrid{grid-template-columns:1fr}
  .page-in{padding:16px 14px 32px}
  .page-head h1{font-size:22px}
  .chrow{grid-template-columns:1fr}.chrow>label{padding-top:0}
  .m-user>div{max-width:92%}
  .modepill{display:none}
  .tbl{display:block;overflow-x:auto;white-space:nowrap}
  .chk{flex-direction:column;gap:8px}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
}
</style>
</head>
<body class="view-chat landing">
<div class="gate" id="gate"><span class="glow"></span><div class="card">
  <div class="logo"><span class="mark"><svg viewBox="0 0 16 16"><path d="M3 3 13 13M13 3 3 13"/></svg></span><span class="word">xHarness</span></div>
  <p id="gate-sub">請以你的帳號登入。</p>
  <form id="gate-form" autocomplete="on">
    <div id="gate-users">
      <label for="gate-user">帳號</label>
      <input id="gate-user" name="username" autocomplete="username" autocapitalize="off" spellcheck="false" required>
      <label for="gate-pass">密碼</label>
      <input id="gate-pass" name="password" type="password" autocomplete="current-password" required>
    </div>
    <div id="gate-token-box" hidden>
      <label for="gate-token">存取權杖</label>
      <input id="gate-token" name="token" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" required disabled>
    </div>
    <div class="err" id="gate-err"></div>
    <button class="btn primary" id="gate-go" type="submit">登入</button>
  </form>
</div></div>

<header class="top" id="top">
  <button class="iconbtn" id="btn-drawer" title="任務清單" aria-label="任務清單" aria-controls="drawer" aria-expanded="false">
    <svg class="i" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>
    <span class="badge" id="drawer-badge" hidden></span>
  </button>
  <a class="logo" href="#" id="logo"><span class="mark"><svg viewBox="0 0 16 16"><path d="M3 3 13 13M13 3 3 13"/></svg></span><span class="word">xHarness</span></a>
  <nav class="tabs" aria-label="主要">
    <button class="tab on" id="nav-chat">工作台</button>
    <button class="tab" id="nav-fleet">艦隊<span class="cnt" id="fleet-cnt" hidden></span></button>
    <button class="tab" id="nav-admin" hidden>管理</button>
  </nav>
  <span class="grow"></span>
  <span class="modelchip" id="modelchip" title="目前使用的模型"><span class="live"></span><span id="model-name">連線中…</span></span>
  <div class="who-wrap">
    <button class="whobtn" id="btn-who" aria-haspopup="true" aria-expanded="false">
      <span class="avatar" id="who-initial">X</span>
      <span id="who-name">本機操作者</span>
    </button>
    <div class="menu" id="who-menu" role="menu">
      <div class="mhead">
        <div class="mname" id="menu-name">本機操作者</div>
        <div class="mrole" id="menu-role">未啟用帳號</div>
      </div>
      <div class="msep"></div>
      <div class="mrow"><span>模型</span><span id="menu-model">-</span></div>
      <div class="mrow"><span>沙箱</span><span id="menu-sandbox">-</span></div>
      <div class="mrow"><span>審批</span><span id="menu-approval">-</span></div>
      <div class="mrow"><span>節點</span><span id="menu-node">-</span></div>
      <div class="mrow" id="menu-quota-row" hidden>
        <span style="flex:1">今日配額
          <div class="meter"><i id="menu-quota-bar" style="width:0%"></i></div>
        </span>
        <span id="menu-quota-text"></span>
      </div>
      <div class="msep"></div>
      <button class="mitem" id="btn-theme"><svg class="i" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg><span id="theme-label">關燈（切換成深色）</span></button>
      <button class="mitem" id="btn-logout" hidden><svg class="i" viewBox="0 0 24 24"><path d="M15 17l5-5-5-5M20 12H9M12 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7"/></svg>登出</button>
    </div>
  </div>
</header>

<div class="app">
  <aside class="drawer" id="drawer" aria-label="任務">
    <div class="pane-head"><h2>任務</h2>
      <button class="iconbtn" id="drawer-close" title="收起" aria-label="收起任務清單"><svg class="i" viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg></button></div>
    <button class="newtask" id="btn-newtask"><svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>新任務</button>
    <label class="search"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="task-filter" placeholder="搜尋任務" aria-label="搜尋任務" autocomplete="off"></label>
    <div class="pane-body" id="task-list"></div>
  </aside>

  <main class="work">
    <section class="conv" id="conv">
      <div class="conv-head">
        <span class="title" id="conv-title">新任務</span>
        <span class="pill" id="conv-state" hidden></span>
        <span class="use" id="conv-use"></span>
        <span class="grow"></span>
        <button class="ghostbtn" id="btn-export" title="把這段對話匯出成 Markdown" hidden>
          <svg class="i" viewBox="0 0 24 24"><path d="M12 4v12M7 11l5 5 5-5M4 20h16"/></svg>匯出</button>
        <button class="ghostbtn" id="btn-review" aria-controls="review" aria-expanded="false" title="檢視變更、檔案、工具與記憶">
          <svg class="i" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M14 4v16"/></svg>檢視<span class="cnt" id="review-cnt" hidden></span></button>
      </div>
      <div class="scroller" id="scroller">
        <div class="landing" id="landing">
          <div class="hero">
            <span class="glow"></span><span class="glow b"></span>
            <div class="eyebrow" id="hero-eyebrow">xHarness・管得住的 agent</div>
            <h1 id="hero-title">今天要交給 agent 什麼事？</h1>
            <p id="hero-sub">說清楚要做什麼，它會自己讀檔案、跑指令；要改東西之前一定先問你。</p>
            <div class="tiles">
              <button class="tile" id="tile-running"><div class="n" id="t-running">0</div><div class="l">進行中的任務</div></button>
              <button class="tile" id="tile-waiting"><div class="n" id="t-waiting">0</div><div class="l">等你回應</div></button>
              <div class="tile"><div class="n" id="t-tokens">-</div><div class="l">近 24 小時用量（tokens）</div></div>
            </div>
          </div>
          <div class="lgrid">
            <div class="lcard d1"><h2>從這裡開始</h2><div id="starters"></div></div>
            <div class="lcard d2"><h2>接續最近的任務</h2><div id="recent"></div></div>
          </div>
        </div>
        <div class="goalwrap"><div class="goalbar" id="goalbar" hidden></div></div>
        <div class="thread" id="transcript"></div>
      </div>
      <div class="composer-wrap">
        <div class="composer" id="composer">
          <div class="cmdpop" id="cmdpop" role="listbox" aria-label="指令" hidden></div>
          <div class="queued" id="queued"></div>
          <textarea id="input" rows="1" placeholder="交代一件事，或輸入 / 看指令（Enter 送出，Shift+Enter 換行）"></textarea>
          <div class="cbar">
            <button class="iconbtn" id="btn-attach" title="附加圖片或檔案（也可以直接貼上圖片）" aria-label="附加圖片或檔案">
              <svg class="i" viewBox="0 0 24 24"><path d="M21 11.5 12.5 20a5 5 0 0 1-7-7l8.5-8.5a3.3 3.3 0 0 1 4.7 4.7l-8.5 8.5a1.7 1.7 0 0 1-2.4-2.4l7.8-7.8"/></svg></button>
            <input type="file" id="file-input" multiple hidden>
            <button class="planbtn" id="btn-plan" type="button" title="計畫模式：模型只查看、提出計畫，你核准後才動手">計畫模式</button>
            <select class="modelsel" id="model-select" aria-label="這段對話使用的模型" title="這段對話使用的模型" hidden></select>
            <span class="modepill" id="modepill">有副作用的動作會先問你</span>
            <span class="grow"></span>
            <button class="sendbtn" id="btn-send" title="送出" aria-label="送出">
              <svg class="i" viewBox="0 0 24 24"><path d="M12 19V5M6 11l6-6 6 6"/></svg></button>
          </div>
        </div>
        <div class="hint" id="hint"></div>
      </div>
    </section>

    <aside class="review" id="review" aria-label="檢視">
      <div class="pane-head"><h2>檢視</h2>
        <button class="iconbtn" id="review-close" title="收起" aria-label="收起檢視"><svg class="i" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></button></div>
      <div class="seg" role="tablist">
        <button class="on" data-rv="changes">變更<span class="cnt" id="rv-changes-cnt"></span></button>
        <button data-rv="files">檔案</button>
        <button data-rv="jobs">背景與提醒<span class="cnt" id="rv-jobs-cnt"></span></button>
        <button data-rv="agents">代理<span class="cnt" id="rv-agents-cnt"></span></button>
        <button data-rv="tools">工具</button>
        <button data-rv="memory">記憶</button>
      </div>
      <div class="pane-body">
        <div id="rv-changes"></div>
        <div id="rv-files" hidden></div>
        <div id="rv-jobs" hidden></div>
        <div id="rv-agents" hidden></div>
        <div id="rv-tools" hidden></div>
        <div id="rv-memory" hidden></div>
      </div>
    </aside>

    <section class="page" id="fleet">
      <div class="page-in">
        <div class="page-head"><div><h1>艦隊</h1><p>每台機器上的任務一次看完：誰在跑、誰在等你、用了多少。可以直接在這裡審批或停止。</p></div></div>
        <div class="kpis" id="kpis"></div>
        <div class="sect" id="local-head"></div>
        <div class="grid" id="fleet-grid"></div>
        <div id="nodes"></div>
        <div class="sect"><h2>用量趨勢</h2><span class="m">每個節點的 token 用量；資料來自各節點磁碟上的 session 記錄</span></div>
        <div class="viz-root" id="usage">
          <div class="viz-filters">
            <div class="seg admseg">
              <button data-since="24h">24 小時</button>
              <button class="on" data-since="7d">7 天</button>
              <button data-since="30d">30 天</button>
            </div>
            <span class="grow"></span>
            <button class="btn sm quiet" id="usage-table-toggle">表格</button>
          </div>
          <div id="usage-chart" class="viz-chart"></div>
          <div id="usage-legend" class="viz-legend"></div>
          <div id="usage-table" class="viz-table" hidden></div>
        </div>
      </div>
    </section>

    <section class="page" id="admin">
      <div class="page-in">
        <div class="page-head"><div><h1>管理</h1><p>只有管理者看得到：這台機器的資安狀態、全體用量、帳號與通知頻道。</p></div>
          <span class="grow"></span>
          <div class="seg admseg" id="adm-seg">
            <button class="on" data-adm="sec">機密防護檢查</button>
            <button data-adm="rep">服務使用報告</button>
            <button data-adm="usr">帳號</button>
            <button data-adm="ch">通知頻道</button>
          </div></div>

        <div id="adm-sec">
          <div class="sect"><h2 id="sec-verdict">檢查中…</h2><span class="m">檢查機密放在哪裡、誰讀得到。本頁永遠不顯示機密內容。</span>
            <span class="grow"></span><button class="btn sm" id="sec-rerun">重新檢查</button></div>
          <div id="sec-list"></div>
        </div>

        <div id="adm-rep" hidden>
          <div class="sect"><h2>服務使用報告</h2><span class="m" id="rep-range">近 30 天</span><span class="grow"></span>
            <div class="seg admseg">
              <button data-since="7d">7 天</button>
              <button class="on" data-since="30d">30 天</button>
              <button data-since="90d">90 天</button>
            </div>
            <button class="btn sm" id="rep-json">下載 JSON</button></div>
          <div class="rep" id="rep-cards"></div>
          <div id="rep-detail"></div>
        </div>

        <div id="adm-usr" hidden>
          <div class="sect"><h2>帳號</h2><span class="m">停用不刪除，歷史用量才歸得了戶。</span></div>
          <form class="uform" id="usr-form">
            <input class="field" id="usr-name" placeholder="帳號" autocapitalize="off" spellcheck="false" required>
            <input class="field" id="usr-display" placeholder="顯示名稱">
            <input class="field" id="usr-pass" type="password" placeholder="密碼（至少 8 字）" autocomplete="new-password" required>
            <select class="field" id="usr-role"><option value="user">一般使用者</option><option value="admin">管理者</option></select>
            <button class="btn primary" type="submit">新增帳號</button>
          </form>
          <div class="err" id="usr-err"></div>
          <div id="usr-list"></div>
        </div>

        <div id="adm-ch" hidden>
          <div class="sect"><h2>通知頻道</h2><span class="m">任務完成、失敗或等你核准時，傳訊息到手機或群組。金鑰加密保存，存好之後本頁不再顯示。</span>
            <span class="grow"></span><button class="btn sm primary" id="ch-add">新增頻道</button></div>
          <div id="ch-alerts"></div>
          <div class="chgrid" id="ch-list"></div>
          <div class="chform" id="ch-form" hidden></div>
        </div>
      </div>
    </section>
  </main>
</div>

<div class="dlg-back" id="dlg"><div class="dlg" role="alertdialog" aria-modal="true" aria-labelledby="dlg-t">
  <h3 id="dlg-t"></h3><p id="dlg-p"></p>
  <div class="acts"><button class="btn quiet" id="dlg-no">取消</button><button class="btn danger" id="dlg-yes">確定</button></div>
</div></div>
<div class="toasts" id="toasts" aria-live="polite"></div>
<script>
(function(){
  const $=s=>document.querySelector(s);
  const transcript=$('#transcript'),input=$('#input'),hint=$('#hint'),scroller=$('#scroller');
  let conv=null,es=null,token=null,me=null,admin=false,gateOn=false,meta=null;
  let view='chat',convPreview='',convTitle='',convSession='',convState='idle',running=false;
  let commands=[],models={providers:[],switchable:false},pendingProvider='',searchHits=null,lastAiEl=null,knownRatings={};
  let planOn=false,turnWasPlan=false,jobs={},pendingImages=[],reminders=[],agents=[],roles=null,discs={},lastRoute='';
  let assistantEl=null,assistantText='',stepsBox=null,composing=false;
  const rows={};          // call id -> step row
  let changes=[];         // change previews for the open conversation
  let liveConvs=[],savedSessions=[];

  // ---- small helpers -------------------------------------------------------
  function h(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
  const ICONS={
    eye:'<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    pen:'<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    folder:'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    term:'<path d="m4 17 6-5-6-5M12 19h8"/>',
    globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    list:'<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
    shield:'<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6Z"/>',
    help:'<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14M12 17h.01"/>',
    users:'<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M21.5 20a6.5 6.5 0 0 0-4-6"/>',
    bookmark:'<path d="M6 3h12v18l-6-4-6 4Z"/>',
    plug:'<path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0ZM12 17v5"/>',
    tool:'<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4Z"/>',
    check:'<path d="M5 12.5 10 17 19 7"/>',
    x:'<path d="M6 6l12 12M18 6 6 18"/>',
    chev:'<path d="m6 9 6 6 6-6"/>',
    warn:'<path d="M12 3 2 20h20Z"/><path d="M12 10v4M12 17h.01"/>',
    send:'<path d="M12 19V5M6 11l6-6 6 6"/>',
    stop:'<rect x="7" y="7" width="10" height="10" rx="2"/>',
    file:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/>',
    upload:'<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
    up:'<path d="M7 10v11"/><path d="M15 5.9 14 10h5.8a2 2 0 0 1 1.9 2.6l-2.3 7.9A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.8a2 2 0 0 0 1.8-1.1L12 2a3.1 3.1 0 0 1 3 3.9Z"/>',
    down:'<path d="M17 14V3"/><path d="M9 18.1 10 14H4.2a2 2 0 0 1-1.9-2.6l2.3-7.9A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.8a2 2 0 0 0-1.8 1.1L12 22a3.1 3.1 0 0 1-3-3.9Z"/>',
    copy:'<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    note:'<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    layers:'<path d="m12 2 10 5-10 5L2 7Z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/>'
  };
  function icon(name){const s=document.createElementNS('http://www.w3.org/2000/svg','svg');s.setAttribute('class','i');s.setAttribute('viewBox','0 0 24 24');
    s.innerHTML=ICONS[name]||ICONS.tool;return s;}   // fixed strings from this file only
  function toast(text,bad){const t=h('div','toast'+(bad?' bad':''),text);$('#toasts').appendChild(t);setTimeout(()=>t.remove(),bad?6000:3200);}
  // http:// pages have no navigator.clipboard; fall back to a hidden textarea.
  async function copyText(text){
    try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);return true;}}catch(e){}
    const t=document.createElement('textarea');t.value=text;t.setAttribute('readonly','');t.style.position='fixed';t.style.opacity='0';
    document.body.appendChild(t);t.select();let ok=false;try{ok=document.execCommand('copy');}catch(e){}t.remove();return ok;
  }
  function askConfirm(title,body,yes){
    return new Promise(resolve=>{
      $('#dlg-t').textContent=title;$('#dlg-p').textContent=body;$('#dlg-yes').textContent=yes||'確定';$('#dlg').classList.add('on');
      const done=v=>{$('#dlg').classList.remove('on');$('#dlg-yes').onclick=$('#dlg-no').onclick=null;resolve(v);};
      $('#dlg-yes').onclick=()=>done(true);$('#dlg-no').onclick=()=>done(false);
      $('#dlg-yes').focus();
    });
  }
  const KB=1024,MB=KB*1024;
  function humanSize(n){return n>=MB?(n/MB).toFixed(1)+' MB':n>=KB?Math.round(n/KB)+' KB':n+' B';}
  function fmtNum(n){return (n||0).toLocaleString('zh-TW');}
  function ago(ts){if(!ts)return '';const s=Math.max(0,Date.now()/1000-ts);
    if(s<60)return '剛剛';if(s<3600)return Math.floor(s/60)+' 分鐘前';if(s<86400)return Math.floor(s/3600)+' 小時前';
    const d=new Date(ts*1000);return (d.getMonth()+1)+'/'+d.getDate();}
  function stickBottom(){const near=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<160;return near;}
  function toBottom(force){if(force||stickBottom())scroller.scrollTop=scroller.scrollHeight;}

  // ---- theme and the glass top bar -------------------------------------------
  const root=document.documentElement;
  function themeLabel(){$('#theme-label').textContent=root.dataset.theme==='dark'?'開燈（切換成淺色）':'關燈（切換成深色）';}
  themeLabel();
  $('#btn-theme').onclick=()=>{const next=root.dataset.theme==='dark'?'light':'dark';root.dataset.theme=next;try{localStorage.setItem('xh-theme',next)}catch(e){}themeLabel();};
  // The scrolling happens inside the panes, not the window: listen in the capture phase.
  document.addEventListener('scroll',e=>{const t=e.target;if(t&&t.scrollTop!=null)$('#top').toggleAttribute('data-scrolled',t.scrollTop>20);},true);

  // ---- api --------------------------------------------------------------------
  async function api(path,opts){
    opts=opts||{};const headers=Object.assign({'Content-Type':'application/json','X-XHarness-Client':'1'},opts.headers||{});
    if(token)headers['Authorization']='Bearer '+token;
    const r=await fetch(path,{method:opts.method||'GET',headers,body:opts.body?JSON.stringify(opts.body):undefined});
    if(r.status===401){
      // Every credential request is the gate, shown once; a native box per
      // failing request used to stack two on page load.
      let d={};try{d=await r.json()}catch(e){}
      token=null;saveToken();   // whatever was sent is missing or no longer valid
      showGate(true,d.auth==='users'?'users':'token');
      throw new Error(d.auth==='users'?'請先登入':'需要存取權杖');
    }
    if(!r.ok){let d={};try{d=await r.json()}catch(e){}throw new Error(d.error||('HTTP '+r.status));}
    return r.status===204?null:r.json();
  }

  // ---- markdown, built as DOM nodes (model output never becomes HTML) ----------
  function inline(parent,text){
    const re=/(`[^`]+`|\*\*[^*]+\*\*)/g;let last=0,m;
    while((m=re.exec(text))){
      if(m.index>last)parent.appendChild(document.createTextNode(text.slice(last,m.index)));
      const t=m[0],code=t[0]==='`';parent.appendChild(h(code?'code':'strong',null,code?t.slice(1,-1):t.slice(2,-2)));last=re.lastIndex;
    }
    if(last<text.length)parent.appendChild(document.createTextNode(text.slice(last)));
  }
  const LIST=/^\s*([-*+]|\d+[.)])\s+/,TROW=/^\s*\|.*\|\s*$/,TSEP=/^\s*\|[\s:|-]+\|\s*$/;
  function cells(line){return line.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());}
  function md(src){
    const frag=document.createDocumentFragment(),lines=(src||'').replace(/\r/g,'').split('\n');let i=0;
    while(i<lines.length){
      const ln=lines[i];
      const fence=ln.match(/^\s*```/);
      if(fence){const buf=[];i++;while(i<lines.length&&!/^\s*```/.test(lines[i])){buf.push(lines[i]);i++;}i++;
        const pre=h('pre');pre.appendChild(h('code',null,buf.join('\n')));frag.appendChild(pre);continue;}
      const hd=ln.match(/^(#{1,4})\s+(.*)/);
      if(hd){const e=h('h'+Math.min(6,hd[1].length+2));inline(e,hd[2]);frag.appendChild(e);i++;continue;}
      if(TROW.test(ln)&&i+1<lines.length&&TSEP.test(lines[i+1])){
        const wrap=h('div','tablewrap'),table=h('table'),head=h('tr');cells(ln).forEach(c=>{const th=h('th');inline(th,c);head.appendChild(th);});
        table.appendChild(head);i+=2;
        while(i<lines.length&&TROW.test(lines[i])){const tr=h('tr');cells(lines[i]).forEach(c=>{const td=h('td');inline(td,c);tr.appendChild(td);});table.appendChild(tr);i++;}
        wrap.appendChild(table);frag.appendChild(wrap);continue;}
      if(LIST.test(ln)){const ordered=/^\s*\d/.test(ln),list=h(ordered?'ol':'ul');
        while(i<lines.length&&LIST.test(lines[i])){const li=h('li');inline(li,lines[i].replace(LIST,''));list.appendChild(li);i++;}
        frag.appendChild(list);continue;}
      if(!ln.trim()){i++;continue;}
      const p=h('p');let first=true;
      while(i<lines.length&&lines[i].trim()&&!/^\s*(```|#{1,4}\s)/.test(lines[i])&&!LIST.test(lines[i])&&!TROW.test(lines[i])){
        if(!first)p.appendChild(h('br'));inline(p,lines[i]);first=false;i++;}
      frag.appendChild(p);
    }
    return frag;
  }

  // ---- what a tool call is, in words ------------------------------------------
  const KIND={read:['eye','讀取'],write:['pen','寫入'],edit:['pen','修改'],glob:['folder','找檔案'],grep:['search','搜尋'],
    bash:['term','執行'],webfetch:['globe','開啟網頁'],browser_open:['globe','開啟網頁'],web_search:['globe','搜尋網路'],
    todo_write:['list','更新待辦清單'],security_scan:['shield','資安掃描'],ask_user:['help','問你一個問題'],
    subagent:['users','交給子代理'],subagent_batch:['users','交給多個子代理'],discussion:['users','開討論'],discussion_join:['users','加入討論廳'],
    terminal:['term','終端機'],job_start:['term','背景執行'],job_status:['list','查看背景工作'],job_wait:['list','等背景工作'],
    job_stop:['stop','停止背景工作'],goal_done:['check','回報目標'],skill:['layers','載入技能'],
    lsp:['search','查程式碼'],workflow:['users','工作流程'],schedule_add:['list','設提醒'],schedule_list:['list','查看提醒'],
    schedule_cancel:['x','取消提醒']};
  function parseArgs(raw){
    try{const o=JSON.parse(raw||'{}');return o&&typeof o==='object'?o:{};}
    catch(e){const o={};String(raw||'').replace(/"(path|pattern|command|url|query|name|task)"\s*:\s*"((?:[^"\\]|\\.)*)/g,(_,k,v)=>{o[k]=v.replace(/\\n/g,' ').replace(/\\"/g,'"');});return o;}
  }
  function toolLabel(name,raw){
    const a=parseArgs(raw);let k=KIND[name];
    if(!k){if(name.indexOf('memory')===0)k=['bookmark','記憶'];
      else if(name.indexOf('mcp__')===0){const p=name.split('__');k=['plug','MCP '+(p[1]||'')+'・'+p.slice(2).join('__')];}
      else k=['tool',name];}
    let target=a.path||a.pattern||a.command||a.url||a.query||a.name||a.id||a.message||(a.task?String(a.task):'')||(a.summary?String(a.summary):'')||
      (Array.isArray(a.steps)?a.steps.length+' 個步驟':'')||'';
    if(name==='lsp'&&a.action)target=a.action+' '+(a.path||'')+(a.symbol?' '+a.symbol:'');
    target=String(target).replace(/\s+/g,' ');if(target.length>90)target=target.slice(0,90)+'…';
    return {icon:k[0],verb:k[1],target};
  }

  // ---- the conversation thread --------------------------------------------------
  function showLanding(on){document.body.classList.toggle('landing',on);if(on)renderLanding();showTitle();}
  function showTitle(){const t=convTitle||convPreview;$('#conv-title').textContent=t?(t.length>60?t.slice(0,60)+'…':t):'新任務';
    $('#btn-export').hidden=!convSession||document.body.classList.contains('landing');}
  function setTitle(text){convPreview=text||'';showTitle();}
  const STATE_TEXT={running:'執行中',waiting:'等你回應',idle:'已完成',error:'出錯了'};
  function setState(state){
    convState=state;running=(state==='running'||state==='waiting');
    const pill=$('#conv-state');pill.hidden=!conv||document.body.classList.contains('landing');
    pill.className='pill '+state;pill.textContent='';const d=h('span','dot '+(state==='error'?'':state));pill.append(d,document.createTextNode(STATE_TEXT[state]||state));
    const b=$('#btn-send');b.classList.toggle('stop',running);b.textContent='';b.appendChild(icon(running?'stop':'send'));
    b.title=running?'停止這個任務':'送出';b.setAttribute('aria-label',b.title);
  }
  function setUsage(u){if(!u)return;$('#conv-use').textContent=fmtNum(u.total_tokens)+' tokens・'+(u.tool_calls||0)+' 個步驟';}
  function addUser(text,images,reminder){const w=h('div','m-user');const col=h('div');
    if(reminder)col.appendChild(h('div','att','提醒時間到，自動送出'));col.appendChild(h('div','bubble',text));
    if(images)col.appendChild(h('div','att','附上 '+images+' 張圖片'));w.appendChild(col);transcript.appendChild(w);toBottom(true);}
  function addNote(text,ic){const n=h('div','m-note');if(ic)n.appendChild(icon(ic));n.appendChild(document.createTextNode(text));transcript.appendChild(n);toBottom();}
  function addError(text){transcript.appendChild(h('div','m-error',text));toBottom(true);}
  function finishAssistant(){if(assistantEl){const c=assistantEl.querySelector('.cursor');if(c)c.remove();}assistantEl=null;assistantText='';}
  function renderAssistant(final){
    assistantEl.textContent='';assistantEl.appendChild(md(assistantText));
    if(!final){const c=h('span','cursor');(assistantEl.lastElementChild||assistantEl).appendChild(c);}
  }

  // Under each reply: copy it, and rate it. A rating is a record for people;
  // it never goes back to the model.
  function replyBar(el,turn,text){
    if(!el||!turn||el.nextElementSibling&&el.nextElementSibling.classList.contains('replybar'))return;
    const bar=h('div','replybar');bar.dataset.turn=turn;
    const btn=(ic,title)=>{const b=h('button','rb');b.type='button';b.title=title;b.setAttribute('aria-label',title);b.appendChild(icon(ic));return b;};
    const cp=btn('copy','複製這則回覆'),up=btn('up','這則回覆有幫助'),dn=btn('down','這則回覆沒幫助'),nt=btn('note','寫一句備註');
    const msg=h('span','rmsg');
    let rating=null;
    const send=async(r,note)=>{
      try{await api('/api/conversations/'+conv+'/feedback',{method:'POST',body:{turn,rating:r,note:note||''}});
        rating=r;up.classList.toggle('on',r==='up');dn.classList.toggle('on',r==='down');bar.classList.toggle('rated',!!r);
        msg.textContent=note?'已記下備註':(r?'謝謝回饋':'');}
      catch(e){toast('送出失敗：'+e.message,true);}
    };
    cp.onclick=async()=>{toast(await copyText(text||el.textContent)?'已複製':'複製失敗，請手動選取',false);};
    up.onclick=()=>send(rating==='up'?null:'up');
    dn.onclick=()=>{send(rating==='down'?null:'down');if(rating!=='down')openNote();};
    function openNote(){
      if(bar.querySelector('.rnote'))return;
      const box=h('div','rnote'),f=h('input');f.placeholder='哪裡不好？（選填，Enter 送出）';f.setAttribute('aria-label','評價備註');
      imeSafeEnter(f,()=>{send(rating||'down',f.value.trim());box.remove();});box.appendChild(f);bar.insertBefore(box,msg);f.focus();
    }
    nt.onclick=openNote;
    bar.append(cp,up,dn,nt,msg);el.after(bar);
    const known=knownRatings[String(turn)];if(known){rating=known;(known==='up'?up:dn).classList.add('on');bar.classList.add('rated');}
  }
  function cmdNote(line,text,ok){const b=h('div','m-cmd'+(ok===false?' bad':''));b.append(h('div','cl',line),h('div','ct',text));transcript.appendChild(b);toBottom(true);}
  function compactedNote(ev){
    const n=h('div','m-note');n.appendChild(icon('layers'));
    n.appendChild(document.createTextNode(ev.reason==='history'?'這段對話較早的部分已整理成摘要。':
      '已把較早的 '+ev.replaced+' 則訊息整理成摘要'+(ev.reason==='auto'||ev.reason==='overflow'?'（對話接近模型上限，自動整理）':'')+'。'));
    const more=h('button','linkbtn','看摘要');const box=h('div','summary-box');box.hidden=true;box.appendChild(md(ev.summary||''));
    more.onclick=()=>{box.hidden=!box.hidden;more.textContent=box.hidden?'看摘要':'收起';};
    n.appendChild(more);transcript.append(n,box);toBottom();
  }

  function setPlan(on){planOn=!!on;$('#btn-plan').classList.toggle('on',planOn);document.body.classList.toggle('plan-on',planOn);
    $('#btn-plan').textContent=planOn?'計畫模式：開':'計畫模式';
    input.placeholder=planOn?'計畫模式：說要做什麼，模型會先提計畫給你核准':'交代一件事，或輸入 / 看指令（Enter 送出，Shift+Enter 換行）';}
  async function planCommand(on){
    if(!conv)await newConversation();
    try{const r=await api('/api/conversations/'+conv+'/commands',{method:'POST',body:{line:'/plan '+(on?'on':'off')}});setPlan(r.data&&r.data.plan_mode);toast(r.text);}
    catch(e){toast('切換失敗：'+e.message,true);}
  }
  $('#btn-plan').onclick=()=>{if(running){toast('任務進行中，等它結束再切換計畫模式。');return;}planCommand(!planOn);};
  const GOAL_WORDS={started:'開始',round:'進行中',done:'完成',blocked:'卡住了',incomplete:'輪數用完，尚未完成',stopped:'已停止',failed:'出錯了'};
  function goalEvent(ev){
    const bar=$('#goalbar');bar.hidden=false;bar.className='goalbar '+(ev.state==='round'||ev.state==='started'?'':ev.state);bar.textContent='';
    bar.append(icon('layers'),h('span','gt','目標：'+ev.objective),h('span','gr','第 '+ev.round+'/'+ev.max_rounds+' 輪・'+(GOAL_WORDS[ev.state]||ev.state)));
    if(ev.state==='round'&&ev.round>1)addNote('目標模式：第 '+ev.round+' 輪，目標還沒完成，繼續做。','layers');
    if(['done','blocked','incomplete','stopped','failed'].includes(ev.state)){
      const n=h('div','m-cmd'+(ev.state==='done'?'':' bad'));n.append(h('div','cl','目標'+(GOAL_WORDS[ev.state]||ev.state)),h('div','ct',ev.summary||'（模型沒有回報摘要）'));transcript.appendChild(n);toBottom();}
  }
  const JOB_WORDS={running:'執行中',done:'完成',failed:'失敗',stopped:'已停止',timeout:'逾時被停止'};
  function jobEvent(ev){
    const before=jobs[ev.id];jobs[ev.id]=ev;renderJobs();
    if(before&&before.state==='running'&&ev.state!=='running')toast('背景工作 '+ev.id+' '+(JOB_WORDS[ev.state]||ev.state),ev.state!=='done');
  }
  function renderJobs(){
    const box=$('#rv-jobs');box.textContent='';const list=Object.values(jobs);
    const live=list.filter(j=>j.state==='running').length+reminders.length;$('#rv-jobs-cnt').textContent=live?String(live):'';
    if(reminders.length){box.appendChild(h('div','sub','提醒（'+reminders.length+'）'));
      reminders.forEach(r=>{const c=h('div','job'),hd=h('div','job-h');hd.append(h('span','pill pending',r.due_local),h('span','c',r.message));
        const x=h('button','linkbtn','取消');x.onclick=async()=>{try{await api('/api/conversations/'+conv+'/reminders',{method:'POST',body:{cancel:r.id}});}catch(e){toast(e.message,true);}};hd.appendChild(x);
        c.append(hd,h('div','m',r.id+(r.every_minutes?'・每 '+r.every_minutes+' 分鐘，還剩 '+r.remaining+' 次':'')));box.appendChild(c);});
      if(list.length)box.appendChild(h('div','sub','背景工作'));}
    if(!list.length&&reminders.length)return;
    if(!list.length){const e=h('div','empty');e.append(h('b',null,'沒有背景工作'),document.createTextNode('模型遇到建置、測試、下載這類長指令時，會用 job_start 丟到背景執行，這裡可以看進度和停止。'));box.appendChild(e);return;}
    list.slice().reverse().forEach(j=>{const c=h('div','job'),hd=h('div','job-h');hd.append(h('span','pill '+j.state,JOB_WORDS[j.state]||j.state),h('span','c',j.command));
      if(j.state==='running'){const st=h('button','linkbtn','停止');st.onclick=async()=>{try{await api('/api/conversations/'+conv+'/jobs',{method:'POST',body:{stop:j.id}});}catch(e){toast(e.message,true);}};hd.appendChild(st);}
      c.append(hd,h('div','m',j.id+'・'+j.runtime+' 秒'+(j.exit_code!=null?'・結束碼 '+j.exit_code:'')+'・記錄 '+j.log));box.appendChild(c);});
  }

  const AGENT_WORDS={running:'進行中',done:'完成',failed:'失敗',stopped:'已停止'};
  const AGENT_PILL={running:'running',done:'idle',failed:'error',stopped:'skipped'};
  const AGENT_KIND={subagent:'子代理',workflow:'工作流程步驟',discussion:'討論參與者'};
  async function agentAction(body){
    try{await api('/api/conversations/'+conv+'/agents',{method:'POST',body});}catch(e){toast(e.message,true);}}
  async function loadRoles(){try{roles=await api('/api/agents/roles');}catch(e){roles=[];}if(rvTab==='agents')renderAgents();}
  function renderAgents(){
    const run=agents.filter(a=>a.status==='running');$('#rv-agents-cnt').textContent=run.length?String(run.length):'';
    if(rvTab!=='agents')return;
    const box=$('#rv-agents');box.textContent='';
    if(agents.length){
      const sub=h('div','sub','這段對話的代理（'+agents.length+'）');box.appendChild(sub);
      if(run.length>1){const all=h('button','linkbtn','全部停止');all.style.marginLeft='8px';all.onclick=()=>agentAction({all:true});sub.appendChild(all);}
      agents.slice().reverse().forEach(a=>{const c=h('div','job'),hd=h('div','job-h');
        hd.append(h('span','pill '+(AGENT_PILL[a.status]||''),AGENT_WORDS[a.status]||a.status),h('span','c',a.name+(a.role?'・'+a.role:'')));
        if(a.status==='running'){const st=h('button','linkbtn','停止');st.onclick=()=>agentAction({stop:a.id});hd.appendChild(st);}
        c.append(hd,h('div','m',(AGENT_KIND[a.kind]||a.kind)+(a.room?'・討論 '+a.room:'')+'・'+(a.tokens||0).toLocaleString()+' token・'
          +a.calls+' 次呼叫・'+a.seconds+' 秒'+(a.model?'・'+a.model:'')+(a.status==='running'&&a.activity?'・正在 '+a.activity:'')));
        if(a.task)c.appendChild(h('div','m',a.task));box.appendChild(c);});
    }
    box.appendChild(h('div','sub','代理角色'));
    if(roles===null){box.appendChild(h('div','m','讀取中…'));loadRoles();return;}
    if(!roles.length){const e=h('div','empty');e.append(h('b',null,'還沒有代理角色'),document.createTextNode(
      '在工作目錄的 .xharness/agents/ 放一個「名稱.md」：開頭寫 name、description，可加 tools（限用的工具）、model（設定檔裡的模型名稱）、max_turns、max_tokens，下面寫這個代理的工作說明。Claude Code 的 .claude/agents/ 也讀得到。'));
      box.appendChild(e);return;}
    roles.forEach(r=>{const c=h('div','job'),hd=h('div','job-h');hd.append(h('span','pill',r.source),h('span','c',r.name));
      const lim=[r.tools?'工具：'+r.tools.join('、'):'工具：同呼叫者',r.model?'模型：'+r.model:'模型：同呼叫者'];
      if(r.max_turns)lim.push('最多 '+r.max_turns+' 輪');if(r.max_tokens)lim.push('上限 '+r.max_tokens.toLocaleString()+' token');
      c.append(hd,h('div','m',r.description||'（沒有說明）'),h('div','m',lim.join('・')));box.appendChild(c);});
  }
  const DISC_MODE={roundtable:'圓桌討論',debate:'辯論',review:'審查',free:'自由討論',teams:'組對組'};
  const DISC_END={rounds:'輪數用完',converged:'整輪都沒有新意見',approved:'審查者全數通過',moderator:'主持人判斷已有結論',budget:'token 額度用完',stopped:'被停止',
    host:'討論廳結束','host-lost':'連不上討論廳','no-hall':'這裡不能開討論廳','not-enough':'加入的組別不足兩組'};
  function discussionEvent(ev){
    if(ev.state==='start'){
      finishAssistant();closeSteps();
      const box=h('div','disc'),hd=h('div','disc-h'),ic=h('span','ic'),tt=h('div','tt');ic.appendChild(icon('users'));
      const teams=ev.teams||[],lineup=teams.length?teams.map(t=>t.name+'（'+t.members.join('、')+'）').join(' 對 '):(ev.participants||[]).join('、');
      tt.append(h('b',null,(ev.hall?'加入討論廳（'+ev.hall+'）':(DISC_MODE[ev.mode]||ev.mode))+'：'+ev.topic),h('span','m',lineup));
      const st=h('button','linkbtn','停止討論');st.onclick=()=>agentAction({room:ev.room});
      hd.append(ic,tt,st);const body=h('div','disc-body');box.append(hd,body);transcript.appendChild(box);
      discs[ev.room]={box,body,stop:st,names:ev.participants||[],teams:teams.map(t=>t.name)};toBottom();return;}
    const d=discs[ev.room];if(!d)return;
    if(ev.state==='invite'){
      // The invite is a credential: shown here to copy, never written to the record.
      const card=h('div','disc-invite'),row=h('div','row'),code=h('code',null,ev.invite),cp=h('button','linkbtn','複製邀請碼');
      cp.onclick=async()=>{await copyText(ev.invite);toast('已複製邀請碼');};row.append(code,cp);
      card.append(h('div','cl','等其他對話加入'),h('div','m','把邀請碼貼到另一個對話（這台或別台機器的 xHarness 都可以），說「加入討論廳，我們是 '
        +(ev.open||[])[0]+'：成員…」。加入的組用它自己的成員、資料與模型；只有代表的公開發言會傳過來。'),row,h('div','m wait','還在等：'+(ev.open||[]).join('、')));
      d.box.insertBefore(card,d.body);d.invite=card;toBottom();return;}
    if(ev.state==='joined'){if(!d.invite)return;
      const joined=(ev.teams||[]).map(t=>t.name+(t.node?'（'+t.node+'）':'')).join('、');
      d.invite.querySelector('.wait').textContent=(joined?'已加入：'+joined:'')+((ev.open||[]).length?(joined?'；':'')+'還在等：'+ev.open.join('、'):'');
      if(ev.started){const r=d.invite.querySelector('.row');if(r)r.remove();const how=d.invite.querySelector('.m:not(.wait)');if(how)how.remove();
        d.invite.querySelector('.cl').textContent='討論廳已開始，邀請碼已失效';}
      return;}
    if(ev.state==='turn'){
      // Teams mode: one colour per team, so who argues for whom is visible at a glance.
      const idx=Math.max(0,ev.team?d.teams.indexOf(ev.team):d.names.indexOf(ev.speaker)),row=h('div','dturn'+(ev.scope==='caucus'?' caucus':'')),av=h('span','av',String(ev.speaker||'?').slice(0,1));
      av.style.setProperty('--c','var(--sp-'+(idx%6+1)+')');
      const b=h('div','db'),nm=h('div','dn');nm.appendChild(h('b',null,ev.speaker));
      if(ev.team)nm.appendChild(h('span','tag',ev.team+(ev.scope==='caucus'?'・組內（其他組看不到）':'・代表發言')+(ev.remote?'（來自 '+ev.remote+'，數字由對方查核）':'')));
      else if(ev.stance||ev.role)nm.appendChild(h('span','tag',ev.stance||ev.role));
      if(ev.unverified&&ev.unverified.length){const w=h('span','tag warn','未查證：'+ev.unverified.join('、'));
        w.title='這些數字在查到的資料裡找不到原文，可能是推算或捏造';nm.appendChild(w);}
      if(ev.uncited&&ev.uncited.length){const w=h('span','tag warn','未查證出處：'+ev.uncited.join('、'));
        w.title='引用的來源不是讀過的檔案、工具結果或提供的背景';nm.appendChild(w);}
      nm.appendChild(h('span','m','第 '+ev.round+' 輪'+(ev.tools&&ev.tools.length?'・查了 '+ev.tools.join('、'):'・沒有查資料')
        +(ev.rechecked?'・被要求先查證':'')));
      const tx=h('div','dt m-ai');tx.appendChild(md(ev.text||''));b.append(nm,tx);row.append(av,b);d.body.appendChild(row);
      d.body.scrollTop=d.body.scrollHeight;toBottom();return;}
    if(ev.state==='end'){
      d.stop.remove();const e=h('div','disc-end');
      e.appendChild(h('div','cl','結論・'+(DISC_END[ev.reason]||ev.reason)+'・'+ev.turns+' 次發言・'+(ev.tokens||0).toLocaleString()+' token'));
      const s=h('div','dt m-ai');s.appendChild(md(ev.summary||''));e.appendChild(s);d.box.appendChild(e);toBottom();}
  }

  function stepsHead(box){
    const all=box.querySelectorAll('.step'),busy=box.querySelectorAll('.step .spin').length,bad=box.querySelectorAll('.step .badmark').length;
    const t=box.querySelector('.steps-head .tx');
    t.textContent=all.length+' 個步驟'+(busy?'・進行中':'')+(bad?'・'+bad+' 個沒成功':'');
  }
  function ensureSteps(){
    if(stepsBox&&transcript.lastElementChild===stepsBox)return stepsBox;
    const box=h('div','steps'),head=h('button','steps-head');head.type='button';
    head.append(icon('chev'),h('span','tx',''));head.firstChild.classList.add('chev');
    head.onclick=()=>box.classList.toggle('closed');
    box.append(head,h('div','steps-body'));transcript.appendChild(box);stepsBox=box;return box;
  }
  function settleSpinners(){transcript.querySelectorAll('.step .spin').forEach(sp=>{const m=h('span','badmark');m.appendChild(icon('x'));m.title='沒有完成';sp.replaceWith(m);});
    transcript.querySelectorAll('.steps').forEach(stepsHead);}
  function closeSteps(){if(stepsBox&&!stepsBox.querySelector('.spin')&&!stepsBox.querySelector('.badmark'))stepsBox.classList.add('closed');stepsBox=null;}
  function toolStart(ev){
    finishAssistant();
    const box=ensureSteps(),lab=toolLabel(ev.name,ev.args);
    const step=h('div','step'),row=h('div','step-row'),ic=h('span','ic');ic.appendChild(icon(lab.icon));
    const lb=h('span','lb',lab.verb);if(lab.target){lb.appendChild(h('code',null,lab.target));}
    const st=h('span','spin');
    row.append(ic,lb,h('span','delta'),h('span','tm'),st);
    const det=h('div','step-detail');det.appendChild(h('div','cap','參數'));det.appendChild(h('pre','pre',ev.args||'(無)'));
    row.onclick=()=>step.classList.toggle('open');
    step.append(row,det);box.querySelector('.steps-body').appendChild(step);
    step._t0=Date.now();step._label=lab.verb+(lab.target?' '+lab.target:'');rows[ev.call||('n'+Object.keys(rows).length)]=step;stepsHead(box);toBottom();
  }
  function toolEnd(ev){
    let step=ev.call!=null?rows[ev.call]:null;
    if(!step){const all=[...transcript.querySelectorAll('.step')].reverse();step=all.find(s=>s.querySelector('.spin'));}
    if(!step)return;
    const sp=step.querySelector('.spin');if(sp){const m=h('span',ev.ok?'okmark':'badmark');m.appendChild(icon(ev.ok?'check':'x'));sp.replaceWith(m);}
    const secs=(Date.now()-(step._t0||Date.now()))/1000;step.querySelector('.tm').textContent=secs>=0.1?secs.toFixed(1)+' 秒':'';
    const det=step.querySelector('.step-detail');det.appendChild(h('div','cap',ev.ok?'結果':'錯誤'));det.appendChild(h('pre','pre',ev.output||'(沒有輸出)'));
    const ch=changes.find(c=>c.call===ev.call);
    if(ch){ch.status=ev.ok?'applied':(/denied/.test(ev.output||'')?'denied':'skipped');renderChanges();}
    stepsHead(step.closest('.steps'));
  }
  function onChange(ev){
    const ch=Object.assign({status:'pending'},ev);changes.push(ch);
    const step=rows[ev.call];
    if(step){const d=step.querySelector('.delta');d.textContent='';d.append(h('span','a','+'+ev.added),h('span','d','−'+ev.removed));
      step.querySelector('.step-detail').append(h('div','cap','變更'),diffView(ch,160));}
    renderChanges();
  }
  function diffView(ch,limit){
    const box=h('div','diff');
    if(ch.note){box.appendChild(h('div','more',ch.note));return box;}
    const lines=ch.diff||[];
    if(!lines.length){box.appendChild(h('div','more',ch.created?'新檔案（空的）':'內容沒有改變'));return box;}
    lines.slice(0,limit||lines.length).forEach(l=>{const c=l[0]==='+'?'a':l[0]==='-'?'d':l[0]==='@'?'h':'';box.appendChild(h('div',c,l||' '));});
    if((limit&&lines.length>limit)||ch.truncated)box.appendChild(h('div','more','還有更多行，完整差異在「檢視」裡'));
    return box;
  }

  function approvalCard(ev){
    finishAssistant();
    const card=h('div','ask'),t=h('div','ask-t');t.append(icon('warn'),h('span',null,'需要你的許可'));
    card.append(t,h('div','ask-s',ev.summary));card.dataset.aid=ev.id;
    const ch=changes.find(c=>c.call===ev.call),step=rows[ev.call];
    // What the settled line says: the step's own words ("修改 通知.md"), not the raw summary with its full path.
    card.dataset.label=step&&step._label?step._label:ev.summary;
    if(ch)card.appendChild(diffView(ch,40));
    const acts=h('div','acts'),ok=h('button','btn primary','允許'),no=h('button','btn','拒絕');
    acts.append(ok,no);
    if(ch){const more=h('button','linkbtn','在檢視中看完整變更');more.onclick=()=>openReview('changes');acts.appendChild(more);}
    const decide=async allow=>{ok.disabled=no.disabled=true;
      try{await api('/api/conversations/'+conv+'/approvals',{method:'POST',body:{id:ev.id,allow}});}
      catch(e){ok.disabled=no.disabled=false;toast('送出失敗：'+e.message,true);}};
    ok.onclick=()=>decide(true);no.onclick=()=>decide(false);
    card.appendChild(acts);transcript.appendChild(card);stepsBox=null;toBottom(true);
  }
  function settleApproval(id,allowed){
    const card=transcript.querySelector('.ask[data-aid="'+id+'"]');if(!card)return;
    card.classList.add('done');const t=card.querySelector('.ask-t');t.textContent='';
    t.append(icon(allowed?'check':'x'),h('span',null,(allowed?'已允許：':'已拒絕：')+(card.dataset.label||'').slice(0,80)));
    card.querySelector('.ask-s').hidden=true;
  }

  // --- questions from the ask_user tool ------------------------------------
  // A tinted card in the thread, answered by a click or in the person's own
  // words. Everything the model wrote is set with textContent, never as HTML.
  function imeSafeEnter(input,onEnter){
    let composing=false;
    input.addEventListener('compositionstart',()=>{composing=true;});
    input.addEventListener('compositionend',()=>{composing=false;});
    input.addEventListener('keydown',e=>{
      if(e.key!=='Enter')return;
      if(composing||e.isComposing||e.keyCode===229)return;   // the IME is confirming a candidate
      e.preventDefault();onEnter();
    });
  }
  function renderQuestion(ev){
    finishAssistant();
    const card=h('div','ask question'),t=h('div','ask-t');t.append(icon('help'),h('span','who','需要你的回答'));
    card.append(t,h('div','ask-q',ev.question));card.dataset.qid=ev.id;
    const opts=h('div','qopts'),free=h('div','qfree');
    const field=h('input');field.placeholder='或用自己的話回答…';field.setAttribute('aria-label','自己輸入回答');
    const go=h('button','btn sm','送出'),err=h('div','qerr');
    const controls=()=>[...card.querySelectorAll('button,input')];
    const send=async answer=>{
      answer=(answer||'').trim();if(!answer)return;
      controls().forEach(x=>x.disabled=true);err.textContent='';
      try{await api('/api/conversations/'+conv+'/answers',{method:'POST',body:{id:ev.id,answer}});}
      catch(e){controls().forEach(x=>x.disabled=false);err.textContent='送出失敗：'+e.message;}
    };
    (ev.options||[]).forEach(o=>{
      const b=h('button','qopt');b.dataset.label=o.label;b.appendChild(h('span','ql',o.label));
      if(o.description)b.appendChild(h('span','qd',o.description));
      b.onclick=()=>send(o.label);opts.appendChild(b);
    });
    go.onclick=()=>send(field.value);
    imeSafeEnter(field,()=>send(field.value));
    free.append(field,go);card.append(opts,free,err);transcript.appendChild(card);stepsBox=null;toBottom(true);
  }
  function settleQuestion(id,answer){
    const card=transcript.querySelector('.ask.question[data-qid="'+id+'"]');
    if(!card)return;
    card.querySelectorAll('button,input').forEach(x=>x.disabled=true);
    card.classList.add('done');if(answer)card.classList.add('keep');
    card.querySelector('.who').textContent=answer?'你的回答：'+answer:'未回答（逾時或任務已停止）';
    card.querySelectorAll('.qopt').forEach(b=>b.classList.toggle('chosen',b.dataset.label===answer));
  }

  function handle(ev){
    const t=ev.type;
    if(t==='user'){showLanding(false);closeSteps();addUser(ev.text,ev.images,ev.reminder);setState('running');turnWasPlan=planOn;if(!convPreview)setTitle(ev.text);}
    else if(t==='history'){showLanding(false);if(ev.role==='user'){addUser(ev.text);if(!convPreview)setTitle(ev.text);}
      else{const a=h('div','m-ai');a.appendChild(md(ev.text));transcript.appendChild(a);replyBar(a,ev.turn,ev.text);}}
    else if(t==='compacted')compactedNote(ev);
    else if(t==='plan')setPlan(ev.on);
    else if(t==='goal')goalEvent(ev);
    else if(t==='job')jobEvent(ev);
    else if(t==='attached')addNote('已附加：'+ev.files.join('、'),'file');
    else if(t==='reminders'){reminders=ev.items||[];renderJobs();}
    else if(t==='agents'){agents=ev.items||[];renderAgents();}
    else if(t==='route'){
      // The router picks per request: keep the chip honest, and say so when personal data or local files kept the request off the bought-in models.
      setModelShown(null,ev.model);
      const kept=(ev.excluded||[]).some(x=>x.why==='sensitive'||x.why==='files'),key=ev.provider+'|'+kept;
      const where=({local:'本機',private:'私有雲',public:'外部'}[ev.where]||ev.where);
      if(key!==lastRoute){lastRoute=key;addNote(ev.fallback?ev.reason+'（'+where+'）':'這一步選用 '+ev.provider+'（'+where+'）：'+ev.reason.replace(/；選 [^；]*$/,''),'shield');}}
    else if(t==='discussion')discussionEvent(ev);
    else if(t==='workflow'){if(ev.state==='start')addNote('工作流程開始：'+ev.steps.length+' 個步驟（'+ev.steps.join('、')+'），沒有相依的步驟會同時進行。','users');
      else addNote('工作流程結束：'+(ev.ok?'全部完成':'有步驟沒有完成，請看結果'),ev.ok?'check':'warn');}
    else if(t==='title'){convTitle=ev.title||'';showTitle();refreshTasks();}
    else if(t==='model')setModelShown(ev.provider,ev.model);   // the command block or a toast already says so
    else if(t==='delta'){
      if(!assistantEl){closeSteps();assistantEl=h('div','m-ai');transcript.appendChild(assistantEl);lastAiEl=assistantEl;}
      assistantText+=ev.text;renderAssistant(false);toBottom();
    }
    else if(t==='tool_start')toolStart(ev);
    else if(t==='change')onChange(ev);
    else if(t==='tool_end')toolEnd(ev);
    else if(t==='approval_request'){approvalCard(ev);setState('waiting');}
    else if(t==='approval_resolved'){settleApproval(ev.id,ev.allowed);if(running)setState('running');}
    else if(t==='question'){renderQuestion(ev);setState('waiting');}
    else if(t==='question_resolved'){settleQuestion(ev.id,ev.answer);if(running)setState('running');}
    else if(t==='subagent_start'){finishAssistant();addNote('子代理「'+ev.name+'」開始：'+ev.task,'users');}
    else if(t==='subagent_end'){addNote('子代理「'+ev.name+'」'+(ev.ok?'完成':'沒有完成'),'users');}
    else if(t==='stop_requested'){addNote('已要求停止：下一次呼叫模型前會停下，等待中的許可一律拒絕。','stop');}
    else if(t==='done'){
      if(assistantEl)renderAssistant(true);
      const replyText=assistantText||ev.answer||'';
      if(!ev.answer&&!assistantText&&!assistantEl)addNote('（模型沒有回覆文字）');
      else if(lastAiEl&&transcript.lastElementChild===lastAiEl)replyBar(lastAiEl,ev.turn,replyText);
      if(turnWasPlan&&planOn&&replyText){const b=h('button','btn primary approve-plan');b.append(icon('check'),document.createTextNode('核准計畫並執行'));
        b.onclick=async()=>{b.disabled=true;await planCommand(false);input.value='計畫核准，請照計畫執行，完成後說明你檢查了什麼。';send();b.remove();};transcript.appendChild(b);toBottom();}
      finishAssistant();settleSpinners();closeSteps();
      transcript.querySelectorAll('.steps').forEach(b=>{if(!b.querySelector('.badmark'))b.classList.add('closed');});
      setState('idle');setUsage(ev.usage);refreshTasks();
    }
    else if(t==='error'){finishAssistant();settleSpinners();closeSteps();addError(ev.message);setState('error');setUsage(ev.usage);refreshTasks();}
  }

  async function connect(id,after){
    if(es)es.close();
    let url='/api/conversations/'+id+'/events?after='+(after||0);
    if(token){const t=await api('/api/tickets',{method:'POST'});url+='&ticket='+encodeURIComponent(t.ticket);}
    es=new EventSource(url);
    es.onmessage=e=>{try{handle(JSON.parse(e.data))}catch(err){console.error(err)}};
    es.onerror=()=>{/* EventSource reconnects with Last-Event-ID */};
  }
  function resetThread(){
    transcript.textContent='';assistantEl=null;assistantText='';stepsBox=null;changes=[];lastAiEl=null;convTitle='';knownRatings={};
    jobs={};reminders=[];renderJobs();agents=[];discs={};lastRoute='';renderAgents();$('#goalbar').hidden=true;setPlan(false);
    Object.keys(rows).forEach(k=>delete rows[k]);renderChanges();$('#conv-use').textContent='';
  }
  async function openConversation(c){
    conv=c.id;resetThread();convSession=c.session||'';convTitle=c.title||'';knownRatings=c.ratings||{};setTitle(c.preview||'');showLanding(false);setState(c.state||'idle');setUsage(c.usage);
    setModelShown(c.provider,c.model);setPlan(c.plan_mode);(c.jobs||[]).forEach(j=>{jobs[j.id]=j;});reminders=c.reminders||[];renderJobs();agents=c.agents||[];renderAgents();
    hint.textContent='';showView('chat');await connect(conv,0);closeDrawer();renderTasks();
  }
  async function newConversation(resume){
    const body=resume?{resume}:{};const choice=pendingProvider||($('#model-select').value||'');
    if(choice&&choice!==models.default)body.provider=choice;
    const c=await api('/api/conversations',{method:'POST',body});
    conv=c.id;resetThread();convSession=c.session||'';convTitle=c.title||'';setTitle('');showLanding(!resume);setState('idle');setModelShown(c.provider,c.model);
    hint.textContent=resume?'接續 session '+resume:'';showView('chat');
    await connect(conv,0);refreshTasks();closeDrawer();input.focus();return c;
  }
  function commandOf(text){
    if(!text.startsWith('/'))return null;
    const name=text.slice(1).split(/\s+/)[0].toLowerCase();
    return commands.find(c=>c.name===name)||null;
  }
  async function runCommand(text){
    const cmd=commandOf(text);closePop();input.value='';autosize();
    if(cmd.name==='new'){await newConversation();return;}
    if(cmd.name==='plan'){const w=text.split(/\s+/)[1];if(running){toast('任務進行中，等它結束再切換計畫模式。');return;}await planCommand(w==='off'?false:w==='on'?true:!planOn);input.value='';autosize();return;}
    if(cmd.name==='stop'){if(running)await stopTask();else toast('沒有進行中的任務');return;}
    if(cmd.name==='export'){await exportConv();return;}
    if(cmd.name==='goal'&&text.trim()!=='/goal'){
      if(running){toast('任務還在進行，等它結束再設目標。');return;}
      if(!conv)await newConversation();
      try{const r=await api('/api/conversations/'+conv+'/commands',{method:'POST',body:{line:text}});
        if(!r.ok||r.action!=='goal'){cmdNote(text,r.text,r.ok);return;}
        await api('/api/conversations/'+conv+'/messages',{method:'POST',body:{text:r.data.objective,goal:{rounds:r.data.rounds}}});
      }catch(e){cmdNote(text,e.message,false);}
      return;}
    if(!conv)await newConversation();
    try{
      const r=await api('/api/conversations/'+conv+'/commands',{method:'POST',body:{line:text}});
      showLanding(false);
      if(!(cmd.name==='compact'&&r.ok&&r.data&&r.data.summary))cmdNote(text,r.text,r.ok);   // the compaction note already shows it
      if(r.data&&r.data.title){convTitle=r.data.title;showTitle();refreshTasks();}
    }catch(e){cmdNote(text,e.message,false);}
  }
  async function send(){
    const typed0=input.value.trim();
    if(commandOf(typed0)){await runCommand(typed0);return;}
    // A bare /word that is not a command never reaches the model, which would
    // treat it as a request and may invent work it never did.
    if(/^\/[A-Za-z][\w-]*$/.test(typed0)){toast('沒有 '+typed0+' 這個指令，輸入 / 看全部',true);return;}
    if(running){toast('這個任務還在進行；要換方向先按停止。');return;}
    const typed=input.value.trim();
    // Attached files are named in the message itself, so the agent knows they
    // exist and where they are without any special protocol.
    const text=(typed||(pendingImages.length?'請看這張圖片。':''))+takeQueued();
    if(!text.trim())return;
    if(!conv)await newConversation();
    const images=pendingImages.map(i=>({name:i.name,type:i.type,data:i.data}));
    try{await api('/api/conversations/'+conv+'/messages',{method:'POST',body:images.length?{text,images}:{text}});input.value='';autosize();
      pendingImages.forEach(i=>URL.revokeObjectURL(i.url));pendingImages=[];renderQueued();}
    catch(e){addError(e.message);}
  }
  async function exportConv(){
    if(!convSession){toast('還沒有可以匯出的對話');return;}
    try{
      const headers={'X-XHarness-Client':'1'};if(token)headers['Authorization']='Bearer '+token;
      const r=await fetch('/api/sessions/'+encodeURIComponent(convSession)+'/export',{headers});
      if(!r.ok)throw new Error('HTTP '+r.status);
      const url=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=url;
      a.download=((convTitle||convPreview||'xharness').replace(/[\\/:*?"<>|\s]+/g,'_').slice(0,40)||'xharness')+'.md';a.click();URL.revokeObjectURL(url);
      toast('已匯出');
    }catch(e){toast('匯出失敗：'+e.message,true);}
  }
  $('#btn-export').onclick=exportConv;
  // ---- models ----
  function setModelShown(provider,model){
    const sel=$('#model-select');if(provider&&[...sel.options].some(o=>o.value===provider)){sel.value=provider;shownProvider=provider;}
    if(model)$('#model-name').textContent=model;
  }
  async function loadModels(){
    try{models=await api('/api/models');}catch(e){return;}
    const sel=$('#model-select');sel.textContent='';
    models.providers.forEach(p=>{const o=document.createElement('option');o.value=p.name;o.textContent=p.name+'・'+p.model+(p.hosting==='self-hosted'?'':'（外部）');sel.appendChild(o);});
    sel.value=models.default;shownProvider=models.default;sel.hidden=!models.switchable||models.providers.length<2;
  }
  let shownProvider='';
  $('#model-select').onchange=async e=>{
    const name=e.target.value,p=models.providers.find(x=>x.name===name);
    if(!conv){pendingProvider=name;if(p)$('#model-name').textContent=p.model;return;}   // used when the task is created
    if(running){toast('任務還在進行，等它結束再換模型。');e.target.value=shownProvider;return;}
    try{const r=await api('/api/conversations/'+conv+'/model',{method:'POST',body:{provider:name}});toast(r.text);}
    catch(err){toast('換模型失敗：'+err.message,true);e.target.value=shownProvider;}
  };
  async function stopTask(){
    if(!conv)return;
    try{await api('/api/conversations/'+conv+'/stop',{method:'POST',body:{}});toast('已要求停止');}catch(e){toast('停止失敗：'+e.message,true);}
  }

  // composer: IME-safe Enter (compositionstart/end flag + isComposing + keyCode 229)
  input.addEventListener('compositionstart',()=>composing=true);
  input.addEventListener('compositionend',()=>{composing=false});
  input.addEventListener('keydown',e=>{
    if(composing||e.isComposing||e.keyCode===229)return;
    if(!$('#cmdpop').hidden){
      if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();movePop(e.key==='ArrowDown'?1:-1);return;}
      if(e.key==='Escape'){e.preventDefault();closePop();return;}
      if(e.key==='Tab'||(e.key==='Enter'&&!e.shiftKey&&(popMode==='file'||!exactCommand()))){e.preventDefault();pickPop();return;}
    }
    if(e.key!=='Enter'||e.shiftKey)return;
    e.preventDefault();send();
  });
  function autosize(){input.style.height='auto';input.style.height=Math.min(220,input.scrollHeight)+'px';input.style.overflowY=input.scrollHeight>220?'auto':'hidden';}
  input.addEventListener('input',()=>{autosize();updatePop();});
  // ---- slash-command popup ----
  let popIndex=0,popItems=[];
  function exactCommand(){const t=input.value.trim();const c=commandOf(t);return !!c&&(t.length>c.name.length+1||!c.args);}
  function closePop(){$('#cmdpop').hidden=true;popItems=[];popMode='cmd';popIndex=0;}   // reopened menus start at the top
  let popMode='cmd',mentionAt=-1,mentionTimer=null;
  function mentionQuery(){
    const pos=input.selectionStart,before=input.value.slice(0,pos);const m=before.match(/(^|\s)@([^\s@]*)$/);
    if(!m)return null;mentionAt=pos-m[2].length-1;return m[2];
  }
  function showMentions(items){
    const pop=$('#cmdpop');popItems=items;popMode='file';popIndex=Math.min(popIndex,Math.max(0,items.length-1));pop.textContent='';
    if(!items.length){closePop();return;}
    items.forEach((f,i)=>{const it=h('div','ci'+(i===popIndex?' on':''));it.setAttribute('role','option');
      it.append(h('span','cn','@'),h('span','ch',f.path),h('span','ca',humanSize(f.bytes)));
      it.onmousedown=ev=>{ev.preventDefault();popIndex=i;pickPop();};it.onmousemove=()=>{if(popIndex!==i){popIndex=i;markPop();}};pop.appendChild(it);});
    pop.hidden=false;markPop();
  }
  function updatePop(){
    const t=input.value;const pop=$('#cmdpop');
    const q=mentionQuery();
    if(q!==null){clearTimeout(mentionTimer);mentionTimer=setTimeout(async()=>{try{showMentions(await api('/api/workspace/files?q='+encodeURIComponent(q)));}catch(e){closePop();}},150);return;}
    popMode='cmd';
    if(!t.startsWith('/')||/\s/.test(t.trim())||t.indexOf('\n')>=0){closePop();return;}
    const prefix=t.slice(1).toLowerCase();popItems=commands.filter(c=>c.name.indexOf(prefix)===0);
    if(!popItems.length){closePop();return;}
    popIndex=Math.min(popIndex,popItems.length-1);pop.textContent='';
    popItems.forEach((c,i)=>{const it=h('div','ci'+(i===popIndex?' on':''));it.setAttribute('role','option');
      it.append(h('span','cn','/'+c.name),h('span','ca',c.args||''),h('span','ch',c.help));
      it.onmousedown=ev=>{ev.preventDefault();popIndex=i;pickPop();};it.onmousemove=()=>{if(popIndex!==i){popIndex=i;markPop();}};pop.appendChild(it);});
    pop.hidden=false;markPop();
  }
  // Arrow keys move the highlight only. Rebuilding the list on every key reset its scroll
  // to the top, so past the eighth item the highlight sank out of sight.
  function markPop(){
    const pop=$('#cmdpop');[...pop.children].forEach((el,i)=>{el.classList.toggle('on',i===popIndex);el.setAttribute('aria-selected',String(i===popIndex));});
    const cur=pop.children[popIndex];if(!cur)return;
    const pr=pop.getBoundingClientRect(),cr=cur.getBoundingClientRect();   // scroll the list only, never the page
    if(cr.top<pr.top)pop.scrollTop-=pr.top-cr.top+4;else if(cr.bottom>pr.bottom)pop.scrollTop+=cr.bottom-pr.bottom+4;
  }
  function movePop(d){if(!popItems.length)return;popIndex=(popIndex+d+popItems.length)%popItems.length;markPop();}
  function pickPop(){const c=popItems[popIndex];if(!c)return;
    if(popMode==='file'){const pos=input.selectionStart;const v=input.value;const insert='@'+c.path+' ';
      input.value=v.slice(0,mentionAt)+insert+v.slice(pos);const caret=mentionAt+insert.length;closePop();autosize();input.focus();input.setSelectionRange(caret,caret);return;}
    input.value='/'+c.name+(c.args?' ':'');closePop();autosize();input.focus();
    if(!c.args)send();}
  input.addEventListener('blur',()=>setTimeout(closePop,150));
  input.addEventListener('focus',()=>$('#composer').classList.add('focus'));
  input.addEventListener('blur',()=>$('#composer').classList.remove('focus'));
  $('#btn-send').onclick=()=>running?stopTask():send();

  // ---- landing ----------------------------------------------------------------------
  const STARTERS=[
    ['認識專案','eye','摘要這個工作目錄在做什麼','摘要這個工作目錄在做什麼，先看有哪些檔案再回答。'],
    // A discussion needs a topic: this one only fills the box (prefill) and waits for the person.
    ['代理討論','users','讓幾個代理針對一個題目辯論','讓 正方 和 反方 辯論：在這裡寫題目，兩輪。發言前先查資料。','在這裡寫題目'],
    ['資安','shield','掃一次這個專案的資安問題','對這個目錄跑一次 security_scan，並用白話解釋每一項發現。'],
    ['交接','users','新同事接手該先讀哪三個檔案','這個專案如果要交給新同事接手，最需要先讀的三個檔案是哪些？為什麼？']];
  function renderLanding(){
    const box=$('#starters');if(!box.childElementCount){
      STARTERS.forEach(([k,ic,v,task,blank])=>{const b=h('button','start'),i=h('span','ic');i.appendChild(icon(ic));
        const tx=h('span');tx.append(h('span','sk',k),h('span','sv',v));b.append(i,tx);
        b.onclick=()=>{input.value=task;autosize();
          if(blank){const at=task.indexOf(blank);input.focus();input.setSelectionRange(at,at+blank.length);return;}
          send();};box.appendChild(b);});
    }
    const hour=new Date().getHours(),greet=hour<11?'早安':hour<14?'午安':hour<18?'午安':'晚安';
    $('#hero-title').textContent=greet+(me?'，'+me.display:'')+'。今天要交給 agent 什麼事？';
    const rec=$('#recent');rec.textContent='';
    const items=savedSessions.filter(s=>s.preview).slice(0,4);
    if(!items.length){rec.appendChild(h('div','muted','還沒有做過的任務。完成第一個任務後，這裡可以一鍵接續。'));}
    items.forEach(s=>{const b=h('button','start'),i=h('span','ic');i.appendChild(icon('list'));
      const tx=h('span');tx.append(h('span','sk',ago(s.mtime)),h('span','sv',s.title||s.preview));b.append(i,tx);
      b.onclick=()=>newConversation(s.id);rec.appendChild(b);});
  }
  $('#tile-running').onclick=()=>openDrawer();
  $('#tile-waiting').onclick=()=>openDrawer();
  async function loadDayUsage(){try{const u=await api('/api/usage?since=24h');$('#t-tokens').textContent=fmtNum(u.totals.total_tokens);}catch(e){}}

  // ---- drawer: every task, live ones first ---------------------------------------
  function openDrawer(){document.body.classList.add('drawer-open');$('#btn-drawer').setAttribute('aria-expanded','true');$('#btn-drawer').classList.add('on');refreshTasks(true);}
  function closeDrawer(){document.body.classList.remove('drawer-open');$('#btn-drawer').setAttribute('aria-expanded','false');$('#btn-drawer').classList.remove('on');}
  $('#btn-drawer').onclick=()=>document.body.classList.contains('drawer-open')?closeDrawer():openDrawer();
  $('#drawer-close').onclick=closeDrawer;
  $('#btn-newtask').onclick=()=>newConversation();
  let searchTimer=null;
  $('#task-filter').addEventListener('input',()=>{
    renderTasks();clearTimeout(searchTimer);const q=$('#task-filter').value.trim();
    if(q.length<2){searchHits=null;renderTasks();return;}
    searchTimer=setTimeout(async()=>{try{searchHits=await api('/api/sessions?q='+encodeURIComponent(q));}catch(e){searchHits=[];}renderTasks();},300);
  });
  async function refreshTasks(withSessions){
    try{liveConvs=await api('/api/conversations');}catch(e){return;}
    if(withSessions||!savedSessions.length){try{savedSessions=await api('/api/sessions');}catch(e){}}
    const waiting=liveConvs.filter(c=>c.state==='waiting').length,runningN=liveConvs.filter(c=>c.state==='running').length;
    const badge=$('#drawer-badge');badge.hidden=!waiting;badge.textContent=waiting;
    $('#t-running').textContent=runningN;$('#t-waiting').textContent=waiting;
    renderTasks();if(document.body.classList.contains('landing'))renderLanding();
  }
  function taskItem(title,meta,state,active,onclick,snippet){
    const d=h('div','task'+(active?' active':''));d.append(h('span','dot '+(state||'')));
    const b=h('div','tb');b.append(h('div','tt',title));if(snippet)b.appendChild(h('div','snip',snippet));b.appendChild(h('div','tm',meta));
    d.appendChild(b);d.onclick=onclick;return d;
  }
  function renderTasks(){
    const box=$('#task-list');box.textContent='';const q=($('#task-filter').value||'').trim().toLowerCase();
    const match=t=>!q||String(t||'').toLowerCase().indexOf(q)>=0;
    const groups=[['等你回應',c=>c.state==='waiting'],['進行中',c=>c.state==='running'],['這次開啟的',c=>c.state==='idle']];
    let shown=0;const liveSessions=new Set();
    groups.forEach(([label,test])=>{
      const list=liveConvs.filter(c=>test(c)&&(c.preview||c.state!=='idle')&&match((c.title||'')+' '+c.preview));list.forEach(c=>liveSessions.add(c.session));
      if(!list.length)return;box.appendChild(h('div','grp',label));
      list.forEach(c=>{shown++;const u=c.usage||{};
        box.appendChild(taskItem(c.title||c.preview||'（尚未送出訊息）',ago(c.last_activity)+(u.total_tokens?'・'+fmtNum(u.total_tokens)+' tokens':''),c.state,c.id===conv,()=>openConversation(c)));});
    });
    if(searchHits){
      const hits=searchHits.filter(s=>!liveSessions.has(s.id));
      box.appendChild(h('div','grp','搜尋結果（'+hits.length+'）'));
      hits.forEach(s=>{shown++;box.appendChild(taskItem(s.title||s.preview||s.id,ago(s.mtime),'',false,()=>newConversation(s.id),s.snippet));});
      if(!hits.length)box.appendChild(h('div','muted','較早的任務裡沒有找到。'));
      return;
    }
    const older=savedSessions.filter(s=>!liveSessions.has(s.id)&&match((s.title||'')+' '+(s.preview||s.id))).slice(0,40);
    if(older.length){box.appendChild(h('div','grp','較早的任務（點一下接續）'));
      older.forEach(s=>{shown++;box.appendChild(taskItem(s.title||s.preview||s.id,ago(s.mtime),'',false,()=>newConversation(s.id)));});}
    if(!shown)box.appendChild(h('div','muted',q?'沒有符合「'+q+'」的任務。':'還沒有任務。按「新任務」開始。'));
  }

  // ---- review pane: changes, files, tools, memory ------------------------------------
  let rvTab='changes';
  function openReview(tab){if(tab)rvShow(tab);document.body.classList.add('review-open');$('#btn-review').classList.add('on');$('#btn-review').setAttribute('aria-expanded','true');}
  function closeReview(){document.body.classList.remove('review-open');$('#btn-review').classList.remove('on');$('#btn-review').setAttribute('aria-expanded','false');}
  $('#btn-review').onclick=()=>document.body.classList.contains('review-open')?closeReview():openReview();
  $('#review-close').onclick=closeReview;
  document.querySelectorAll('[data-rv]').forEach(b=>b.onclick=()=>rvShow(b.dataset.rv));
  function rvShow(tab){
    rvTab=tab;document.querySelectorAll('[data-rv]').forEach(b=>b.classList.toggle('on',b.dataset.rv===tab));
    if(tab==='jobs')renderJobs();
    if(tab==='agents'){roles=null;renderAgents();}
    ['changes','files','jobs','agents','tools','memory'].forEach(k=>{$('#rv-'+k).hidden=k!==tab;});
    if(tab==='files')loadFiles();if(tab==='tools')loadTools();if(tab==='memory')loadMemory();
  }
  const STATUS={pending:'等你許可',applied:'已套用',skipped:'沒有套用',denied:'已拒絕'};
  function renderChanges(){
    const box=$('#rv-changes');box.textContent='';
    const n=changes.length,pend=changes.filter(c=>c.status==='pending').length;
    const cnt=$('#review-cnt');cnt.hidden=!n;cnt.textContent=n;cnt.classList.toggle('hot',!!pend);
    $('#rv-changes-cnt').textContent=n?String(n):'';
    if(!n){const e=h('div','empty');e.append(h('b',null,'這個任務還沒有改任何檔案'),document.createTextNode('agent 寫入或修改檔案時，差異會出現在這裡；需要許可的會先停下來等你。'));box.appendChild(e);return;}
    changes.slice().reverse().forEach((c,i)=>{
      const card=h('div','chg'+(i>2?' closed':'')),hd=h('div','chg-h');
      hd.append(h('span','pill '+c.status,STATUS[c.status]||c.status),h('span','p',c.path));
      const d=h('span','delta');d.append(h('span','a','+'+c.added),h('span','d','−'+c.removed));hd.appendChild(d);
      hd.onclick=()=>card.classList.toggle('closed');card.appendChild(hd);
      if(c.created)card.appendChild(h('div','note','新建立的檔案'));
      card.appendChild(diffView(c));box.appendChild(card);
    });
  }
  async function loadTools(){
    const box=$('#rv-tools');box.textContent='';
    try{
      const tl=await api('/api/tools');
      box.appendChild(h('div','muted',tl.tools.length+' 個工具・沙箱 '+(tl.sandbox||'未啟用')+(tl.mcp_servers.length?'・MCP：'+tl.mcp_servers.map(sv=>sv.name+'（'+sv.tools+(sv.ok?'':'，未啟動')+'）').join('、'):'')));
      try{const sk=await api('/api/skills');
        box.appendChild(h('div','sub','技能（'+sk.length+'）'));
        if(!sk.length)box.appendChild(h('div','muted','還沒有技能。把含 SKILL.md 的資料夾放進 .xharness/skills/，模型就會在任務符合時自己載入。'));
        sk.forEach(k=>{const d=h('div','tool'),n=h('div','tn',k.name);n.appendChild(h('span','tag ro',k.source));d.append(n,h('div','td',k.description));box.appendChild(d);});
      }catch(e){}
      if((tl.hooks||[]).length||tl.notify){box.appendChild(h('div','sub','Hooks（'+tl.hooks.length+'）'));
        tl.hooks.forEach(k=>{const d=h('div','tool'),n=h('div','tn',k.event);n.appendChild(h('span','tag ro',k.matcher));d.append(n,h('div','td','來源：'+k.source));box.appendChild(d);});
        if(tl.notify)box.appendChild(h('div','muted','每個任務結束後會通知外部程式（notify）。'));}
      if((tl.language_servers||[]).length){box.appendChild(h('div','sub','語言伺服器'));
        tl.language_servers.forEach(l=>{const d=h('div','tool');d.append(h('div','tn',l.name),h('div','td',l.extensions.join('、')));box.appendChild(d);});}
      [['會改東西（走審批）',t=>t.mutating],['唯讀',t=>!t.mutating]].forEach(([label,test])=>{
        const list=tl.tools.filter(test);if(!list.length)return;box.appendChild(h('div','sub',label));
        list.forEach(t=>{const d=h('div','tool'),n=h('div','tn',t.name);n.appendChild(h('span','tag'+(t.mutating?'':' ro'),t.source==='mcp'?'MCP・'+t.server:'內建'));
          d.append(n,h('div','td',t.description));d.title=t.description;box.appendChild(d);});
      });
    }catch(e){box.appendChild(h('div','muted','讀取失敗：'+e.message));}
  }
  async function loadMemory(){
    const box=$('#rv-memory');box.textContent='';
    try{
      const mem=await api('/api/memory');
      if(!mem.length){const e=h('div','empty');e.append(h('b',null,'還沒有記憶'),document.createTextNode('agent 用記憶工具記下的事會出現在這裡，標示多久沒確認過。'));box.appendChild(e);return;}
      let last=null;
      mem.forEach(m=>{if(m.topic!==last){last=m.topic;box.appendChild(h('div','sub','主題：'+m.topic));}
        const d=h('div','tool');d.appendChild(h('div','tn',m.name));
        const meta=m.kind+(m.updated?'・'+m.updated.slice(0,10):'')+(m.stale?'・'+m.age_days+' 天沒確認':'');
        const td=h('div','td',m.description+'（'+meta+'）');if(m.stale)td.classList.add('stale');d.appendChild(td);box.appendChild(d);});
    }catch(e){box.appendChild(h('div','muted','讀取失敗：'+e.message));}
  }

  // ---- uploads ----------------------------------------------------------------------
  let uploadsOn=false;
  const queued=[];
  async function uploadFiles(fileList){
    const list=[...fileList];
    if(!list.length)return;
    if(!uploadsOn){toast('這台伺服器沒有啟用帳號，只能附加圖片；文件請放進工作目錄後用 @ 檔名引用。',true);return;}
    const form=new FormData();
    list.forEach(f=>form.append('file',f,f.name));
    hint.textContent='上傳中：'+list.map(f=>f.name).join('、');
    try{
      const headers={'X-XHarness-Client':'1'};      // no Content-Type: the browser adds the boundary
      if(token)headers['Authorization']='Bearer '+token;
      const r=await fetch('/api/files',{method:'POST',headers,body:form});
      const d=await r.json().catch(()=>({}));
      if(r.status===401){token=null;saveToken();showGate(true,d.auth==='users'?'users':'token');return;}
      (d.refused||[]).forEach(item=>toast('沒有上傳 '+item.name+'：'+item.reason,true));
      const stored=d.stored||[];
      if(stored.length){stored.forEach(f=>queued.push(f));renderQueued();if(rvTab==='files')loadFiles();
        hint.textContent='已上傳 '+stored.length+' 個檔案，送出時會一併告訴 agent。';}
      else{hint.textContent='';if(!d.refused||!d.refused.length)toast('上傳失敗：'+(d.error||('HTTP '+r.status)),true);}
    }catch(err){hint.textContent='';toast('上傳失敗：'+err.message,true);}
  }
  function renderQueued(){
    const box=$('#queued');box.textContent='';
    pendingImages.forEach((img,index)=>{const c=h('span','imgchip');const im=document.createElement('img');im.src=img.url;im.alt=img.name;
      const x=h('button');x.appendChild(icon('x'));x.title='不要附這張圖';x.setAttribute('aria-label','移除 '+img.name);
      x.onclick=()=>{URL.revokeObjectURL(img.url);pendingImages.splice(index,1);renderQueued();};c.append(im,x);box.appendChild(c);});
    queued.forEach((f,index)=>{
      const chip=h('span','qfile');chip.appendChild(h('span',null,f.name+'（'+humanSize(f.bytes)+'）'));
      const drop=h('button');drop.appendChild(icon('x'));drop.title='不要附這個檔案';drop.setAttribute('aria-label','移除 '+f.name);
      drop.onclick=()=>{queued.splice(index,1);renderQueued();};chip.appendChild(drop);box.appendChild(chip);
    });
  }
  function takeQueued(){
    if(!queued.length)return '';
    const lines=queued.map(f=>'- '+f.path).join('\n');
    queued.length=0;renderQueued();
    return '\n\n我已上傳以下檔案，請用 read 工具讀取：\n'+lines;
  }
  const IMAGE_TYPES=['image/png','image/jpeg','image/webp','image/gif'];
  function canSee(){const p=models.providers.find(x=>x.name===(shownProvider||models.default));return (p&&p.vision)||!!models.describer;}
  async function addImages(files){
    for(const f of files){
      if(pendingImages.length>=4){toast('一則訊息最多 4 張圖片',true);break;}
      if(f.size>5*1024*1024){toast(f.name+' 超過 5 MB',true);continue;}
      const data=await new Promise((ok,bad)=>{const r=new FileReader();r.onload=()=>ok(String(r.result).split(',')[1]);r.onerror=bad;r.readAsDataURL(f);});
      pendingImages.push({name:f.name||'image.png',type:f.type,data,url:URL.createObjectURL(f)});
    }
    renderQueued();
    if(pendingImages.length&&!canSee())toast('目前的模型看不懂圖片，也沒有設定描述圖片的模型；送出後模型會告訴你它看不到。',true);
  }
  async function takeFiles(list){
    const files=[...list],images=files.filter(f=>IMAGE_TYPES.includes(f.type)),others=files.filter(f=>!IMAGE_TYPES.includes(f.type));
    if(images.length)await addImages(images);
    if(others.length)uploadFiles(others);
  }
  $('#btn-attach').onclick=()=>$('#file-input').click();
  $('#file-input').onchange=e=>{takeFiles(e.target.files);e.target.value='';};
  input.addEventListener('paste',e=>{const items=[...(e.clipboardData&&e.clipboardData.files||[])].filter(f=>IMAGE_TYPES.includes(f.type));
    if(items.length){e.preventDefault();addImages(items);}});
  const composer=$('#composer');
  ['dragenter','dragover'].forEach(type=>composer.addEventListener(type,e=>{e.preventDefault();composer.classList.add('over');}));
  ['dragleave','drop'].forEach(type=>composer.addEventListener(type,e=>{
    e.preventDefault();if(type==='drop'||!composer.contains(e.relatedTarget))composer.classList.remove('over');}));
  composer.addEventListener('drop',e=>{if(e.dataTransfer&&e.dataTransfer.files)takeFiles(e.dataTransfer.files);});
  async function loadFiles(){
    const box=$('#rv-files');box.textContent='';
    try{
      const d=await api('/api/files');
      uploadsOn=!!d.workspace;
      // images are always accepted (they travel with the message); documents need a workspace
      if(!uploadsOn){const e=h('div','empty');e.append(h('b',null,'沒有個人檔案區'),document.createTextNode('這台伺服器沒有啟用帳號，agent 直接使用伺服器的工作目錄。'));box.appendChild(e);return;}
      const up=h('button','btn sm');up.append(icon('upload'),document.createTextNode('上傳檔案'));up.onclick=()=>$('#file-input').click();
      const head=h('div','row');head.append(h('span','rn','已用 '+humanSize(d.used_bytes)+' / '+humanSize(d.max_total_bytes)),up);box.appendChild(head);
      if(!d.files.length){box.appendChild(h('div','muted','還沒有上傳任何檔案。單檔上限 '+humanSize(d.max_file_bytes)+'；也可以直接把檔案拖到輸入框。'));return;}
      d.files.forEach(f=>{
        const row=h('div','row'),ic=h('span','ic');ic.appendChild(icon('file'));
        const name=h('span','rn',f.path);name.title=f.path;
        const attach=h('button','linkbtn','附加');attach.onclick=()=>{queued.push(f);renderQueued();toast('送出下一則訊息時會附上 '+f.name);};
        const drop=h('button','linkbtn','刪除');
        drop.onclick=async()=>{
          if(!await askConfirm('刪除 '+f.name+'？','檔案會從你的工作區移除，agent 之後讀不到。','刪除'))return;
          try{await api('/api/files',{method:'POST',body:{action:'delete',name:f.name}});loadFiles();toast('已刪除 '+f.name);}
          catch(e){toast('刪除失敗：'+e.message,true);}
        };
        row.append(ic,name,h('span','rm',humanSize(f.bytes)),attach,drop);box.appendChild(row);
      });
    }catch(e){box.appendChild(h('div','muted','讀取失敗：'+e.message));}
  }

  // ---- views --------------------------------------------------------------------------
  let fleetTimer=null;
  function showView(name){
    view=name;['chat','fleet','admin'].forEach(k=>{document.body.classList.toggle('view-'+k,k===name);$('#nav-'+k).classList.toggle('on',k===name);});
    if(fleetTimer){clearInterval(fleetTimer);fleetTimer=null;}
    if(name==='fleet'){renderFleet();loadUsage();fleetTimer=setInterval(()=>{if(!document.hidden)renderFleet();},2500);}
    if(name==='admin')admShow('sec');
  }
  $('#nav-chat').onclick=()=>showView('chat');
  $('#nav-fleet').onclick=()=>showView('fleet');
  $('#nav-admin').onclick=()=>showView('admin');
  $('#logo').onclick=e=>{e.preventDefault();showView('chat');};

  // ---- fleet --------------------------------------------------------------------------
  function fmtState(s){return s==='running'?'執行中':s==='waiting'?'等你回應':'閒置';}
  async function renderFleet(){
    try{
      const f=await api('/api/fleet');const k=Object.assign({},f.summary);
      (f.nodes||[]).forEach(n=>{const s=n.summary||{};['conversations','running','waiting','children','total_tokens'].forEach(key=>{k[key]=(k[key]||0)+(s[key]||0);});});
      const nodesUp=(f.nodes||[]).filter(n=>n.ok).length,nodesAll=(f.nodes||[]).length;
      // Redrawn every 2.5 s: only what changed is touched, and the entrance animation
      // plays once per element -- rebuilding everything made the whole page jump.
      const kp=$('#kpis');
      const tiles=[['任務',k.conversations],['執行中',k.running],['等你回應',k.waiting,k.waiting>0],['代理',k.children],['總 tokens',fmtNum(k.total_tokens)]].concat(nodesAll?[['節點在線',nodesUp+'/'+nodesAll]]:[]);
      if(kp.children.length!==tiles.length){kp.textContent='';tiles.forEach(()=>{const d=enter(h('div','kpi'));d.append(h('div','n'),h('div','l'));kp.appendChild(d);});}
      tiles.forEach(([l,n,hot],i)=>{const d=kp.children[i];d.classList.toggle('hot',!!hot);
        const v=String(n==null?0:n);if(d.firstChild.textContent!==v)d.firstChild.textContent=v;if(d.lastChild.textContent!==l)d.lastChild.textContent=l;});
      const fc=$('#fleet-cnt');fc.hidden=!k.waiting;fc.textContent=k.waiting||'';
      const lh=$('#local-head'),head='本機：'+f.node+'|v'+f.version+'・'+f.model;
      if(lh.dataset.sig!==head){lh.dataset.sig=head;lh.textContent='';lh.append(h('h2',null,'本機：'+f.node),h('span','m','v'+f.version+'・'+f.model));}
      syncCards($('#fleet-grid'),f.items,null,'本機還沒有任務。到「工作台」交代第一件事。');
      const nb=$('#nodes');
      (f.nodes||[]).forEach(n=>{let sec=nb.querySelector('[data-node="'+CSS.escape(n.name)+'"]');
        if(!sec){sec=h('div');sec.dataset.node=n.name;sec.append(h('div','sect'),h('div','grid'));nb.appendChild(sec);}
        const hd=sec.firstChild,line=n.url+(n.ok?'・v'+(n.version||'?')+'・'+(n.model||'')+'・'+n.latency_ms+'ms':'');
        if(hd.dataset.sig!==line+(n.error||'')){hd.dataset.sig=line+(n.error||'');hd.textContent='';hd.append(h('h2',null,'節點：'+n.name),h('span','m',line));
          if(!n.ok)hd.appendChild(h('span','down','離線：'+(n.error||'')));}
        syncCards(sec.lastChild,n.ok?(n.items||[]):[],n,n.ok?'此節點沒有任務。':'');});
      [...nb.children].forEach(sec=>{if(!(f.nodes||[]).some(n=>n.name===sec.dataset.node))sec.remove();});
    }catch(e){console.error(e)}
  }
  function enter(el){el.classList.add('enter');el.addEventListener('animationend',()=>el.classList.remove('enter'),{once:true});return el;}
  function syncCards(grid,items,node,emptyText){
    // Keyed by conversation: unchanged cards stay put (a card being typed into is never replaced),
    // a card whose only change is the running time just updates that number.
    const seen=new Set();
    grid.querySelectorAll(':scope > .empty').forEach(e=>e.remove());
    items.forEach((it,i)=>{const key=it.id;seen.add(key);const sig=JSON.stringify(Object.assign({},it,{elapsed:null,last_activity:null}));
      let el=grid.querySelector(':scope > [data-key="'+CSS.escape(key)+'"]');
      if(el&&el.dataset.sig!==sig&&!el.contains(document.activeElement)){const fresh=card(it,node);fresh.dataset.key=key;fresh.dataset.sig=sig;el.replaceWith(fresh);el=fresh;}
      if(!el){el=enter(card(it,node));el.dataset.key=key;el.dataset.sig=sig;}
      const tm=el.querySelector('.tm'),txt=it.elapsed!=null?it.elapsed+' 秒':'';if(tm&&tm.textContent!==txt)tm.textContent=txt;
      if(grid.children[i]!==el)grid.insertBefore(el,grid.children[i]||null);});
    [...grid.children].forEach(el=>{if(el.dataset.key&&!seen.has(el.dataset.key))el.remove();});
    if(!items.length&&emptyText&&!grid.children.length)grid.appendChild(h('div','empty',emptyText));
  }
  function card(it,node){
    const mine=!node&&it.id===conv,c=h('div','fc'+(mine?' current':''));
    const base=node?'/api/nodes/'+encodeURIComponent(node.name)+'/conversations/'+it.id:'/api/conversations/'+it.id;
    const u=it.usage||{};
    const head=h('div','head');const pill=h('span','pill '+it.state);pill.append(h('span','dot '+it.state),document.createTextNode(fmtState(it.state)));head.appendChild(pill);
    if(mine)head.appendChild(h('span','pill running','目前開著'));
    head.appendChild(h('span','grow'));head.appendChild(h('span','tm',it.elapsed!=null?it.elapsed+' 秒':''));
    c.append(head,h('div','prev',it.preview||'（尚未送出訊息）'));
    const meta=h('div','meta');[fmtNum(u.total_tokens)+' tokens',(u.calls||0)+' 次模型呼叫',(it.tool_calls||0)+' 個步驟',it.model,'session '+it.session].forEach(t=>meta.appendChild(h('span',null,t)));c.appendChild(meta);
    const working=(it.agents||[]).filter(a=>a.status==='running');
    if(working.length){const kids=h('div','kids','代理：');working.forEach(a=>{const s=h('span',null,a.name+(a.role?'（'+a.role+'）':''));s.title=a.activity||a.task||'';kids.appendChild(s);});c.appendChild(kids);}
    else if(it.children.length){const kids=h('div','kids','子代理：');it.children.forEach(ch=>{const s=h('span',null,ch.name);s.title=ch.task;kids.appendChild(s);});c.appendChild(kids);}
    const ap=h('div','apprs');
    it.pending_approvals.forEach(a=>{const d=h('div','appr');d.appendChild(h('span','s',a.summary));
      const ok=h('button','btn primary sm','允許'),no=h('button','btn sm','拒絕');
      ok.onclick=()=>api(base+'/approvals',{method:'POST',body:{id:a.id,allow:true}}).then(renderFleet);
      no.onclick=()=>api(base+'/approvals',{method:'POST',body:{id:a.id,allow:false}}).then(renderFleet);
      d.append(ok,no);ap.appendChild(d);});
    (it.pending_questions||[]).forEach(q=>{
      const box=h('div','qbox');
      const qq=h('div','qq',q.question);box.appendChild(qq);
      const row=h('div','qrow');
      const reply=answer=>{answer=(answer||'').trim();if(!answer)return;
        api(base+'/answers',{method:'POST',body:{id:q.id,answer}}).then(renderFleet).catch(e=>{qq.textContent=q.question+'（送出失敗：'+e.message+'）';});};
      q.options.forEach(o=>{const b=h('button','btn sm',o.label);
        if(o.description)b.title=o.description;b.onclick=()=>reply(o.label);row.appendChild(b);});
      const field=h('input');field.placeholder='或自己輸入';
      imeSafeEnter(field,()=>reply(field.value));
      row.appendChild(field);box.appendChild(row);ap.appendChild(box);});
    c.appendChild(ap);
    const acts=h('div','acts');
    const open=h('button','btn sm'+(mine?' primary':''),node?'在節點開啟':(mine?'回到這個任務':'開啟'));
    if(node){open.onclick=()=>window.open(node.url,'_blank','noopener');}
    else{open.onclick=()=>openConversation(it);}
    acts.appendChild(open);
    if(it.running){const st=h('button','btn quiet sm','停止');st.onclick=()=>api(base+'/stop',{method:'POST',body:{}}).then(renderFleet);acts.appendChild(st);}
    c.appendChild(acts);
    return c;
  }
  // usage trend: one axis, one fixed hue per node, crosshair tooltip, legend, table view
  let usageSince='7d',usageData=null;
  const SERIES=8;
  function nodeColor(i){return 'var(--series-'+(Math.min(i,SERIES-1)+1)+')';}
  async function loadUsage(){
    try{
      const u=await api('/api/fleet/usage?since='+usageSince);
      const series=[{name:'本機：'+u.node,history:u.history}];
      (u.nodes||[]).forEach(n=>{if(n.ok)series.push({name:n.name,history:n.history});else series.push({name:n.name+'（離線）',history:null});});
      // more than 8 series fold into "其他" (never a generated hue)
      let shown=series.slice(0,SERIES);
      if(series.length>SERIES){const rest=series.slice(SERIES-1);const base=rest[0].history;shown=series.slice(0,SERIES-1);
        const other={name:'其他（'+rest.length+'）',history:base?{bucket:base.bucket,series:base.series.map((b,i)=>({start:b.start,total_tokens:rest.reduce((a,r)=>a+((r.history&&r.history.series[i])?r.history.series[i].total_tokens:0),0),calls:0,tool_calls:0,tasks:0}))}:null};shown.push(other);}
      usageData={bucket:(u.history||{}).bucket,series:shown};renderUsage();
    }catch(e){console.error(e)}
  }
  function fmtBucket(iso,bucket){const d=new Date(iso);return bucket==='hour'?(d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':00':(d.getMonth()+1)+'/'+d.getDate();}
  function swatch(si){const s=h('span','sw');s.style.background=nodeColor(si);return s;}
  function renderUsage(){
    const box=$('#usage-chart');box.textContent='';if(!usageData)return;
    const ref=usageData.series.find(s=>s.history);if(!ref){box.appendChild(h('div','empty','還沒有用量資料。'));return;}
    const buckets=ref.history.series,n=buckets.length,W=box.clientWidth||800,H=240,padL=56,padR=16,padT=12,padB=28;
    const top=Math.max(1,...usageData.series.flatMap(s=>s.history?s.history.series.map(b=>b.total_tokens):[0]));
    // Round tick steps (1, 2, 5 x 10^n): with a small maximum, quarters of it rounded to 0, 0, 1, 1, 1.
    let step=Math.max(1,Math.pow(10,Math.floor(Math.log10(top/4))));step=[1,2,5,10].map(m=>m*step).find(v=>v*4>=top)||step*10;
    const max=step*4;
    const x=i=>padL+(n<=1?0:(W-padL-padR)*i/(n-1)),y=v=>padT+(H-padT-padB)*(1-v/max);
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 '+W+' '+H);
    const grid=document.createElementNS(ns,'g');grid.setAttribute('class','grid');const axis=document.createElementNS(ns,'g');axis.setAttribute('class','axis');
    for(let t=0;t<=4;t++){const v=max*t/4,ln=document.createElementNS(ns,'line');ln.setAttribute('x1',padL);ln.setAttribute('x2',W-padR);ln.setAttribute('y1',y(v));ln.setAttribute('y2',y(v));grid.appendChild(ln);
      const tx=document.createElementNS(ns,'text');tx.setAttribute('x',padL-8);tx.setAttribute('y',y(v)+4);tx.setAttribute('text-anchor','end');tx.textContent=v>=1000?(Math.round(v/100)/10)+'k':Math.round(v);axis.appendChild(tx);}
    const ticks=Math.min(n,6);for(let k=0;k<ticks;k++){const i=Math.round(k*(n-1)/Math.max(1,ticks-1));const tx=document.createElementNS(ns,'text');tx.setAttribute('x',x(i));tx.setAttribute('y',H-8);tx.setAttribute('text-anchor',k===0?'start':k===ticks-1?'end':'middle');tx.textContent=fmtBucket(buckets[i].start,usageData.bucket);axis.appendChild(tx);}
    svg.append(grid,axis);
    usageData.series.forEach((s,si)=>{if(!s.history)return;const g=document.createElementNS(ns,'g');g.setAttribute('class','series');const p=document.createElementNS(ns,'path');
      p.setAttribute('d',s.history.series.map((b,i)=>(i?'L':'M')+x(i)+' '+y(b.total_tokens)).join(' '));p.setAttribute('stroke',nodeColor(si));g.appendChild(p);
      s.history.series.forEach((b,i)=>{if(!b.total_tokens)return;const c=document.createElementNS(ns,'circle');c.setAttribute('cx',x(i));c.setAttribute('cy',y(b.total_tokens));c.setAttribute('r',4);c.setAttribute('fill',nodeColor(si));g.appendChild(c);});
      svg.appendChild(g);});
    const cross=document.createElementNS(ns,'line');cross.setAttribute('class','crosshair');cross.setAttribute('y1',padT);cross.setAttribute('y2',H-padB);svg.appendChild(cross);
    const tip=h('div','viz-tip');box.append(svg,tip);
    svg.addEventListener('mousemove',ev=>{const r=svg.getBoundingClientRect();const px=(ev.clientX-r.left)*W/r.width;let i=0,best=1e9;for(let k=0;k<n;k++){const d=Math.abs(x(k)-px);if(d<best){best=d;i=k;}}
      cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.style.opacity=1;
      tip.textContent='';tip.appendChild(h('div','t',fmtBucket(buckets[i].start,usageData.bucket)));
      usageData.series.forEach((s,si)=>{const b=s.history?s.history.series[i]:null;const r2=h('div','r');r2.append(swatch(si),document.createTextNode(s.name+'：'+(b?fmtNum(b.total_tokens)+' tokens・'+b.calls+' 次呼叫':'—')));tip.appendChild(r2);});
      const left=Math.min(r.width-160,Math.max(0,(x(i)*r.width/W)+12));tip.style.left=left+'px';tip.style.top='8px';tip.style.opacity=1;});
    svg.addEventListener('mouseleave',()=>{cross.style.opacity=0;tip.style.opacity=0;});
    const lg=$('#usage-legend');lg.textContent='';usageData.series.forEach((s,si)=>{const sp=h('span');sp.append(swatch(si),document.createTextNode(s.name));lg.appendChild(sp);});
    const tb=$('#usage-table');tb.textContent='';const table=h('table'),hr=h('tr');hr.appendChild(h('th',null,'時段'));usageData.series.forEach(s=>hr.appendChild(h('th',null,s.name)));table.appendChild(hr);
    buckets.forEach((b,i)=>{const tr=h('tr');tr.appendChild(h('td',null,fmtBucket(b.start,usageData.bucket)));usageData.series.forEach(s=>tr.appendChild(h('td',null,s.history?fmtNum(s.history.series[i].total_tokens):'—')));table.appendChild(tr);});
    tb.appendChild(table);
  }
  document.querySelectorAll('#usage [data-since]').forEach(b=>b.onclick=()=>{usageSince=b.dataset.since;document.querySelectorAll('#usage [data-since]').forEach(o=>o.classList.toggle('on',o===b));loadUsage();});
  $('#usage-table-toggle').onclick=()=>{const t=$('#usage-table');t.hidden=!t.hidden;$('#usage-table-toggle').textContent=t.hidden?'表格':'圖表';$('#usage-chart').style.display=t.hidden?'':'none';};
  window.addEventListener('resize',()=>{if(view==='fleet')renderUsage();});

  // --- identity ---------------------------------------------------
  function saveToken(){try{token?localStorage.setItem('xh-token',token):localStorage.removeItem('xh-token')}catch(e){}}
  function loadToken(){try{token=localStorage.getItem('xh-token')||null}catch(e){token=null}}
  let gateMode='users';
  function showGate(on,mode){
    gateOn=on!==false;
    if(mode)gateMode=mode;
    const tokenMode=gateMode==='token';
    $('#gate-users').hidden=tokenMode;
    $('#gate-token-box').hidden=!tokenMode;
    // A hidden field marked required silently blocks the submit; disabled
    // fields are left out of validation, so disable whichever is not shown.
    $('#gate-user').disabled=$('#gate-pass').disabled=tokenMode;
    $('#gate-token').disabled=!tokenMode;
    $('#gate-sub').textContent=tokenMode
      ?'這台節點以存取權杖保護。權杖存放在啟動服務時指定的檔案裡，請向管理者索取。'
      :'請以你的帳號登入。';
    $('#gate-go').textContent=tokenMode?'進入':'登入';
    $('#gate').classList.toggle('on',gateOn);
    if(gateOn)setTimeout(()=>$(tokenMode?'#gate-token':'#gate-user').focus(),50);
  }
  async function signIn(user,password){
    const r=await fetch('/api/login',{method:'POST',
      headers:{'Content-Type':'application/json','X-XHarness-Client':'1'},
      body:JSON.stringify({user,password})});
    let d={};try{d=await r.json()}catch(e){}
    if(!r.ok)throw new Error(d.error||('HTTP '+r.status));
    token=d.token;saveToken();return d.user;
  }
  // The submit is driven by Enter, so an IME confirming a candidate must not send
  // a half-typed form: three checks, any one of them is enough.
  let gateComposing=false;
  ['#gate-user','#gate-pass','#gate-token'].forEach(sel=>{
    $(sel).addEventListener('compositionstart',()=>{gateComposing=true;});
    $(sel).addEventListener('compositionend',()=>{gateComposing=false;});
    $(sel).addEventListener('keydown',e=>{
      if(e.key==='Enter'&&(gateComposing||e.isComposing||e.keyCode===229))e.preventDefault();
    });
  });
  $('#gate-form').addEventListener('submit',async e=>{
    e.preventDefault();
    if(gateComposing)return;
    const btn=$('#gate-go');btn.disabled=true;$('#gate-err').textContent='';
    try{
      if(gateMode==='token'){
        const candidate=$('#gate-token').value.trim();
        // Check it before keeping it: a wrong token saved to storage would
        // only bring the gate straight back after the reload.
        const r=await fetch('/api/meta',{headers:{'Authorization':'Bearer '+candidate,'X-XHarness-Client':'1'}});
        if(!r.ok)throw new Error('權杖不正確');
        token=candidate;saveToken();
      }else{
        await signIn($('#gate-user').value.trim(),$('#gate-pass').value);
      }
      $('#gate-pass').value='';$('#gate-token').value='';showGate(false);location.reload();
    }catch(err){$('#gate-err').textContent=err.message;}
    finally{btn.disabled=false;}
  });
  $('#btn-logout').onclick=async()=>{
    try{await api('/api/logout',{method:'POST',body:{}})}catch(e){}
    token=null;saveToken();location.reload();
  };

  const APPROVAL_TEXT={prompt:'有副作用的動作會先問你',auto:'自動核准所有動作'};
  function applyMeta(m){
    meta=m;
    $('#model-name').textContent=m.model;$('#modelchip').title='模型 '+m.model+'・'+m.base_url;
    $('#menu-model').textContent=m.model;
    $('#menu-sandbox').textContent=m.sandbox||'未啟用';
    $('#menu-approval').textContent=APPROVAL_TEXT[m.approval]||m.approval;
    $('#menu-node').textContent=(m.node||'-')+'・v'+m.version;
    $('#modepill').textContent=APPROVAL_TEXT[m.approval]||('審批：'+m.approval);
    $('#hero-eyebrow').textContent='xHarness・'+(m.node||'本機')+'・'+m.model;
    $('#hero-sub').textContent='說清楚要做什麼，它會自己讀檔案、跑指令；'+(m.approval==='auto'?'目前設定為自動核准，請小心。':'要改東西之前一定先問你。')+(m.sandbox?'指令在沙箱（'+m.sandbox+'）裡執行。':'');
    me=m.user;admin=!!m.admin;
    if(m.identity&&me){
      $('#who-name').textContent=me.display;
      $('#who-initial').textContent=(me.display||me.name).trim().charAt(0);
      $('#menu-name').textContent=me.display;
      $('#menu-role').textContent=me.role==='admin'?'管理者':'一般使用者';
      $('#btn-logout').hidden=false;
    }
    $('#nav-admin').hidden=!admin;
    refreshQuota();
  }
  async function refreshQuota(){
    if(!me)return;
    try{
      const d=await api('/api/me');
      const q=d.quota&&d.quota.day;
      if(q&&q.limit){
        const pct=Math.min(100,Math.round(q.used/q.limit*100));
        $('#menu-quota-row').hidden=false;
        $('#menu-quota-bar').style.width=pct+'%';
        $('#menu-quota-text').textContent=pct+'%';
      }
    }catch(e){}
  }

  // --- admin ------------------------------------------------------
  let repSince='30d';
  function admShow(which){
    ['sec','rep','usr','ch'].forEach(k=>{$('#adm-'+k).hidden=(k!==which);});
    document.querySelectorAll('[data-adm]').forEach(b=>b.classList.toggle('on',b.dataset.adm===which));
    if(which==='sec')loadSecurity();
    if(which==='rep')loadReport();
    if(which==='usr')loadUsers();
    if(which==='ch')loadChannels();
  }
  document.querySelectorAll('[data-adm]').forEach(b=>{b.onclick=()=>admShow(b.dataset.adm);});
  const VERDICT={pass:'通過',warn:'有建議事項',fail:'有必須修正的項目'};
  function checkRow(state,label,title,detail,fix){
    const row=h('div','chk');row.append(h('span','st '+state,label));
    const bd=h('div','bd');bd.append(h('div','ti',title),h('div','de',detail));if(fix)bd.appendChild(h('div','fx',fix));row.appendChild(bd);return row;
  }
  async function loadSecurity(){
    const box=$('#sec-list');box.textContent='';
    try{
      const d=await api('/api/security-check');
      $('#sec-verdict').textContent='檢查結果：'+(VERDICT[d.verdict]||d.verdict)+'（通過 '+d.summary.pass+'、建議 '+d.summary.warn+'、必修 '+d.summary.fail+'）';
      d.items.forEach(it=>box.appendChild(checkRow(it.state,{pass:'通過',warn:'建議',fail:'必修'}[it.state]||it.state,it.title,it.detail,it.fix?'修法：'+it.fix:'')));
    }catch(e){$('#sec-verdict').textContent='檢查失敗：'+e.message;}
  }
  $('#sec-rerun').onclick=loadSecurity;
  function repCard(title,value,metaText){const b=h('div','b');b.append(h('h3',null,title),h('div','n',value),h('div','m',metaText||''));return b;}
  async function loadReport(){
    const cards=$('#rep-cards'),detail=$('#rep-detail');cards.textContent='';detail.textContent='';
    try{
      const d=await api('/api/usage-report?since='+encodeURIComponent(repSince));
      const gen=new Date(d.generated);
      $('#rep-range').textContent='近 '+d.since+'，產生於 '+gen.toLocaleString('zh-TW',{timeZone:'Asia/Taipei',hour12:false})+'（台北時間）';
      cards.append(
        repCard('多少人在用',String(d.people.active),d.people.accounts+' 個帳號・身分驗證 '+(d.people.identity==='on'?'已啟用':'未啟用')),
        repCard('花多少',fmtNum(d.spend.tokens)+' tokens',d.spend.cost.estimate!=null?('估算 '+fmtNum(d.spend.cost.estimate)):d.spend.cost.assumption),
        repCard('自建 vs 外購',fmtNum(d.hosting.self_hosted.tokens)+' / '+fmtNum(d.hosting.external.tokens),'地端自建 / 外購 API（tokens）'),
        repCard('設備',String(d.machines.configured_nodes)+' 個節點',d.machines.unreachable.length?('連不上 '+d.machines.unreachable.join('、')):'全部可連線'),
        repCard('對外串接',d.outbound.unsafe.length?'有缺口':'無缺口',d.outbound.unsafe.length?d.outbound.unsafe.join('、'):'沙箱、身分、對外連線皆符合建議'),
        repCard('健康與治理',String(d.health.issues.length)+' 項待處理',d.health.errors+' 次失敗・'+d.health.failed_signins+' 次登入失敗'));
      const hd=h('div','sect');hd.appendChild(h('h2',null,'用量前幾名'));detail.appendChild(hd);
      const table=h('table','tbl'),hr=h('tr');['使用者','任務','tokens','工具呼叫'].forEach(t=>hr.appendChild(h('th',null,t)));table.appendChild(hr);
      d.people.top.forEach(r=>{const tr=h('tr');[r.user,r.tasks,fmtNum(r.tokens),r.tool_calls].forEach(v=>tr.appendChild(h('td',null,String(v))));table.appendChild(tr);});
      detail.appendChild(table);
      if(d.health.issues.length){
        const h2=h('div','sect');h2.append(h('h2',null,'待處理事項'),h('span','m','報表不是只報喜：這些是系統自己的問題'));detail.appendChild(h2);
        d.health.issues.forEach(it=>detail.appendChild(checkRow('warn','待處理',it.item,it.detail,'建議：'+it.action)));
      }
    }catch(e){detail.textContent='報告產生失敗：'+e.message;}
  }
  document.querySelectorAll('#adm-rep [data-since]').forEach(b=>{
    b.onclick=()=>{repSince=b.dataset.since;document.querySelectorAll('#adm-rep [data-since]').forEach(x=>x.classList.toggle('on',x===b));loadReport();};
  });
  $('#rep-json').onclick=async()=>{
    try{
      const d=await api('/api/usage-report?since='+encodeURIComponent(repSince));
      const url=URL.createObjectURL(new Blob([JSON.stringify(d,null,2)],{type:'application/json'}));
      const a=document.createElement('a');a.href=url;a.download='xharness-usage-report.json';a.click();
      URL.revokeObjectURL(url);
    }catch(e){toast('下載失敗：'+e.message,true);}
  };
  async function loadUsers(){
    const box=$('#usr-list');box.textContent='';
    try{
      const d=await api('/api/users');
      const table=h('table','tbl'),hr=h('tr');['帳號','顯示名稱','角色','每日上限','狀態',''].forEach(t=>hr.appendChild(h('th',null,t)));table.appendChild(hr);
      d.users.forEach(u=>{
        const tr=h('tr');
        [u.name,u.display,u.role==='admin'?'管理者':'一般',u.quota_tokens_per_day?fmtNum(u.quota_tokens_per_day):'無限制'].forEach(v=>tr.appendChild(h('td',null,v)));
        const stt=h('td');stt.appendChild(h('span','pill '+(u.disabled?'skipped':'idle'),u.disabled?'已停用':'使用中'));tr.appendChild(stt);
        const td=h('td');
        if(!u.disabled){
          const b=h('button','btn sm quiet','停用');
          b.onclick=async()=>{
            if(!await askConfirm('停用 '+u.name+'？','歷史用量會保留，帳號不會被刪除；停用後這個人無法再登入。','停用'))return;
            try{await api('/api/users',{method:'POST',body:{action:'disable',user:u.name}});loadUsers();toast('已停用 '+u.name);}
            catch(e){$('#usr-err').textContent=e.message;}
          };
          td.appendChild(b);
        }
        tr.appendChild(td);table.appendChild(tr);
      });
      box.appendChild(table);
    }catch(e){box.textContent='讀取失敗：'+e.message;}
  }
  $('#usr-form').addEventListener('submit',async e=>{
    e.preventDefault();$('#usr-err').textContent='';
    try{
      await api('/api/users',{method:'POST',body:{action:'add',
        user:$('#usr-name').value.trim(),password:$('#usr-pass').value,
        role:$('#usr-role').value,display:$('#usr-display').value.trim()}});
      const name=$('#usr-name').value.trim();
      $('#usr-name').value='';$('#usr-pass').value='';$('#usr-display').value='';
      loadUsers();toast('已新增 '+name);
    }catch(err){$('#usr-err').textContent=err.message;}
  });
  // forms submit on Enter; an IME confirming a candidate must not
  function imeGuard(fields){
    fields.forEach(f=>{
      let busy=false;
      f.addEventListener('compositionstart',()=>{busy=true;});f.addEventListener('compositionend',()=>{busy=false;});
      f.addEventListener('keydown',e=>{if(e.key==='Enter'&&(busy||e.isComposing||e.keyCode===229))e.preventDefault();});
    });
  }
  imeGuard(['#usr-name','#usr-display','#usr-pass'].map(sel=>$(sel)));

  // --- notification channels ------------------------------------------
  // Loaded when the tab opens and after each action; nothing polls, so nothing redraws under the reader.
  let chData=null,chEditing=null;
  const CH_HOW={
    telegram:['在 Telegram 搜尋 @BotFather，傳 /newbot，照指示取名字，最後會拿到一串權杖（像 123456789:AAH…）。',
      '把權杖貼到下面，按「新增並驗證」。',
      '在 Telegram 搜尋你的機器人，傳任何一句話給它；回到本頁按「重新整理」，在卡片上按「授權」。'],
    line:['到 LINE Developers（developers.line.biz）建立 Provider，再建立一個 Messaging API channel，會一併建立 LINE 官方帳號。',
      '在 channel 的「Messaging API」分頁最下方按 Issue，複製 Channel access token（長效）。',
      '在「Basic settings」分頁最下方複製 Your user ID（U 開頭），再用手機掃 Messaging API 分頁的 QR code，把官方帳號加為好友。'],
    teams:['在 Teams 要收通知的頻道，按頻道名稱旁的「…」，選「工作流程」。',
      '選「收到 webhook 要求時張貼在頻道」，確認團隊與頻道後建立。',
      '複製它給的網址貼到下面。這個網址等同密碼，拿到的人就能往頻道發訊息，請勿外流。']
  };
  const CH_SECRET={telegram:'機器人權杖',line:'Channel access token',teams:'Webhook 網址'};
  const CH_DESC={telegram:'雙向：手機上直接下指令、按按鈕核准。',line:'只通知：用 LINE 官方帳號推播給你。',teams:'只通知：發到 Teams 頻道，團隊一起看。'};
  const CH_ACCOUNT={telegram:'機器人 ',line:'官方帳號 ',teams:'Teams '};
  function fmtTaipei(t){return new Date(t*1000).toLocaleString('zh-TW',{timeZone:'Asia/Taipei',hour12:false,month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})+'（台北時間）';}
  async function loadChannels(){
    try{chData=await api('/api/channels');}catch(e){$('#ch-list').textContent='讀取失敗：'+e.message;return;}
    renderChannels();
  }
  async function chAct(body,done){
    try{await api('/api/channels',{method:'POST',body});toast(done);}catch(e){toast(e.message,true);}
    await loadChannels();
  }
  function renderChannels(){
    const d=chData,alerts=$('#ch-alerts'),list=$('#ch-list');alerts.textContent='';list.textContent='';
    $('#ch-add').disabled=!d.available;
    if(!d.available)alerts.appendChild(checkRow('fail','必修','這台伺服器缺少加密套件','沒有它就無法安全保存金鑰，所以先不開放新增頻道。',
      '修法：在執行 xharness 的環境執行 pip install cryptography，然後重新啟動 xharness。'));
    if(d.config_telegram){
      const row=checkRow('warn','建議','Telegram 機器人目前寫在設定檔裡',
        '它照常運作（'+d.config_telegram.chats+' 個對象），但在網頁上看不到也改不了。搬進來之後由本頁管理，權杖改為加密保存。','');
      row.classList.add('chcheck');
      const b=h('button','btn sm primary','搬到網頁管理');b.onclick=()=>chAct({action:'import'},'已搬入，之後在本頁管理');
      row.querySelector('.bd').appendChild(b);alerts.appendChild(row);
    }
    if(!d.channels.length){
      const e=h('div','empty');
      e.append(h('b',null,'還沒有通知頻道'),document.createTextNode('按「新增頻道」選 Telegram、LINE 或 Teams；存好後按「測試發送」確認收得到。'));
      list.appendChild(e);
    }
    d.channels.forEach(c=>list.appendChild(channelCard(c)));
  }
  function channelCard(c){
    const card=h('div','chcard'+(c.enabled?'':' off')),hd=h('div','hd');
    hd.append(h('span','pill',c.type_label),h('h3',null,c.name),h('span','pill '+(c.enabled?'idle':'skipped'),c.enabled?'啟用中':'已停用'));
    card.appendChild(hd);
    card.appendChild(h('div','ln',CH_ACCOUNT[c.type]+(c.account||'')));
    if(c.type!=='teams'){
      const t=c.targets||[];
      card.appendChild(h('div','ln','對象：'+(t.length?t.map(x=>c.type==='line'?x.slice(0,5)+'…'+x.slice(-4):x).join('、'):'尚未授權任何人')));
    }
    const ev=(c.events||[]).map(k=>k==='done'&&c.min_seconds?chData.events[k]+'（'+c.min_seconds+' 秒以上）':chData.events[k]).join('・');
    card.appendChild(h('div','ln','通知：'+ev+(c.include_text?'；附上回覆內容':'；不附內容')));
    const last=c.last;
    card.appendChild(h('div','ln'+(last&&!last.ok?' bad':''),last?('最近一次'+(last.ok?'成功':'失敗')+'，'+fmtTaipei(last.at)+'：'+last.detail):'還沒送過；按「測試發送」確認收得到。'));
    if(c.type==='telegram'&&c.enabled)card.appendChild(pairBox(c));
    const acts=h('div','acts');
    const test=h('button','btn sm','測試發送');
    test.onclick=async()=>{
      test.disabled=true;test.textContent='傳送中…';
      try{const r=await api('/api/channels',{method:'POST',body:{action:'test',id:c.id}});toast(r.ok?'已送出，請看手機或頻道':'沒送成功：'+r.detail,!r.ok);}
      catch(e){toast(e.message,true);}
      await loadChannels();
    };
    const edit=h('button','btn sm quiet','編輯');edit.onclick=()=>openChForm(c);
    const tog=h('button','btn sm quiet',c.enabled?'停用':'啟用');
    tog.onclick=()=>chAct({action:c.enabled?'disable':'enable',id:c.id},c.enabled?'已停用 '+c.name:'已啟用 '+c.name);
    const del=h('button','btn sm quiet','刪除');
    del.onclick=async()=>{
      if(!await askConfirm('刪除「'+c.name+'」？','金鑰會一起刪掉，之後不會再收到這個頻道的通知。','刪除'))return;
      chAct({action:'delete',id:c.id},'已刪除 '+c.name);
    };
    acts.append(test,edit,tog,del);card.appendChild(acts);
    return card;
  }
  function pairBox(c){
    const box=h('div','chpair'),strangers=chData.strangers||[],bot=c.account||'你的機器人';
    if(!chData.telegram_running){box.appendChild(h('div',null,'機器人目前沒有在運作；按「測試發送」看原因。'));return box;}
    if(!strangers.length){
      box.appendChild(h('div',null,(c.targets||[]).length?'要加人：請對方在 Telegram 傳一句話給 '+bot+'，再按重新整理。'
        :'下一步：在 Telegram 搜尋 '+bot+'，傳任何一句話，再按重新整理。'));
    }else{
      box.appendChild(h('div',null,'這些人傳了訊息給機器人，還沒授權：'));
      strangers.forEach(s=>{
        const row=h('div','chprow'),who=(s.name||'（沒有名稱）')+(s.username?' @'+s.username:'');
        row.appendChild(h('span','grow',who+'，chat id '+s.chat));
        const b=h('button','btn sm primary','授權');
        b.onclick=async()=>{
          if(!await askConfirm('授權 '+who+'？','授權後他可以用 Telegram 指揮這台節點做事，需要核准的動作照樣會問。','授權'))return;
          chAct({action:'authorize',id:c.id,chat:String(s.chat)},'已授權 '+who);
        };
        row.appendChild(b);box.appendChild(row);
      });
    }
    const r=h('button','btn sm quiet','重新整理');r.onclick=loadChannels;box.appendChild(r);
    return box;
  }
  function openChForm(c){
    chEditing=c?{type:c.type,c}:{type:''};
    renderChForm();
    const f=$('#ch-form');f.hidden=false;f.scrollIntoView({behavior:'smooth',block:'nearest'});
  }
  function closeChForm(){chEditing=null;const f=$('#ch-form');f.hidden=true;f.textContent='';}
  function renderChForm(){
    const f=$('#ch-form'),c=chEditing.c,type=chEditing.type;f.textContent='';
    f.appendChild(h('h3',null,c?'編輯「'+c.name+'」':'新增通知頻道'));
    if(!c){
      const types=h('div','chtypes');
      Object.keys(chData.types).forEach(k=>{
        const taken=k==='telegram'&&chData.channels.some(x=>x.type==='telegram');
        const b=h('button','chtype'+(type===k?' on':''));b.type='button';b.disabled=taken;
        b.append(h('b',null,chData.types[k]),h('span',null,taken?'已經有一個機器人，請直接編輯它。':CH_DESC[k]));
        b.onclick=()=>{chEditing.type=k;renderChForm();};
        types.appendChild(b);
      });
      f.appendChild(types);
    }
    if(!type){
      const a=h('div','acts'),x=h('button','btn quiet','取消');x.type='button';x.onclick=closeChForm;a.appendChild(x);f.appendChild(a);
      return;
    }
    const how=h('ol','chhow');CH_HOW[type].forEach(t=>how.appendChild(h('li',null,t)));f.appendChild(how);
    const form=document.createElement('form');form.autocomplete='off';
    function row(label,input,hint){
      const r=h('div','chrow'),l=h('label',null,label),w=h('div');
      if(input.id&&/^(INPUT|TEXTAREA|SELECT)$/.test(input.tagName))l.htmlFor=input.id;
      w.appendChild(input);if(hint)w.appendChild(h('div','chhint',hint));r.append(l,w);form.appendChild(r);return input;
    }
    const field=(tag,props)=>Object.assign(h(tag,'field'),props);
    const name=row('名稱',field('input',{id:'chf-name',value:c?c.name:'',placeholder:chData.types[type],maxLength:40}));
    const secret=row(CH_SECRET[type],field('input',{id:'chf-secret',type:'password',autocomplete:'new-password',
      placeholder:c?'已加密保存；留空表示不變':'貼上'+(/^[A-Za-z]/.test(CH_SECRET[type])?' ':'')+CH_SECRET[type]}),'存好之後本頁不會再顯示，只用來發通知。');
    let targets=null;
    if(type!=='teams'){
      targets=row(type==='line'?'接收對象 User ID':'已授權的 chat id',field('textarea',{id:'chf-targets',value:(c?c.targets:[]).join('\n'),
        placeholder:type==='line'?'U 開頭 33 碼，一行一個':'可以先留空，存好後在卡片上授權'}),
        type==='line'?'一行一個。LINE 群組 ID 要有對外網址才拿得到，目前只支援傳給個人。':'一行一個數字，群組是負數。授權的人可以用 Telegram 指揮這台節點。');
    }
    const ev=h('div','chevents'),chosen=new Set(c?c.events:['done','error','approval','external']);
    Object.entries(chData.events).forEach(([k,label])=>{
      const l=h('label'),cb=document.createElement('input');cb.type='checkbox';cb.value=k;cb.checked=chosen.has(k);
      l.append(cb,document.createTextNode(label));ev.appendChild(l);
    });
    row('要通知的事件',ev);
    const mins=row('多久以上才通知',field('input',{id:'chf-min',type:'number',min:0,max:86400,value:c?c.min_seconds:30}),
      '秒，只影響「任務完成」：很快做完的時候你人還在電腦前，不必叫手機。失敗與核准一律通知。');
    const incl=document.createElement('input');incl.type='checkbox';incl.checked=c?!!c.include_text:type==='telegram';
    const il=h('label','chinc');il.append(incl,document.createTextNode('附上回覆內容（最多 800 字）'));
    row('內容',il,type==='telegram'?'這是你自己的機器人，預設附上。':'勾選後，回覆內容會經過 '+chData.types[type]+' 的伺服器；公務或含個資的內容建議不勾，到網頁看。');
    const err=h('div','err');form.appendChild(err);
    const acts=h('div','acts'),cancel=h('button','btn quiet','取消'),save=h('button','btn primary',c?'儲存並驗證':'新增並驗證');
    cancel.type='button';save.type='submit';cancel.onclick=closeChForm;acts.append(cancel,save);form.appendChild(acts);
    form.addEventListener('submit',async e=>{
      e.preventDefault();err.textContent='';save.disabled=true;const label=save.textContent;save.textContent='驗證中…';
      const body={action:'save',id:c?c.id:'',channel:{type,name:name.value.trim(),secret:secret.value.trim(),
        targets:targets?targets.value.split(/[\s,，、]+/).filter(Boolean):[],
        events:[...ev.querySelectorAll('input:checked')].map(x=>x.value),min_seconds:Number(mins.value||0),include_text:incl.checked}};
      try{
        await api('/api/channels',{method:'POST',body});
        toast(c?'已儲存 '+(body.channel.name||c.name):'已新增，按「測試發送」確認收得到');
        closeChForm();await loadChannels();
      }catch(x){err.textContent=x.message;save.disabled=false;save.textContent=label;}
    });
    imeGuard([name,secret,mins]);
    f.appendChild(form);
    (c?name:secret).focus({preventScroll:true});
  }
  $('#ch-add').onclick=()=>openChForm(null);

  // ---- menus and keys -------------------------------------------------------------------
  const whoMenu=$('#who-menu');
  $('#btn-who').onclick=e=>{
    e.stopPropagation();
    const open=whoMenu.classList.toggle('open');
    $('#btn-who').setAttribute('aria-expanded',String(open));
    if(open)refreshQuota();
  };
  document.addEventListener('click',e=>{if(!whoMenu.contains(e.target))whoMenu.classList.remove('open');});
  document.addEventListener('keydown',e=>{
    if(e.key!=='Escape')return;
    if($('#dlg').classList.contains('on')){$('#dlg-no').click();return;}
    if(whoMenu.classList.contains('open')){whoMenu.classList.remove('open');return;}
    if(document.body.classList.contains('review-open')){closeReview();return;}
    if(document.body.classList.contains('drawer-open')){closeDrawer();return;}
  });
  setInterval(()=>{if(!document.hidden&&!gateOn)refreshTasks();},5000);

  (async function init(){
    showView('chat');
    setState('idle');renderChanges();autosize();
    // The saved credential must be loaded before any request goes out: the
    // file list used to race ahead without it, so a stored token was never
    // used and every reload asked again.
    loadToken();
    try{applyMeta(await api('/api/meta'));}
    catch(e){if(!gateOn)addError(e.message);return;}
    loadFiles();   // also decides whether the attach button is shown
    refreshTasks(true);loadDayUsage();renderLanding();loadModels();
    try{commands=await api('/api/commands');}catch(e){}
    input.focus();
    // A page left open across an upgrade keeps running the old interface: say so once, with a reload button.
    const loaded=meta.version;let told=false;
    setInterval(async()=>{if(told||document.hidden||gateOn)return;
      try{const m=await api('/api/meta');if(m.version&&m.version!==loaded){told=true;
        const t=h('div','toast update');t.append(document.createTextNode('xHarness 已更新到 '+m.version+'，重新整理才會用到新版。'));
        const b=h('button',null,'重新整理');b.onclick=()=>location.reload();t.appendChild(b);document.body.appendChild(t);}}catch(e){}   // top, clear of the composer
    },60000);
  })();
})();
</script>
</body>
</html>
"""
