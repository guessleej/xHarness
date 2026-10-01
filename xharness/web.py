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
from .session import SessionLog, messages_from_events, sessions_dir
from .telegram import TelegramBridge, telegram_settings
from .telemetry import BudgetExceeded

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
#: Callers that are not a signed-in person: the single-operator install with
#: no [auth] at all, and a hub forwarding an action with the node token.
OPERATOR = "operator"
NODE = "node"
APPROVAL_TIMEOUT_SECONDS = 600
TICKET_TTL_SECONDS = 60
MAX_BODY_BYTES = 1_000_000
CSRF_HEADER = "X-XHarness-Client"
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#bf181f"/>'
    '<path d="M19 19 L45 45 M45 19 L19 45" stroke="#ffffff" stroke-width="9" stroke-linecap="round"/>'
    "</svg>"
)


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
        self.preview = ""
        self.created = time.time()
        self.started_at: float | None = None
        self.last_activity = time.time()
        self.children: dict[str, str] = {}  # active subagents: name -> task
        self.turns = 0

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
        if self.running:
            state = "waiting" if pending else "running"
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
            "usage": self.usage(),
            "created": self.created,
            "started_at": self.started_at,
            "last_activity": self.last_activity,
            "elapsed": round(time.time() - self.started_at, 1) if self.running and self.started_at else None,
            "children": [{"name": name, "task": task[:120]} for name, task in self.children.items()],
            "tool_calls": self.turns,
            "pending_approvals": pending,
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
        self.nodes = {node.name: node for node in load_nodes(config.fleet)}
        self.telegram: TelegramBridge | None = None
        self.users = UserStore(getattr(config, "auth", {}) or {})
        self.file_rules = workspace.FileRules.from_config(getattr(config, "files", None))
        #: Recorded for the security self-check, which has to know how we are exposed.
        self.bound_host: str | None = None
        self.has_token = False

    def start_channels(self) -> None:
        """Mount the channels named in [channels.*]; a bad channel config is fatal on purpose."""
        settings = telegram_settings(self.config)
        if settings:
            self.telegram = TelegramBridge(self, settings)
            self.telegram.start()
            print(f"telegram channel on for {len(self.telegram.allowed)} chat(s)", file=sys.stderr)

    def notify(self, text: str, chats: list[int] | None = None) -> int:
        """Push a message out every mounted channel; returns recipients reached."""
        return self.telegram.notify(text, chats) if self.telegram else 0

    def create(
        self,
        resume: str | None = None,
        extra_system: str | None = None,
        user: User | None = None,
    ) -> Conversation:
        harness = self.harness_factory(resume, user) if self._factory_takes_user else self.harness_factory(resume)
        session = harness.ctx.optional("session")
        session_id = session.id if session else "-"
        conv_id = secrets.token_hex(6)
        initial = messages_from_events(SessionLog.load(resume, principal_name(user))) if resume else []
        holder: dict[str, Conversation] = {}

        def prompt(summary: str) -> bool:
            conv = holder["conv"]
            approval_id = secrets.token_hex(4)
            approval = Approval(summary=summary)
            conv.approvals[approval_id] = approval
            conv.push({"type": "approval_request", "id": approval_id, "summary": summary})
            approval.event.wait(APPROVAL_TIMEOUT_SECONDS)
            decision = bool(approval.decision)
            conv.push({"type": "approval_resolved", "id": approval_id, "allowed": decision})
            return decision

        agent = Agent(
            harness.ctx,
            AgentOptions(
                cwd=workspace.ensure(principal_name(user)),
                # The operator's AGENTS.md lives where the server was started;
                # with per-user workspaces nobody would see it otherwise.
                site_instructions_dir=os.getcwd(),
                system_prompt=self.config.system_prompt,
                system_suffix=extra_system,
                max_turns=self.config.max_turns or AgentOptions.max_turns,
                approval_mode=self.approval_mode,
                project_instructions=self.config.project_instructions,
                prompt=prompt,
                initial_messages=initial,
                on_delta=lambda text: holder["conv"].push({"type": "delta", "text": text}),
                on_tool_start=lambda name, args: holder["conv"].push(
                    {"type": "tool_start", "name": name, "args": args[:500]}
                ),
                on_tool_end=lambda name, output, is_error: holder["conv"].push(
                    {"type": "tool_end", "name": name, "ok": not is_error, "output": output[:2000]}
                ),
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
        if initial:
            for message in initial:
                if message["role"] in ("user", "assistant") and message.get("content"):
                    conv.push({"type": "history", "role": message["role"], "text": message["content"]})
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

    def send(self, conv: Conversation, text: str) -> bool:
        with conv.cond:
            if conv.running:
                return False
            conv.running = True
            conv.started_at = time.time()
        conv.preview = text[:80]
        conv.push({"type": "user", "text": text})

        def run() -> None:
            try:
                answer = conv.agent.run(text)
                conv.push({"type": "done", "answer": answer, "usage": conv.usage()})
            except Exception as error:  # noqa: BLE001 - surfaced to the browser
                # An exception often repeats the URL or header that failed, which
                # is exactly where a token would be.
                detail = scrub(f"{type(error).__name__}: {error}")
                conv.push({"type": "error", "message": detail, "usage": conv.usage()})
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

    def approve(self, conv: Conversation, approval_id: str, allow: bool) -> bool:
        approval = conv.approvals.get(approval_id)
        if approval is None or approval.decision is not None:
            return False
        approval.decision = allow
        approval.event.set()
        return True

    def stop(self, conv: Conversation) -> bool:
        """Operator brake: stop before the next model call and deny pending approvals."""
        if not conv.running:
            return False
        conv.agent.stop()
        for approval in conv.approvals.values():
            if approval.decision is None:
                approval.decision = False
                approval.event.set()
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
                "children": sum(len(item["children"]) for item in items),
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
        return {"tools": items, "mcp_servers": servers, "sandbox": sandbox.name if sandbox else None,
                "denied": sorted(denied)}

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
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
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

        def _admin_only(self) -> bool:
            if is_admin(getattr(self, "principal", None)):
                return True
            self._json(403, {"error": "administrator only"})
            return False

        def _body(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
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
                })
                return
            if parts[1:] == ["conversations"]:
                self._json(200, app.list(self.principal))
                return
            if parts[1:] == ["sessions"]:
                self._json(200, SessionLog.list(principal_name(self.principal))[:50])
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
                    "history": history(since=since, bucket=bucket),
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
            body = self._body()
            if body is None:
                return
            if parts[1:] == ["login"]:
                self._login(body)
                return
            if not self._guard():
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
                    conv = app.create(str(resume) if resume else None, user=owner)
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
                    text = str(body.get("text") or "").strip()
                    if not text:
                        self._json(400, {"error": "text is required"})
                        return
                    if not app.send(conv, text):
                        self._json(409, {"error": "conversation is busy"})
                        return
                    self._json(202, {"ok": True})
                    return
                if parts[3] == "approvals":
                    ok = app.approve(conv, str(body.get("id") or ""), bool(body.get("allow")))
                    self._json(200 if ok else 404, {"ok": ok})
                    return
                if parts[3] == "stop":
                    ok = app.stop(conv)
                    self._json(200 if ok else 409, {"ok": ok})
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
<style>
:root{
  --brand:#bf181f;--brand-glow:#e0484f;--brand-dark:#9c1218;
  --bg:#f4f7f9;--card:#ffffff;--ink:#16202a;--ink-2:#3d4b56;--ink-3:#6b7a85;
  --tint:rgba(191,24,31,.08);--shadow:0 12px 32px rgba(22,32,42,.11);--shadow-sm:0 4px 14px rgba(22,32,42,.08);
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --font:"Microsoft JhengHei","PingFang TC","Noto Sans TC",system-ui,sans-serif;
}
:root[data-theme="dark"]{
  --bg:linear-gradient(158deg,#0d1922,#13242f 56%,#1a3040);--card:#13242f;--ink:#e8eef3;--ink-2:#b7c3cc;--ink-3:#8593a0;
  --tint:rgba(224,72,79,.14);--shadow:0 12px 32px rgba(0,0,0,.35);--shadow-sm:0 4px 14px rgba(0,0,0,.3);
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{font-family:var(--font);background:var(--bg);color:var(--ink);line-height:1.6;background-attachment:fixed}
a{color:var(--brand)}
.nav{position:sticky;top:0;z-index:20;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  background:rgba(244,247,249,.72);transition:box-shadow .25s,background .25s}
:root[data-theme="dark"] .nav{background:rgba(13,25,34,.66)}
.nav.scrolled{box-shadow:var(--shadow-sm)}
.nav .in{max-width:1180px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:8px;flex-wrap:nowrap}
.nav .sp{flex:1}
.navlinks{display:flex;gap:2px;align-items:center;min-width:0}
.navlink{font:inherit;font-size:14px;font-weight:600;padding:8px 13px;border-radius:10px;border:0;cursor:pointer;
  background:transparent;color:var(--ink-2);white-space:nowrap;transition:background .2s,color .2s}
.navlink:hover{background:var(--tint);color:var(--ink)}
.navlink.on{background:var(--tint);color:var(--brand)}
/* user menu: identity and the things that belong to it, out of the toolbar */
.who-wrap{position:relative}
.whobtn{display:flex;align-items:center;gap:8px;font:inherit;font-size:14px;padding:6px 10px 6px 6px;border-radius:999px;
  border:0;cursor:pointer;background:var(--tint);color:var(--ink);white-space:nowrap}
.avatar{width:28px;height:28px;border-radius:50%;background:var(--brand);color:#fff;display:grid;place-items:center;
  font-size:13px;font-weight:700;flex:none}
.menu{position:absolute;right:0;top:calc(100% + 8px);min-width:248px;background:var(--card);border-radius:14px;
  box-shadow:var(--shadow);padding:8px;display:none;z-index:40}
.menu.open{display:block}
.menu .mhead{padding:10px 12px 8px}
.menu .mname{font-weight:700}
.menu .mrole{font-size:13px;color:var(--ink-3)}
.menu .mrow{padding:9px 12px;font-size:14px;color:var(--ink-2);display:flex;justify-content:space-between;gap:12px}
.menu .mitem{width:100%;text-align:left;font:inherit;font-size:14px;padding:9px 12px;border-radius:9px;border:0;
  cursor:pointer;background:transparent;color:var(--ink)}
.menu .mitem:hover{background:var(--tint)}
.menu .msep{height:1px;background:rgba(107,122,133,.16);margin:6px 8px}
.meter{height:5px;border-radius:99px;background:rgba(107,122,133,.18);overflow:hidden;margin-top:6px}
.meter i{display:block;height:100%;background:var(--brand);border-radius:99px}
/* run status lives with the transcript, not in the toolbar */
.statusbar{gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 14px}
.brand{font-weight:800;font-size:20px;letter-spacing:.2px;color:var(--brand);text-decoration:none}
.chips{display:flex;gap:8px;flex-wrap:wrap;flex:1;min-width:0}
.chip{font-size:13px;padding:4px 10px;border-radius:999px;background:var(--tint);color:var(--ink-2);white-space:nowrap;max-width:280px;overflow:hidden;text-overflow:ellipsis}
.btn{font:inherit;font-size:14px;padding:8px 14px;border-radius:10px;border:0;cursor:pointer;background:var(--card);color:var(--ink);box-shadow:var(--shadow-sm)}
/* sign-in gate: a full page, not a dialog -- there is nothing behind it to click back to */
.gate{position:fixed;inset:0;z-index:60;display:none;place-items:center;background:var(--bg);background-attachment:fixed;padding:16px}
.gate.on{display:grid}
.gate .card{width:min(420px,100%);background:var(--card);border-radius:18px;padding:32px 34px;box-shadow:var(--shadow)}
.gate h1{margin:0 0 4px;font-size:24px;color:var(--brand)}
.gate p{margin:0 0 20px;color:var(--ink-3);font-size:14px}
.gate label{display:block;font-size:13px;color:var(--ink-2);margin:12px 0 6px}
.gate input{width:100%;font:inherit;padding:10px 12px;border-radius:10px;background:transparent;color:var(--ink);
  border:1px solid rgba(107,122,133,.35)}
.gate input:focus{outline:none;border-color:var(--brand)}
.gate .err{margin-top:12px;font-size:14px;color:var(--brand);min-height:20px}
.gate .btn{width:100%;margin-top:18px}
/* admin surfaces */
.adm{max-width:1080px;margin:0 auto;padding:0 16px 32px}
.admtabs{display:flex;gap:8px;margin:18px 0 14px;flex-wrap:wrap}
.scroll{max-height:56vh;overflow:auto;padding-right:4px}
.chk{display:flex;gap:14px;align-items:flex-start;padding:14px 16px;border-radius:12px;background:var(--card);
  box-shadow:var(--shadow-sm);margin-bottom:8px}
.chk .bd{flex:1;min-width:0}
.chk .ti{font-weight:700;font-size:15px}
.chk .de{font-size:14px;color:var(--ink-2);margin-top:2px}
.chk .fx{font-size:13px;color:var(--ink-3);margin-top:6px;font-family:var(--mono);word-break:break-all}
.st{font-size:13px;padding:3px 11px;border-radius:999px;white-space:nowrap;font-weight:700}
.st.pass{background:rgba(46,125,50,.13);color:#2e7d32}
.st.warn{background:rgba(217,119,6,.16);color:#a85f05}
.st.fail{background:var(--tint);color:var(--brand)}
:root[data-theme="dark"] .st.pass{background:rgba(46,125,50,.2);color:#7bc47f}
:root[data-theme="dark"] .st.warn{background:rgba(217,119,6,.22);color:#e0a052}
.rep{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(248px,1fr))}
.rep .b{background:var(--card);border-radius:14px;padding:16px 18px;box-shadow:var(--shadow-sm)}
.rep .b h3{margin:0 0 8px;font-size:14px;color:var(--ink-3);font-weight:700}
.rep .b .n{font-size:26px;font-weight:800;color:var(--brand);line-height:1.2}
.rep .b .m{font-size:13px;color:var(--ink-2);margin-top:6px}
.tbl{width:100%;border-collapse:collapse;font-size:14px}
.tbl th{text-align:left;font-size:13px;color:var(--ink-3);padding:8px 10px;font-weight:700}
.tbl td{padding:8px 10px;background:var(--card)}
.tbl tr td:first-child{border-radius:10px 0 0 10px}
.tbl tr td:last-child{border-radius:0 10px 10px 0}
.uform{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.uform input,.uform select{font:inherit;font-size:14px;padding:8px 10px;border-radius:10px;background:var(--card);
  color:var(--ink);border:1px solid rgba(107,122,133,.3)}
@media (max-width:640px){
  /* Phone layout: identity stays on the first row next to the wordmark, the
     navigation drops to its own scrollable row. Nothing overlaps, and no
     hamburger hides four buttons that fit on one line. */
  .nav .in{flex-wrap:wrap;gap:6px;padding:8px 16px}
  .nav .sp{display:none}
  .brand{order:1}
  .who-wrap{order:2;margin-left:auto}
  .whobtn{padding:4px}
  #who-name{display:none}
  .navlinks{order:3;flex-basis:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
  .navlinks::-webkit-scrollbar{display:none}
  .navlink{flex:none}
  .menu{right:0;min-width:min(280px,calc(100vw - 32px))}
  #hero{padding:24px 0 20px}
  #hero h1{font-size:30px}
  #hero p{font-size:15px}
  .starters{grid-template-columns:1fr;gap:10px}
  .starter{min-height:0}
  main{padding-bottom:196px}   /* the composer is two lines tall on a phone */
  body.view-chat.landing main{min-height:0;display:block;padding-top:16px}
  .statusbar{margin-bottom:10px}
  .chip{max-width:none;flex-shrink:0}
  .scroll{max-height:none;overflow:visible}
  .tbl{display:block;overflow-x:auto;white-space:nowrap}
  .uform{flex-direction:column;align-items:stretch}
  .uform input,.uform select,.uform .btn{width:100%}
  .chk{flex-direction:column;gap:8px}
  .st{align-self:flex-start}
}
.btn.primary{background:var(--brand);color:#fff}
.btn.primary:hover{background:var(--brand-dark)}
.btn:disabled{opacity:.55;cursor:not-allowed}
main{max-width:1080px;margin:0 auto;padding:10px 16px 170px}
body.view-chat.landing main{min-height:calc(100vh - 210px);display:flex;flex-direction:column;justify-content:center;padding-bottom:40px}
body.view-chat.landing #hero{padding-top:0}
.hero{padding:26px 0 8px}
.hero h1{margin:0 0 4px;font-size:30px;line-height:1.2}
.hero p{margin:0;color:var(--ink-3);font-size:15px}
/* the landing hero only: centred, larger, and lit */
#hero{position:relative;padding:64px 0 30px;text-align:center;overflow:hidden}
#hero::before{content:"";position:absolute;inset:-40% -20% auto;height:420px;pointer-events:none;z-index:-1;
  background:radial-gradient(closest-side,rgba(191,24,31,.13),transparent 70%);
  animation:drift 26s ease-in-out infinite}
@keyframes drift{0%,100%{transform:translate3d(-4%,0,0) scale(1)}50%{transform:translate3d(6%,3%,0) scale(1.12)}}
#hero h1{margin:0 0 10px;font-size:40px;line-height:1.15;letter-spacing:-.5px}
#hero p{margin:0 auto;color:var(--ink-3);font-size:16px;max-width:560px}
.starters{display:grid;grid-template-columns:repeat(auto-fit,minmax(232px,1fr));gap:12px;margin:26px 0 8px}
.starter{display:flex;flex-direction:column;gap:6px;text-align:left;font:inherit;padding:16px 18px;min-height:104px;
  border-radius:15px;border:0;cursor:pointer;
  background:var(--card);color:var(--ink);box-shadow:var(--shadow-sm);
  transition:transform .22s cubic-bezier(.22,.8,.3,1),box-shadow .22s;
  opacity:0;transform:translateY(16px);animation:rise .6s cubic-bezier(.22,.8,.3,1) forwards}
.starter:nth-child(2){animation-delay:.07s}
.starter:nth-child(3){animation-delay:.14s}
.starter:nth-child(4){animation-delay:.21s}
.starter:hover{transform:translateY(-3px);box-shadow:var(--shadow)}
.starter .sk{display:block;font-size:12px;font-weight:700;letter-spacing:.4px;color:var(--brand)}
.starter .sv{display:block;font-size:15px;line-height:1.5}
@keyframes rise{to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){
  #hero::before{animation:none}
  .starter{animation:none;opacity:1;transform:none}
}
.msg{background:var(--card);border-radius:16px;padding:16px 18px;margin:14px 0;box-shadow:var(--shadow-sm);
  opacity:0;transform:translateY(14px);animation:rv .45s cubic-bezier(.22,.8,.3,1) forwards}
@keyframes rv{to{opacity:1;transform:none}}
.msg.user{background:var(--tint);box-shadow:none}
.msg .who{font-size:12px;letter-spacing:.6px;text-transform:uppercase;color:var(--ink-3);margin-bottom:6px}
.msg .body{white-space:pre-wrap;word-break:break-word}
.msg.tool{padding:10px 14px;font-family:var(--mono);font-size:13px;color:var(--ink-2)}
.msg.tool summary{cursor:pointer;font-family:var(--font);font-size:14px;color:var(--ink)}
.msg.tool pre{margin:8px 0 0;white-space:pre-wrap;word-break:break-word;max-height:280px;overflow:auto}
.msg.tool .ok{color:#2e7d32}.msg.tool .bad{color:var(--brand)}
.msg.approval{position:relative;padding-top:20px}
.msg.approval:before{content:"";position:absolute;left:18px;right:18px;top:0;height:3px;border-radius:0 0 3px 3px;background:var(--brand)}
.msg.approval .actions{display:flex;gap:10px;margin-top:12px}
.msg.error{color:var(--brand)}
.cursor{display:inline-block;width:8px;height:1.1em;vertical-align:-2px;background:var(--brand);animation:blink 1s steps(2) infinite;border-radius:2px}
@keyframes blink{50%{opacity:0}}
.composer{position:fixed;left:0;right:0;bottom:0;z-index:15;backdrop-filter:blur(14px);background:rgba(244,247,249,.82)}
:root[data-theme="dark"] .composer{background:rgba(13,25,34,.8)}
.composer .in{max-width:860px;margin:0 auto;padding:16px;display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;font:inherit;font-size:15px;line-height:1.5;padding:14px 16px;border:0;border-radius:16px;resize:none;min-height:54px;max-height:220px;
  background:var(--card);color:var(--ink);box-shadow:var(--shadow);outline:none}
textarea:focus{box-shadow:0 0 0 3px rgba(191,24,31,.18),var(--shadow-sm)}
.hint{font-size:12px;color:var(--ink-3);max-width:860px;margin:0 auto;padding:0 16px 12px;text-align:center}
.attach{font:inherit;font-size:14px;padding:0;width:54px;height:54px;border-radius:16px;border:0;cursor:pointer;
  background:var(--card);color:var(--ink-2);box-shadow:var(--shadow);flex:none;display:grid;place-items:center}
.attach:hover{color:var(--brand)}
.attach svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round}
.composer.over .in{outline:2px dashed var(--brand);outline-offset:-8px;border-radius:18px}
.queued{max-width:860px;margin:0 auto;padding:0 16px 8px;display:flex;gap:8px;flex-wrap:wrap}
.qfile{display:flex;align-items:center;gap:8px;font-size:13px;padding:5px 10px;border-radius:999px;
  background:var(--tint);color:var(--ink-2)}
.qfile button{font:inherit;font-size:13px;line-height:1;border:0;background:transparent;color:var(--ink-3);cursor:pointer;padding:0}
.qfile button:hover{color:var(--brand)}
.frow{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:10px}
.frow:hover{background:var(--tint)}
.frow .fn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}
.frow .fz{font-size:12px;color:var(--ink-3);white-space:nowrap}
.frow button{font:inherit;font-size:13px;border:0;background:transparent;color:var(--ink-3);cursor:pointer}
.frow button:hover{color:var(--brand)}
.panel{position:fixed;top:0;right:0;bottom:0;width:min(420px,92vw);z-index:30;background:var(--card);box-shadow:var(--shadow);
  transform:translateX(105%);transition:transform .35s cubic-bezier(.22,.8,.3,1);display:flex;flex-direction:column}
.panel.open{transform:none}
.panel header{padding:18px 18px 10px;display:flex;justify-content:space-between;align-items:center}
.panel h2{margin:0;font-size:18px}
.panel .list{overflow:auto;padding:0 12px 18px;flex:1}
.item{padding:12px 12px;border-radius:12px;cursor:pointer;margin:6px 0;background:var(--bg)}
:root[data-theme="dark"] .item{background:rgba(255,255,255,.04)}
.item.active{box-shadow:inset 0 3px 0 var(--brand)}
.item .t{font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item .m{font-size:12px;color:var(--ink-3);display:flex;gap:8px;align-items:center}
.dot{width:8px;height:8px;border-radius:50%;background:#2e7d32;display:inline-block}
.dot.run{background:var(--brand);animation:blink 1s steps(2) infinite}
.backdrop{position:fixed;inset:0;background:rgba(22,32,42,.35);z-index:25;opacity:0;pointer-events:none;transition:opacity .3s}
.backdrop.open{opacity:1;pointer-events:auto}
.sec{font-size:12px;letter-spacing:.6px;text-transform:uppercase;color:var(--ink-3);padding:12px 12px 4px}
/* Which view is on screen is decided here, by one body class, and nowhere
   else. Setting .hidden or an inline display from JS was how the starter
   cards ended up sitting above the admin page: `hidden` loses to an
   explicit `display:grid` in the stylesheet. */
#fleet,.adm,#hero,#starters,#statusbar{display:none}
body.view-fleet #fleet{display:block}
body.view-admin .adm{display:block}
body.view-chat.landing #hero{display:block}
body.view-chat.landing #starters{display:grid}
body.view-chat:not(.landing) #statusbar{display:flex}
body:not(.view-chat) #transcript{display:none}
body:not(.view-chat) .composer{display:none}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
.kpi{background:var(--card);border-radius:14px;padding:14px 16px;box-shadow:var(--shadow-sm)}
.kpi .n{font-size:28px;font-weight:800;line-height:1.1}
.kpi .l{font-size:12px;color:var(--ink-3);letter-spacing:.5px;text-transform:uppercase;margin-top:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.fc{background:var(--card);border-radius:16px;padding:16px 18px;box-shadow:var(--shadow-sm);position:relative;display:flex;flex-direction:column;gap:8px}
.fc.running:before,.fc.waiting:before{content:"";position:absolute;left:18px;right:18px;top:0;height:3px;border-radius:0 0 3px 3px;background:var(--brand)}
.fc.waiting:before{background:#d97706}
.fc .head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.fc .state{font-size:12px;padding:3px 9px;border-radius:999px;background:var(--tint);color:var(--ink-2)}
.fc.current{background:var(--tint)}
.fc .cur{font-size:12px;padding:3px 9px;border-radius:999px;background:var(--brand);color:#fff;margin-left:auto}
.fc .state.running{color:var(--brand)}.fc .state.waiting{color:#d97706}.fc .state.idle{color:#2e7d32}
.fc .prev{font-size:15px;line-height:1.45;max-height:4.3em;overflow:hidden}
.fc .meta{font-size:12px;color:var(--ink-3);display:flex;flex-wrap:wrap;gap:6px 12px}
.fc .kids{font-size:12px;color:var(--ink-2)}
.fc .kids span{display:inline-block;background:var(--tint);border-radius:999px;padding:2px 8px;margin:2px 4px 0 0}
.fc .appr{font-size:13px;background:var(--tint);border-radius:10px;padding:8px 10px}
.fc .acts{display:flex;gap:8px;margin-top:auto;flex-wrap:wrap}
.btn.sm{padding:6px 10px;font-size:13px}
.btn.ghost{background:transparent;box-shadow:none;color:var(--brand)}
.empty{color:var(--ink-3);padding:40px 0;text-align:center}
/* usage chart: reference categorical palette, light and dark steps both validated */
.viz-root{--surface-1:#ffffff;--text-primary:#16202a;--text-secondary:#6b7a85;--grid:rgba(22,32,42,.08);
  --series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100;--series-5:#e87ba4;--series-6:#008300;--series-7:#4a3aa7;--series-8:#e34948;
  background:var(--surface-1);border-radius:16px;padding:14px 16px 10px;box-shadow:var(--shadow-sm)}
:root[data-theme="dark"] .viz-root{--surface-1:#13242f;--text-primary:#e8eef3;--text-secondary:#8593a0;--grid:rgba(255,255,255,.08);
  --series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;--series-6:#008300;--series-7:#9085e9;--series-8:#e66767}
.viz-filters{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.viz-sp{flex:1}
.viz-chart{position:relative;width:100%;height:260px}
.viz-chart svg{width:100%;height:100%;display:block;font-family:var(--font)}
.viz-chart .grid line{stroke:var(--grid);stroke-width:1}
.viz-chart .axis text{fill:var(--text-secondary);font-size:12px}
.viz-chart .series path{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.viz-chart .series circle{stroke:var(--surface-1);stroke-width:2}
.viz-chart .crosshair{stroke:var(--text-secondary);stroke-width:1;stroke-dasharray:3 3;opacity:0}
.viz-tip{position:absolute;pointer-events:none;background:var(--card);color:var(--ink);font-size:12px;line-height:1.5;padding:8px 10px;border-radius:10px;box-shadow:var(--shadow);opacity:0;transition:opacity .12s;min-width:140px}
.viz-tip .t{color:var(--text-secondary);margin-bottom:2px}
.viz-tip .r{display:flex;align-items:center;gap:6px}
.viz-tip .sw{width:10px;height:10px;border-radius:3px;display:inline-block}
.viz-legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:6px;font-size:13px;color:var(--text-secondary)}
.viz-legend .sw{width:12px;height:12px;border-radius:3px;display:inline-block;vertical-align:-1px;margin-right:6px}
.viz-table table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
.viz-table th,.viz-table td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid);color:var(--text-primary)}
.viz-table th:first-child,.viz-table td:first-child{text-align:left;color:var(--text-secondary)}
.nodehead{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:22px 0 10px}
.nodehead h2{margin:0;font-size:20px}
.nodehead .m{font-size:12px;color:var(--ink-3)}
.nodehead .down{color:var(--brand);font-size:13px}
@media (max-width:640px){.hero h1{font-size:24px}.chip{max-width:160px}}
</style>
</head>
<body>
<div class="gate" id="gate"><div class="card">
  <h1>xHarness</h1>
  <p id="gate-sub">請以你的帳號登入。</p>
  <form id="gate-form" autocomplete="on">
    <label for="gate-user">帳號</label>
    <input id="gate-user" name="username" autocomplete="username" autocapitalize="off" spellcheck="false" required>
    <label for="gate-pass">密碼</label>
    <input id="gate-pass" name="password" type="password" autocomplete="current-password" required>
    <div class="err" id="gate-err"></div>
    <button class="btn primary" id="gate-go" type="submit">登入</button>
  </form>
</div></div>

<nav class="nav" id="nav"><div class="in">
  <a class="brand" href="#">xHarness</a>
  <div class="navlinks">
    <button class="navlink on" id="btn-new">新對話</button>
    <button class="navlink" id="btn-fleet">艦隊</button>
    <button class="navlink" id="btn-admin" hidden>管理</button>
    <button class="navlink" id="btn-list">紀錄</button>
  </div>
  <span class="sp"></span>
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
      <div class="mrow" id="menu-quota-row" hidden>
        <span style="flex:1">今日配額
          <div class="meter"><i id="menu-quota-bar" style="width:0%"></i></div>
        </span>
        <span id="menu-quota-text"></span>
      </div>
      <div class="msep"></div>
      <button class="mitem" id="btn-theme">切換深色／淺色</button>
      <button class="mitem" id="btn-logout" hidden>登出</button>
    </div>
  </div>
</div></nav>

<main>
  <section class="hero" id="hero">
    <h1>管得住的 agent。</h1>
    <p>輸入任務，模型會用工具讀寫檔案與執行指令。有副作用的動作一律先問過你，每一步都留下記錄。</p>
  </section>
  <div class="starters" id="starters">
    <button class="starter" data-task="摘要這個工作目錄在做什麼，先看有哪些檔案再回答。">
      <span class="sk">認識專案</span>
      <span class="sv">摘要這個工作目錄在做什麼</span>
    </button>
    <button class="starter" data-task="列出這個目錄最近修改過的 10 個檔案，說明各自的用途。">
      <span class="sk">找東西</span>
      <span class="sv">最近改過哪些檔案，各自在做什麼</span>
    </button>
    <button class="starter" data-task="對這個目錄跑一次 security_scan，並用白話解釋每一項發現。">
      <span class="sk">資安</span>
      <span class="sv">掃一次這個專案的資安問題</span>
    </button>
    <button class="starter" data-task="這個專案如果要交給新同事接手，最需要先讀的三個檔案是哪些？為什麼？">
      <span class="sk">交接</span>
      <span class="sv">新同事接手該先讀哪三個檔案</span>
    </button>
  </div>
  <div class="statusbar" id="statusbar">
    <span class="chip" id="chip-conv">對話：尚未建立</span>
    <span class="chip" id="chip-status">閒置</span>
    <span class="chip" id="chip-usage">usage: 0 tokens</span>
  </div>
  <div id="transcript"></div>
  <section id="fleet">
    <div class="hero"><h1>艦隊視圖</h1><p>所有進行中的對話與子代理，一眼看完狀態、用量與等待中的許可；可就地審批或停止。</p></div>
    <div class="kpis" id="kpis"></div>
    <div class="nodehead" id="local-head"></div>
    <div class="grid" id="fleet-grid"></div>
    <div id="nodes"></div>
    <div class="nodehead"><h2>用量趨勢</h2><span class="m">每個節點的 token 用量隨時間；資料來自各節點磁碟上的 session 記錄</span></div>
    <div class="viz-root" id="usage">
      <div class="viz-filters">
        <button class="btn sm" data-since="24h">24 小時</button>
        <button class="btn sm primary" data-since="7d">7 天</button>
        <button class="btn sm" data-since="30d">30 天</button>
        <span class="viz-sp"></span>
        <button class="btn sm" id="usage-table-toggle">表格</button>
      </div>
      <div id="usage-chart" class="viz-chart"></div>
      <div id="usage-legend" class="viz-legend"></div>
      <div id="usage-table" class="viz-table" hidden></div>
    </div>
  </section>

  <section class="adm" id="admin">
    <div class="hero"><h1>管理</h1><p>只有管理者看得到：這台機器的資安狀態、全體用量與帳號。</p></div>
    <div class="admtabs">
      <button class="btn sm primary" data-adm="sec">機密防護檢查</button>
      <button class="btn sm" data-adm="rep">服務使用報告</button>
      <button class="btn sm" data-adm="usr">帳號</button>
    </div>

    <div id="adm-sec">
      <div class="nodehead"><h2 id="sec-verdict">檢查中…</h2>
        <span class="m">檢查機密放在哪裡、誰讀得到。本頁永遠不顯示機密內容。</span></div>
      <div class="admtabs"><button class="btn sm" id="sec-rerun">重新檢查</button></div>
      <div class="scroll" id="sec-list"></div>
    </div>

    <div id="adm-rep" hidden>
      <div class="nodehead"><h2>服務使用報告</h2><span class="m" id="rep-range">近 30 天</span></div>
      <div class="admtabs">
        <button class="btn sm" data-since="7d">7 天</button>
        <button class="btn sm primary" data-since="30d">30 天</button>
        <button class="btn sm" data-since="90d">90 天</button>
        <span class="viz-sp"></span>
        <button class="btn sm" id="rep-json">下載 JSON</button>
      </div>
      <div class="rep" id="rep-cards"></div>
      <div class="scroll" id="rep-detail"></div>
    </div>

    <div id="adm-usr" hidden>
      <div class="nodehead"><h2>帳號</h2><span class="m">停用不刪除，歷史用量才歸得了戶。</span></div>
      <form class="uform" id="usr-form">
        <input id="usr-name" placeholder="帳號" autocapitalize="off" spellcheck="false" required>
        <input id="usr-display" placeholder="顯示名稱">
        <input id="usr-pass" type="password" placeholder="密碼（至少 8 字）" autocomplete="new-password" required>
        <select id="usr-role"><option value="user">一般使用者</option><option value="admin">管理者</option></select>
        <button class="btn sm primary" type="submit">新增</button>
      </form>
      <div class="err" id="usr-err"></div>
      <div class="scroll" id="usr-list"></div>
    </div>
  </section>
</main>

<div class="composer" id="composer">
  <div class="queued" id="queued"></div>
  <div class="in">
    <button class="attach" id="btn-attach" title="附加檔案" aria-label="附加檔案">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 11.5 12.5 20a5 5 0 0 1-7-7l8.5-8.5a3.3 3.3 0 0 1 4.7 4.7l-8.5 8.5a1.7 1.7 0 0 1-2.4-2.4l7.8-7.8"/></svg>
    </button>
    <input type="file" id="file-input" multiple hidden>
    <textarea id="input" rows="1" placeholder="輸入任務…（Enter 送出，Shift+Enter 換行）"></textarea>
    <button class="btn primary" id="btn-send">送出</button>
  </div>
  <div class="hint" id="hint">尚未建立對話；送出第一則訊息會自動建立。</div>
</div>

<div class="backdrop" id="backdrop"></div>
<aside class="panel" id="panel">
  <header><h2>對話與紀錄</h2><button class="btn" id="btn-close">關閉</button></header>
  <div class="list">
    <div class="sec">進行中的對話</div>
    <div id="convs"></div>
    <div class="sec">磁碟上的 session（可接續）</div>
    <div id="sessions"></div>
    <div class="sec">工具（agent 能做什麼）</div>
    <div id="tools"></div>
    <div class="sec">我的檔案（上傳後 agent 讀得到）</div>
    <div id="files"></div>
    <div class="sec">記憶（agent 記得什麼）</div>
    <div id="memory"></div>
  </div>
</aside>

<script>
(function(){
  const $=s=>document.querySelector(s);
  const transcript=$('#transcript'),input=$('#input'),hint=$('#hint');
  let conv=null,es=null,token=null,assistantEl=null,assistantText='',composing=false,convPreview='';
  let me=null,admin=false,gateOn=false,admOn=false,repSince='30d';
  function shortTitle(t){t=(t||'').trim();return t?(t.length>18?t.slice(0,18)+'…':t):'尚未送出訊息';}
  function setCurrent(id,preview){conv=id;convPreview=preview||'';
    $('#chip-conv').textContent=id?'對話：'+shortTitle(convPreview):'對話：尚未建立';}
  // The empty state (hero + starters) and the run status are mutually exclusive:
  // a toolbar full of chips before anything has happened is noise.
  function showEmptyState(on){document.body.classList.toggle('landing',on);}
  function showView(name){   // 'chat' | 'fleet' | 'admin'
    const body=document.body;
    ['chat','fleet','admin'].forEach(key=>body.classList.toggle('view-'+key,key===name));
    setNav(name);
  }
  function setNav(which){
    [['chat','#btn-new'],['fleet','#btn-fleet'],['admin','#btn-admin']].forEach(([key,sel])=>{
      const el=$(sel); if(el)el.classList.toggle('on',key===which);
    });
  }
  let fleetTimer=null,fleetOn=false;

  // theme
  const root=document.documentElement;
  try{const t=localStorage.getItem('xh-theme');if(t)root.dataset.theme=t;}catch(e){}
  $('#btn-theme').onclick=()=>{const next=root.dataset.theme==='dark'?'light':'dark';root.dataset.theme=next;try{localStorage.setItem('xh-theme',next)}catch(e){}};
  window.addEventListener('scroll',()=>$('#nav').classList.toggle('scrolled',scrollY>20));

  // api
  async function api(path,opts){
    opts=opts||{};const headers=Object.assign({'Content-Type':'application/json','X-XHarness-Client':'1'},opts.headers||{});
    if(token)headers['Authorization']='Bearer '+token;
    const r=await fetch(path,{method:opts.method||'GET',headers,body:opts.body?JSON.stringify(opts.body):undefined});
    if(r.status===401){
      let d={};try{d=await r.json()}catch(e){}
      if(d.auth==='users'){showGate();throw new Error('請先登入');}
      token=prompt('這個伺服器需要存取權杖，請貼上啟動時設定的 token：');
      if(token){saveToken();return api(path,opts);}
    }
    if(!r.ok){let d={};try{d=await r.json()}catch(e){}throw new Error(d.error||('HTTP '+r.status));}
    return r.status===204?null:r.json();
  }

  // rendering
  function el(cls,who,body){const d=document.createElement('div');d.className='msg '+cls;
    if(who){const w=document.createElement('div');w.className='who';w.textContent=who;d.appendChild(w);}
    const b=document.createElement('div');b.className='body';if(body!=null)b.textContent=body;d.appendChild(b);transcript.appendChild(d);d.scrollIntoView({block:'end',behavior:'smooth'});return d;}
  function setStatus(t,running){$('#chip-status').textContent=t;$('#btn-send').disabled=!!running;}
  function setUsage(u){if(!u)return;$('#chip-usage').textContent='usage: '+u.total_tokens+' tokens · '+u.calls+' calls · '+u.tool_calls+' tools';}
  function finishAssistant(){if(assistantEl){const c=assistantEl.querySelector('.cursor');if(c)c.remove();}assistantEl=null;assistantText='';}

  function handle(ev){
    if(ev.type==='user'){showEmptyState(false);el('user','你',ev.text);setStatus('執行中…',true);if(!convPreview)setCurrent(conv,ev.text);}
    else if(ev.type==='history'){showEmptyState(false);el(ev.role==='user'?'user':'assistant',ev.role==='user'?'你':'xHarness',ev.text);if(ev.role==='user'&&!convPreview)setCurrent(conv,ev.text);}
    else if(ev.type==='delta'){
      if(!assistantEl){assistantEl=el('assistant','xHarness','');const c=document.createElement('span');c.className='cursor';assistantEl.querySelector('.body').appendChild(c);}
      assistantText+=ev.text;const body=assistantEl.querySelector('.body');body.textContent=assistantText;const c=document.createElement('span');c.className='cursor';body.appendChild(c);
    }
    else if(ev.type==='tool_start'){finishAssistant();const d=document.createElement('div');d.className='msg tool';
      d.innerHTML='<details><summary>工具 <b></b> <span class="st">執行中…</span></summary><pre></pre></details>';
      d.querySelector('b').textContent=ev.name;d.querySelector('pre').textContent=ev.args;d.dataset.tool=ev.name;transcript.appendChild(d);d.scrollIntoView({block:'end'});}
    else if(ev.type==='tool_end'){const items=[...transcript.querySelectorAll('.msg.tool')].reverse();const d=items.find(x=>x.dataset.tool===ev.name&&!x.dataset.done);
      if(d){d.dataset.done='1';const st=d.querySelector('.st');st.textContent=ev.ok?'完成':'失敗';st.className='st '+(ev.ok?'ok':'bad');const pre=d.querySelector('pre');pre.textContent+='\n\n'+ev.output;}}
    else if(ev.type==='approval_request'){finishAssistant();const d=el('approval','需要你的許可',ev.summary);
      const a=document.createElement('div');a.className='actions';
      const ok=document.createElement('button');ok.className='btn primary';ok.textContent='允許';
      const no=document.createElement('button');no.className='btn';no.textContent='拒絕';
      const decide=async allow=>{ok.disabled=no.disabled=true;await api('/api/conversations/'+conv+'/approvals',{method:'POST',body:{id:ev.id,allow}});d.querySelector('.who').textContent=allow?'已允許':'已拒絕';};
      ok.onclick=()=>decide(true);no.onclick=()=>decide(false);a.append(ok,no);d.appendChild(a);}
    else if(ev.type==='approval_resolved'){}
    else if(ev.type==='subagent_start'){finishAssistant();const d=el('tool','子代理啟動',ev.name+'：'+ev.task);}
    else if(ev.type==='subagent_end'){el('tool','子代理結束',ev.name+'：'+(ev.ok?'完成':'失敗'));}
    else if(ev.type==='stop_requested'){el('error','操作者要求停止','將在下一次模型呼叫前停止；等待中的許可一律拒絕。');}
    else if(ev.type==='done'){finishAssistant();if(!ev.answer&&assistantText==='')el('assistant','xHarness','(沒有文字回覆)');setStatus('閒置',false);setUsage(ev.usage);refreshList();}
    else if(ev.type==='error'){finishAssistant();el('error','錯誤',ev.message);setStatus('閒置',false);setUsage(ev.usage);refreshList();}
  }

  async function connect(id,after){
    if(es)es.close();
    let url='/api/conversations/'+id+'/events?after='+(after||0);
    if(token){const t=await api('/api/tickets',{method:'POST'});url+='&ticket='+encodeURIComponent(t.ticket);}
    es=new EventSource(url);
    es.onmessage=e=>{try{handle(JSON.parse(e.data))}catch(err){console.error(err)}};
    es.onerror=()=>{/* EventSource reconnects with Last-Event-ID */};
  }

  async function newConversation(resume){
    const c=await api('/api/conversations',{method:'POST',body:resume?{resume}:{}});
    setCurrent(c.id,'');transcript.innerHTML='';assistantEl=null;assistantText='';showEmptyState(!resume);
    hint.textContent='對話 '+c.id+' · session '+c.session;setStatus('閒置',false);await connect(conv,0);refreshList();closePanel();if(fleetOn)toggleFleet(false);input.focus();return c;
  }

  async function send(){
    const typed=input.value.trim();
    // Attached files are named in the message itself, so the agent knows they
    // exist and where they are without any special protocol.
    const text=typed+takeQueued();
    if(!text.trim())return;
    if(!conv)await newConversation();
    try{await api('/api/conversations/'+conv+'/messages',{method:'POST',body:{text}});input.value='';autosize();}
    catch(e){el('error','錯誤',e.message);}
  }

  // composer: IME-safe Enter (compositionstart/end flag + isComposing + keyCode 229)
  input.addEventListener('compositionstart',()=>composing=true);
  input.addEventListener('compositionend',()=>{composing=false});
  input.addEventListener('keydown',e=>{
    if(e.key!=='Enter'||e.shiftKey)return;
    if(composing||e.isComposing||e.keyCode===229)return;
    e.preventDefault();send();
  });
  function autosize(){input.style.height='auto';input.style.height=Math.min(220,input.scrollHeight)+'px';}
  input.addEventListener('input',autosize);
  $('#btn-send').onclick=send;
  $('#btn-new').onclick=()=>{admOn=fleetOn=false;showView('chat');newConversation();};
  document.querySelectorAll('.starter').forEach(button=>{
    button.onclick=()=>{input.value=button.dataset.task;input.focus();send();};
  });

  // panel
  const panel=$('#panel'),backdrop=$('#backdrop');
  function openPanel(){panel.classList.add('open');backdrop.classList.add('open');refreshList();loadFiles();}
  function closePanel(){panel.classList.remove('open');backdrop.classList.remove('open');}
  $('#btn-list').onclick=openPanel;$('#btn-close').onclick=closePanel;backdrop.onclick=closePanel;

  async function refreshList(){
    try{
      const convs=await api('/api/conversations');const box=$('#convs');box.innerHTML='';
      if(!convs.length)box.innerHTML='<div class="item"><div class="m">還沒有對話</div></div>';
      convs.forEach(c=>{const d=document.createElement('div');d.className='item'+(c.id===conv?' active':'');
        d.innerHTML='<div class="t"></div><div class="m"><span class="dot'+(c.running?' run':'')+'"></span><span></span></div>';
        d.querySelector('.t').textContent=c.preview||'(尚未送出訊息)';
        d.querySelector('.m span:last-child').textContent=(c.usage?c.usage.total_tokens+' tokens · ':'')+'session '+c.session;
        d.onclick=async()=>{if(c.id===conv){closePanel();if(fleetOn)toggleFleet(false);return;}setCurrent(c.id,c.preview);transcript.innerHTML='';assistantEl=null;assistantText='';showEmptyState(false);hint.textContent='對話 '+c.id+' · session '+c.session;await connect(conv,0);closePanel();if(fleetOn)toggleFleet(false);};
        box.appendChild(d);});
      const sessions=await api('/api/sessions');const sb=$('#sessions');sb.innerHTML='';
      sessions.slice(0,30).forEach(s=>{const d=document.createElement('div');d.className='item';
        d.innerHTML='<div class="t"></div><div class="m"><span></span></div>';d.querySelector('.t').textContent=s.id;
        d.querySelector('.m span').textContent=new Date(s.mtime*1000).toLocaleString('zh-TW');d.onclick=()=>newConversation(s.id);sb.appendChild(d);});
      const tl=await api('/api/tools');const tb=$('#tools');tb.innerHTML='';
      const hdr=document.createElement('div');hdr.className='item';hdr.style.cursor='default';
      hdr.innerHTML='<div class="m"><span></span></div>';
      hdr.querySelector('span').textContent=tl.tools.length+' 個工具 · 沙箱 '+(tl.sandbox||'關')+(tl.mcp_servers.length?' · MCP server '+tl.mcp_servers.map(sv=>sv.name+'('+sv.tools+(sv.ok?'':'，未啟動')+')').join('、'):' · 未設定 MCP server');
      tb.appendChild(hdr);
      tl.tools.forEach(t=>{const d=document.createElement('div');d.className='item';d.style.cursor='default';
        d.innerHTML='<div class="t"></div><div class="m"><span></span></div>';
        d.querySelector('.t').textContent=t.name+(t.mutating?' · 有副作用（走審批）':'');d.querySelector('.t').title=t.description;
        d.querySelector('.m span').textContent=(t.source==='mcp'?'MCP：'+t.server+' · ':'內建 · ')+t.description.slice(0,90);tb.appendChild(d);});
      const mem=await api('/api/memory');const mb=$('#memory');mb.innerHTML='';
      if(!mem.length)mb.innerHTML='<div class="item"><div class="m">還沒有記憶</div></div>';
      let lastTopic=null;
      mem.forEach(m=>{if(m.topic!==lastTopic){lastTopic=m.topic;const h=document.createElement('div');h.className='sec';h.textContent='主題：'+m.topic;mb.appendChild(h);}
        const d=document.createElement('div');d.className='item';d.style.cursor='default';
        d.innerHTML='<div class="t"></div><div class="m"><span></span></div>';d.querySelector('.t').textContent=m.name+' · '+m.description;
        d.querySelector('.m span').textContent=m.kind+(m.updated?' · '+m.updated.slice(0,10):'')+(m.stale?' · 待確認 '+m.age_days+' 天':'');
        if(m.stale)d.querySelector('.m span').style.color='#d97706';mb.appendChild(d);});
    }catch(e){console.error(e)}
  }

  // fleet view
  function fmtState(s){return s==='running'?'執行中':s==='waiting'?'等待許可':'閒置';}
  async function renderFleet(){
    try{
      const f=await api('/api/fleet');const k=Object.assign({},f.summary);
      (f.nodes||[]).forEach(n=>{const s=n.summary||{};['conversations','running','waiting','children','total_tokens'].forEach(key=>{k[key]=(k[key]||0)+(s[key]||0);});});
      const nodesUp=(f.nodes||[]).filter(n=>n.ok).length,nodesAll=(f.nodes||[]).length;
      $('#kpis').innerHTML=[['對話',k.conversations],['執行中',k.running],['等待許可',k.waiting],['子代理',k.children],['總 tokens',k.total_tokens]].concat(nodesAll?[['節點在線',nodesUp+'/'+nodesAll]]:[])
        .map(([l,n])=>'<div class="kpi"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>').join('');
      $('#local-head').innerHTML='<h2>本機：'+f.node+'</h2><span class="m">v'+f.version+' · '+f.model+'</span>';
      const g=$('#fleet-grid');g.innerHTML='';
      if(!f.items.length){g.innerHTML='<div class="empty">本機還沒有對話。按「新對話」開始。</div>';}
      f.items.forEach(it=>g.appendChild(card(it,null)));
      const nb=$('#nodes');nb.innerHTML='';
      (f.nodes||[]).forEach(n=>{const h=document.createElement('div');h.className='nodehead';
        h.innerHTML='<h2>節點：'+n.name+'</h2><span class="m">'+n.url+(n.ok?' · v'+(n.version||'?')+' · '+(n.model||'')+' · '+n.latency_ms+'ms':'')+'</span>'+(n.ok?'':'<span class="down">離線：'+(n.error||'')+'</span>');
        nb.appendChild(h);const gg=document.createElement('div');gg.className='grid';
        if(n.ok&&!n.items.length)gg.innerHTML='<div class="empty">此節點沒有對話。</div>';
        (n.items||[]).forEach(it=>gg.appendChild(card(it,n)));nb.appendChild(gg);});
    }catch(e){console.error(e)}
  }
  function card(it,node){const c=document.createElement('div');c.className='fc '+it.state+(!node&&it.id===conv?' current':'');
        const base=node?'/api/nodes/'+encodeURIComponent(node.name)+'/conversations/'+it.id:'/api/conversations/'+it.id;
        const u=it.usage||{};
        c.innerHTML='<div class="head"><span class="state '+it.state+'">'+fmtState(it.state)+'</span>'+((!node&&it.id===conv)?'<span class="cur">目前對話</span>':'')+'<span class="meta">'+(it.elapsed!=null?it.elapsed+'s':'')+'</span></div>'
          +'<div class="prev"></div>'
          +'<div class="meta"><span>'+(u.total_tokens||0)+' tokens</span><span>'+(u.calls||0)+' calls</span><span>'+(it.tool_calls||0)+' tools</span><span>'+it.model+'</span><span>session '+it.session+'</span></div>'
          +(it.children.length?'<div class="kids">子代理：'+it.children.map(ch=>'<span title="'+ch.task.replace(/"/g,'')+'">'+ch.name+'</span>').join('')+'</div>':'')
          +'<div class="apprs"></div><div class="acts"></div>';
        c.querySelector('.prev').textContent=it.preview||'(尚未送出訊息)';
        const ap=c.querySelector('.apprs');
        it.pending_approvals.forEach(a=>{const d=document.createElement('div');d.className='appr';d.textContent=a.summary;
          const ok=document.createElement('button');ok.className='btn primary sm';ok.textContent='允許';ok.style.marginLeft='8px';
          const no=document.createElement('button');no.className='btn sm';no.textContent='拒絕';no.style.marginLeft='6px';
          ok.onclick=()=>api(base+'/approvals',{method:'POST',body:{id:a.id,allow:true}}).then(renderFleet);
          no.onclick=()=>api(base+'/approvals',{method:'POST',body:{id:a.id,allow:false}}).then(renderFleet);
          d.append(ok,no);ap.appendChild(d);});
        const acts=c.querySelector('.acts');
        const open=document.createElement('button');open.className='btn sm'+((!node&&it.id===conv)?' primary':'');open.textContent=node?'在節點開啟':((it.id===conv)?'回到此對話':'開啟');
        if(node){open.onclick=()=>window.open(node.url,'_blank','noopener');}
        else{open.onclick=async()=>{setCurrent(it.id,it.preview);transcript.innerHTML='';assistantEl=null;assistantText='';showEmptyState(false);hint.textContent='對話 '+it.id+' · session '+it.session;await connect(conv,0);toggleFleet(false);};}
        acts.appendChild(open);
        if(it.running){const st=document.createElement('button');st.className='btn ghost sm';st.textContent='停止';st.onclick=()=>api(base+'/stop',{method:'POST',body:{}}).then(renderFleet);acts.appendChild(st);}
        return c;}
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
  function renderUsage(){
    const box=$('#usage-chart');box.innerHTML='';if(!usageData)return;
    const ref=usageData.series.find(s=>s.history);if(!ref){box.innerHTML='<div class="empty">還沒有用量資料。</div>';return;}
    const buckets=ref.history.series,n=buckets.length,W=box.clientWidth||800,H=260,padL=56,padR=16,padT=12,padB=28;
    const max=Math.max(1,...usageData.series.flatMap(s=>s.history?s.history.series.map(b=>b.total_tokens):[0]));
    const x=i=>padL+(n<=1?0:(W-padL-padR)*i/(n-1)),y=v=>padT+(H-padT-padB)*(1-v/max);
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 '+W+' '+H);
    const grid=document.createElementNS(ns,'g');grid.setAttribute('class','grid');const axis=document.createElementNS(ns,'g');axis.setAttribute('class','axis');
    for(let t=0;t<=4;t++){const v=max*t/4,ln=document.createElementNS(ns,'line');ln.setAttribute('x1',padL);ln.setAttribute('x2',W-padR);ln.setAttribute('y1',y(v));ln.setAttribute('y2',y(v));grid.appendChild(ln);
      const tx=document.createElementNS(ns,'text');tx.setAttribute('x',padL-8);tx.setAttribute('y',y(v)+4);tx.setAttribute('text-anchor','end');tx.textContent=v>=1000?(Math.round(v/100)/10)+'k':Math.round(v);axis.appendChild(tx);}
    const ticks=Math.min(n,6);for(let k=0;k<ticks;k++){const i=Math.round(k*(n-1)/Math.max(1,ticks-1));const tx=document.createElementNS(ns,'text');tx.setAttribute('x',x(i));tx.setAttribute('y',H-8);tx.setAttribute('text-anchor',k===0?'start':k===ticks-1?'end':'middle');tx.textContent=fmtBucket(buckets[i].start,usageData.bucket);axis.appendChild(tx);}
    svg.append(grid,axis);
    usageData.series.forEach((s,si)=>{if(!s.history)return;const g=document.createElementNS(ns,'g');g.setAttribute('class','series');const p=document.createElementNS(ns,'path');
      p.setAttribute('d',s.history.series.map((b,i)=>(i?'L':'M')+x(i)+' '+y(b.total_tokens)).join(' '));p.setAttribute('stroke',nodeColor(si));g.appendChild(p);
      // 8px markers only where a bucket has activity, so the line stays thin
      s.history.series.forEach((b,i)=>{if(!b.total_tokens)return;const c=document.createElementNS(ns,'circle');c.setAttribute('cx',x(i));c.setAttribute('cy',y(b.total_tokens));c.setAttribute('r',4);c.setAttribute('fill',nodeColor(si));g.appendChild(c);});
      svg.appendChild(g);});
    const cross=document.createElementNS(ns,'line');cross.setAttribute('class','crosshair');cross.setAttribute('y1',padT);cross.setAttribute('y2',H-padB);svg.appendChild(cross);
    const tip=document.createElement('div');tip.className='viz-tip';box.append(svg,tip);
    svg.addEventListener('mousemove',ev=>{const r=svg.getBoundingClientRect();const px=(ev.clientX-r.left)*W/r.width;let i=0,best=1e9;for(let k=0;k<n;k++){const d=Math.abs(x(k)-px);if(d<best){best=d;i=k;}}
      cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.style.opacity=1;
      tip.innerHTML='<div class="t">'+fmtBucket(buckets[i].start,usageData.bucket)+'</div>'+usageData.series.map((s,si)=>{const b=s.history?s.history.series[i]:null;return '<div class="r"><span class="sw" style="background:'+nodeColor(si)+'"></span>'+s.name+'：'+(b?b.total_tokens+' tokens · '+b.calls+' calls':'—')+'</div>';}).join('');
      const left=Math.min(r.width-160,Math.max(0,(x(i)*r.width/W)+12));tip.style.left=left+'px';tip.style.top='8px';tip.style.opacity=1;});
    svg.addEventListener('mouseleave',()=>{cross.style.opacity=0;tip.style.opacity=0;});
    $('#usage-legend').innerHTML=usageData.series.map((s,si)=>'<span><span class="sw" style="background:'+nodeColor(si)+'"></span>'+s.name+'</span>').join('');
    const tb=$('#usage-table');tb.innerHTML='<table><thead><tr><th>時段</th>'+usageData.series.map(s=>'<th>'+s.name+'</th>').join('')+'</tr></thead><tbody>'+buckets.map((b,i)=>'<tr><td>'+fmtBucket(b.start,usageData.bucket)+'</td>'+usageData.series.map(s=>'<td>'+(s.history?s.history.series[i].total_tokens:'—')+'</td>').join('')+'</tr>').join('')+'</tbody></table>';
  }
  document.querySelectorAll('#usage [data-since]').forEach(b=>b.onclick=()=>{usageSince=b.dataset.since;document.querySelectorAll('#usage [data-since]').forEach(o=>o.classList.toggle('primary',o===b));loadUsage();});
  $('#usage-table-toggle').onclick=()=>{const t=$('#usage-table');t.hidden=!t.hidden;$('#usage-table-toggle').textContent=t.hidden?'表格':'圖表';$('#usage-chart').style.display=t.hidden?'':'none';};
  window.addEventListener('resize',()=>{if(fleetOn)renderUsage();});
  function toggleFleet(on){
    fleetOn=on;
    if(on)admOn=false;
    showView(on?'fleet':'chat');
    setCurrent(conv,convPreview);
    if(fleetTimer){clearInterval(fleetTimer);fleetTimer=null;}
    if(on){renderFleet();loadUsage();fleetTimer=setInterval(()=>{if(!document.hidden)renderFleet();},2000);}}
  $('#btn-fleet').onclick=()=>toggleFleet(!fleetOn);

  // --- identity ---------------------------------------------------
  function saveToken(){try{token?localStorage.setItem('xh-token',token):localStorage.removeItem('xh-token')}catch(e){}}
  function loadToken(){try{token=localStorage.getItem('xh-token')||null}catch(e){token=null}}
  function showGate(on){gateOn=on!==false;$('#gate').classList.toggle('on',gateOn);
    if(gateOn)setTimeout(()=>$('#gate-user').focus(),50);}
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
  $('#gate-pass').addEventListener('compositionstart',()=>{gateComposing=true;});
  $('#gate-user').addEventListener('compositionstart',()=>{gateComposing=true;});
  ['#gate-user','#gate-pass'].forEach(sel=>{
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
      await signIn($('#gate-user').value.trim(),$('#gate-pass').value);
      $('#gate-pass').value='';showGate(false);location.reload();
    }catch(err){$('#gate-err').textContent=err.message;}
    finally{btn.disabled=false;}
  });
  $('#btn-logout').onclick=async()=>{
    try{await api('/api/logout',{method:'POST',body:{}})}catch(e){}
    token=null;saveToken();location.reload();
  };

  function applyMeta(m){
    $('#menu-model').textContent=m.model;
    $('#menu-sandbox').textContent=(m.sandbox||'無')+' · 審批 '+m.approval;
    me=m.user;admin=!!m.admin;
    if(m.identity&&me){
      $('#who-name').textContent=me.display;
      $('#who-initial').textContent=(me.display||me.name).trim().charAt(0);
      $('#menu-name').textContent=me.display;
      $('#menu-role').textContent=me.role==='admin'?'管理者':'一般使用者';
      $('#btn-logout').hidden=false;
    }
    $('#btn-admin').hidden=!admin;
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
  function admShow(which){
    ['sec','rep','usr'].forEach(k=>{$('#adm-'+k).hidden=(k!==which);});
    document.querySelectorAll('[data-adm]').forEach(b=>b.classList.toggle('primary',b.dataset.adm===which));
    if(which==='sec')loadSecurity();
    if(which==='rep')loadReport();
    if(which==='usr')loadUsers();
  }
  function toggleAdmin(on){
    admOn=on;
    if(on)fleetOn=false;
    if(fleetTimer){clearInterval(fleetTimer);fleetTimer=null;}
    showView(on?'admin':'chat');
    if(on)admShow('sec');
  }
  $('#btn-admin').onclick=()=>toggleAdmin(!admOn);
  document.querySelectorAll('[data-adm]').forEach(b=>{b.onclick=()=>admShow(b.dataset.adm);});

  const VERDICT={pass:'通過',warn:'有建議事項',fail:'有必須修正的項目'};
  async function loadSecurity(){
    const box=$('#sec-list');box.textContent='';
    try{
      const d=await api('/api/security-check');
      $('#sec-verdict').textContent='檢查結果：'+(VERDICT[d.verdict]||d.verdict)+
        '（通過 '+d.summary.pass+'、建議 '+d.summary.warn+'、必修 '+d.summary.fail+'）';
      d.items.forEach(it=>{
        const row=document.createElement('div');row.className='chk';
        const st=document.createElement('span');st.className='st '+it.state;
        st.textContent={pass:'通過',warn:'建議',fail:'必修'}[it.state]||it.state;
        const bd=document.createElement('div');bd.className='bd';
        const ti=document.createElement('div');ti.className='ti';ti.textContent=it.title;
        const de=document.createElement('div');de.className='de';de.textContent=it.detail;
        bd.appendChild(ti);bd.appendChild(de);
        if(it.fix){const fx=document.createElement('div');fx.className='fx';fx.textContent='修法：'+it.fix;bd.appendChild(fx);}
        row.appendChild(st);row.appendChild(bd);box.appendChild(row);
      });
    }catch(e){$('#sec-verdict').textContent='檢查失敗：'+e.message;}
  }
  $('#sec-rerun').onclick=loadSecurity;

  function card(title,value,meta){
    const b=document.createElement('div');b.className='b';
    const h=document.createElement('h3');h.textContent=title;
    const n=document.createElement('div');n.className='n';n.textContent=value;
    const m=document.createElement('div');m.className='m';m.textContent=meta||'';
    b.appendChild(h);b.appendChild(n);b.appendChild(m);return b;
  }
  async function loadReport(){
    const cards=$('#rep-cards'),detail=$('#rep-detail');cards.textContent='';detail.textContent='';
    try{
      const d=await api('/api/usage-report?since='+encodeURIComponent(repSince));
      $('#rep-range').textContent='近 '+d.since+'，產生於 '+d.generated.slice(0,19).replace('T',' ')+' UTC';
      cards.appendChild(card('多少人在用',String(d.people.active),
        d.people.accounts+' 個帳號・身分驗證 '+(d.people.identity==='on'?'已啟用':'未啟用')));
      cards.appendChild(card('花多少',d.spend.tokens.toLocaleString()+' tokens',
        d.spend.cost.estimate!=null?('估算 '+d.spend.cost.estimate.toLocaleString()):d.spend.cost.assumption));
      cards.appendChild(card('自建 vs 外購',d.hosting.self_hosted.tokens.toLocaleString()+' / '+d.hosting.external.tokens.toLocaleString(),
        '地端自建 / 外購 API（tokens）'));
      cards.appendChild(card('設備',String(d.machines.configured_nodes)+' 個節點',
        d.machines.unreachable.length?('連不上 '+d.machines.unreachable.join('、')):'全部可連線'));
      cards.appendChild(card('對外串接',d.outbound.unsafe.length?'有缺口':'無缺口',
        d.outbound.unsafe.length?d.outbound.unsafe.join('、'):'沙箱、身分、對外連線皆符合建議'));
      cards.appendChild(card('健康與治理',String(d.health.issues.length)+' 項待處理',
        d.health.errors+' 次失敗・'+d.health.failed_signins+' 次登入失敗'));

      const table=document.createElement('table');table.className='tbl';
      table.innerHTML='<tr><th>使用者</th><th>任務</th><th>tokens</th><th>工具呼叫</th></tr>';
      d.people.top.forEach(r=>{
        const tr=document.createElement('tr');
        [r.user,r.tasks,r.tokens.toLocaleString(),r.tool_calls].forEach(v=>{
          const td=document.createElement('td');td.textContent=v;tr.appendChild(td);});
        table.appendChild(tr);
      });
      const h=document.createElement('div');h.className='nodehead';h.innerHTML='<h2>用量前幾名</h2>';
      detail.appendChild(h);detail.appendChild(table);
      if(d.health.issues.length){
        const h2=document.createElement('div');h2.className='nodehead';
        h2.innerHTML='<h2>待處理事項</h2><span class="m">報表不是只報喜：這些是系統自己的問題</span>';
        detail.appendChild(h2);
        d.health.issues.forEach(it=>{
          const row=document.createElement('div');row.className='chk';
          const st=document.createElement('span');st.className='st warn';st.textContent='待處理';
          const bd=document.createElement('div');bd.className='bd';
          const ti=document.createElement('div');ti.className='ti';ti.textContent=it.item;
          const de=document.createElement('div');de.className='de';de.textContent=it.detail;
          const fx=document.createElement('div');fx.className='fx';fx.textContent='建議：'+it.action;
          bd.appendChild(ti);bd.appendChild(de);bd.appendChild(fx);
          row.appendChild(st);row.appendChild(bd);detail.appendChild(row);
        });
      }
    }catch(e){cards.textContent='';detail.textContent='報告產生失敗：'+e.message;}
  }
  document.querySelectorAll('#adm-rep [data-since]').forEach(b=>{
    b.onclick=()=>{repSince=b.dataset.since;
      document.querySelectorAll('#adm-rep [data-since]').forEach(x=>x.classList.toggle('primary',x===b));
      loadReport();};
  });
  $('#rep-json').onclick=async()=>{
    try{
      const d=await api('/api/usage-report?since='+encodeURIComponent(repSince));
      const url=URL.createObjectURL(new Blob([JSON.stringify(d,null,2)],{type:'application/json'}));
      const a=document.createElement('a');a.href=url;a.download='xharness-usage-report.json';a.click();
      URL.revokeObjectURL(url);
    }catch(e){alert('下載失敗：'+e.message);}
  };

  async function loadUsers(){
    const box=$('#usr-list');box.textContent='';
    try{
      const d=await api('/api/users');
      const table=document.createElement('table');table.className='tbl';
      table.innerHTML='<tr><th>帳號</th><th>顯示名稱</th><th>角色</th><th>每日上限</th><th>狀態</th><th></th></tr>';
      d.users.forEach(u=>{
        const tr=document.createElement('tr');
        const cells=[u.name,u.display,u.role==='admin'?'管理者':'一般',
          u.quota_tokens_per_day?u.quota_tokens_per_day.toLocaleString():'無限制',
          u.disabled?'已停用':'使用中'];
        cells.forEach(v=>{const td=document.createElement('td');td.textContent=v;tr.appendChild(td);});
        const td=document.createElement('td');
        if(!u.disabled){
          const b=document.createElement('button');b.className='btn sm';b.textContent='停用';
          b.onclick=async()=>{
            if(!confirm('停用 '+u.name+'？歷史用量會保留，帳號不會被刪除。'))return;
            try{await api('/api/users',{method:'POST',body:{action:'disable',user:u.name}});loadUsers();}
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
      $('#usr-name').value='';$('#usr-pass').value='';$('#usr-display').value='';
      loadUsers();
    }catch(err){$('#usr-err').textContent=err.message;}
  });

  // --- uploads ----------------------------------------------------
  const KB=1024,MB=KB*1024;
  function humanSize(n){return n>=MB?(n/MB).toFixed(1)+' MB':n>=KB?Math.round(n/KB)+' KB':n+' B';}
  let uploadsOn=false;
  async function uploadFiles(fileList){
    const list=[...fileList];
    if(!list.length)return;
    if(!uploadsOn){hint.textContent='這台伺服器沒有啟用帳號，無法上傳檔案。';return;}
    const form=new FormData();
    list.forEach(f=>form.append('file',f,f.name));
    hint.textContent='上傳中：'+list.map(f=>f.name).join('、');
    try{
      const headers={'X-XHarness-Client':'1'};      // no Content-Type: the browser adds the boundary
      if(token)headers['Authorization']='Bearer '+token;
      const r=await fetch('/api/files',{method:'POST',headers,body:form});
      const d=await r.json().catch(()=>({}));
      (d.refused||[]).forEach(item=>el('error','上傳未完成',item.name+'：'+item.reason));
      const stored=d.stored||[];
      if(stored.length){
        stored.forEach(f=>queued.push(f));
        renderQueued();
        loadFiles();
        hint.textContent='已上傳 '+stored.length+' 個檔案，送出訊息時會一併告訴 agent。';
      }else if(!d.refused||!d.refused.length){
        hint.textContent='上傳失敗：'+(d.error||('HTTP '+r.status));
      }
    }catch(err){hint.textContent='上傳失敗：'+err.message;}
  }
  const queued=[];
  function renderQueued(){
    const box=$('#queued');box.textContent='';
    queued.forEach((f,index)=>{
      const chip=document.createElement('span');chip.className='qfile';
      const label=document.createElement('span');label.textContent=f.name+'（'+humanSize(f.bytes)+'）';
      const drop=document.createElement('button');drop.textContent='×';drop.title='不要附這個檔案';
      drop.onclick=()=>{queued.splice(index,1);renderQueued();};
      chip.appendChild(label);chip.appendChild(drop);box.appendChild(chip);
    });
  }
  function takeQueued(){
    if(!queued.length)return '';
    const lines=queued.map(f=>'- '+f.path).join('\n');
    queued.length=0;renderQueued();
    return '\n\n我已上傳以下檔案，請用 read 工具讀取：\n'+lines;
  }
  $('#btn-attach').onclick=()=>$('#file-input').click();
  $('#file-input').onchange=e=>{uploadFiles(e.target.files);e.target.value='';};
  const composer=$('#composer');
  ['dragenter','dragover'].forEach(type=>composer.addEventListener(type,e=>{
    e.preventDefault();composer.classList.add('over');}));
  ['dragleave','drop'].forEach(type=>composer.addEventListener(type,e=>{
    e.preventDefault();if(type==='drop'||!composer.contains(e.relatedTarget))composer.classList.remove('over');}));
  composer.addEventListener('drop',e=>{if(e.dataTransfer&&e.dataTransfer.files)uploadFiles(e.dataTransfer.files);});

  async function loadFiles(){
    const box=$('#files');box.textContent='';
    try{
      const d=await api('/api/files');
      uploadsOn=!!d.workspace;
      $('#btn-attach').style.display=uploadsOn?'':'none';
      if(!uploadsOn){box.textContent='未啟用帳號，agent 直接使用伺服器的工作目錄。';return;}
      if(!d.files.length){
        box.textContent='還沒有上傳任何檔案。上限：單檔 '+humanSize(d.max_file_bytes)+'，總共 '+humanSize(d.max_total_bytes)+'。';
        return;
      }
      const head=document.createElement('div');head.className='frow';
      head.innerHTML='<span class="fn" style="color:var(--ink-3);font-size:13px">已用 '+humanSize(d.used_bytes)+' / '+humanSize(d.max_total_bytes)+'</span>';
      box.appendChild(head);
      d.files.forEach(f=>{
        const row=document.createElement('div');row.className='frow';
        const name=document.createElement('span');name.className='fn';name.textContent=f.path;name.title=f.path;
        const size=document.createElement('span');size.className='fz';size.textContent=humanSize(f.bytes);
        const attach=document.createElement('button');attach.textContent='附加';
        attach.onclick=()=>{queued.push(f);renderQueued();closePanel();};
        const drop=document.createElement('button');drop.textContent='刪除';
        drop.onclick=async()=>{
          if(!confirm('刪除 '+f.name+'？'))return;
          try{await api('/api/files',{method:'POST',body:{action:'delete',name:f.name}});loadFiles();}
          catch(e){box.textContent='刪除失敗：'+e.message;}
        };
        row.appendChild(name);row.appendChild(size);row.appendChild(attach);row.appendChild(drop);
        box.appendChild(row);
      });
    }catch(e){box.textContent='讀取失敗：'+e.message;}
  }

  const whoMenu=$('#who-menu');
  $('#btn-who').onclick=e=>{
    e.stopPropagation();
    const open=whoMenu.classList.toggle('open');
    $('#btn-who').setAttribute('aria-expanded',String(open));
    if(open)refreshQuota();
  };
  document.addEventListener('click',e=>{
    if(!whoMenu.contains(e.target))whoMenu.classList.remove('open');
  });
  document.addEventListener('keydown',e=>{if(e.key==='Escape')whoMenu.classList.remove('open');});

  (async function init(){
    showView('chat');
    loadFiles();   // also decides whether the attach button is shown
    document.body.classList.add('landing');   // nothing has happened yet ('empty' is taken by the empty-list style)
    loadToken();
    try{applyMeta(await api('/api/meta'));}
    catch(e){if(!gateOn)el('error','錯誤',e.message);}
  })();
})();
</script>
</body>
</html>
"""
