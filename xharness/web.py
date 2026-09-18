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
from .memory import MemoryStore, default_memory_dir
from .presets import build_harness
from .sandbox import resolve_sandbox
from .session import SessionLog, messages_from_events

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
APPROVAL_TIMEOUT_SECONDS = 600
TICKET_TTL_SECONDS = 60
MAX_BODY_BYTES = 1_000_000
CSRF_HEADER = "X-XHarness-Client"


@dataclass
class Approval:
    summary: str
    event: threading.Event = field(default_factory=threading.Event)
    decision: bool | None = None


class Conversation:
    def __init__(self, conv_id: str, harness: Harness, agent: Agent, session_id: str) -> None:
        self.id = conv_id
        self.harness = harness
        self.agent = agent
        self.session_id = session_id
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
        return {
            "id": self.id,
            "session": self.session_id,
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
        harness_factory: Callable[[str | None], Harness] | None = None,
    ) -> None:
        self.config = config
        self.approval_mode = approval_mode
        self.harness_factory = harness_factory or (lambda resume: build_harness(config, resume=resume))
        self.conversations: dict[str, Conversation] = {}
        self.lock = threading.Lock()
        self.tickets: dict[str, float] = {}

    def create(self, resume: str | None = None) -> Conversation:
        harness = self.harness_factory(resume)
        session = harness.ctx.optional("session")
        session_id = session.id if session else "-"
        conv_id = secrets.token_hex(6)
        initial = messages_from_events(SessionLog.load(resume)) if resume else []
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
                system_prompt=self.config.system_prompt,
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
        conv = Conversation(conv_id, harness, agent, session_id)
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

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            convs = sorted(self.conversations.values(), key=lambda c: c.created, reverse=True)
        return [conv.summary() for conv in convs]

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
                conv.push({"type": "error", "message": f"{type(error).__name__}: {error}", "usage": conv.usage()})
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

    def fleet(self) -> dict[str, Any]:
        items = self.list()
        model = str(self.config.provider["model"])
        for item in items:
            item["model"] = model
        usage_total = sum((item["usage"] or {}).get("total_tokens", 0) for item in items)
        return {
            "summary": {
                "conversations": len(items),
                "running": sum(1 for item in items if item["state"] == "running"),
                "waiting": sum(1 for item in items if item["state"] == "waiting"),
                "children": sum(len(item["children"]) for item in items),
                "total_tokens": usage_total,
            },
            "items": items,
        }

    def memory_index(self) -> list[dict[str, Any]]:
        """Read-only view of what the agent remembers, straight from disk."""
        directory = str(self.config.memory.get("dir") or default_memory_dir())
        if not os.path.isdir(directory):
            return []
        store = MemoryStore(directory)
        return [
            {"name": memory.name, "kind": memory.kind, "description": memory.description, "updated": memory.updated}
            for memory in store.list()
        ]

    def issue_ticket(self) -> str:
        ticket = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.tickets = {key: exp for key, exp in self.tickets.items() if exp > now}
            self.tickets[ticket] = now + TICKET_TTL_SECONDS
        return ticket

    def redeem_ticket(self, ticket: str) -> bool:
        with self.lock:
            expiry = self.tickets.pop(ticket, None)
        return expiry is not None and expiry > time.time()

    def dispose(self) -> None:
        with self.lock:
            convs = list(self.conversations.values())
        for conv in convs:
            conv.harness.dispose()


def make_handler(app: WebApp, token: str | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"xharness/{__version__}"
        protocol_version = "HTTP/1.1"

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
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
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

        def _guard(self, sse_ticket: str | None = None) -> bool:
            if token:
                if self._bearer_ok():
                    return True
                if sse_ticket and app.redeem_ticket(sse_ticket):
                    return True
                self._json(401, {"error": "unauthorized"})
                return False
            if not self._host_ok():
                self._json(403, {"error": "forbidden host"})
                return False
            return True

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
                if not token and not self._host_ok():
                    self._json(403, {"error": "forbidden host"})
                    return
                self._html(INDEX_HTML)
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
                self._json(200, {
                    "version": __version__,
                    "model": app.config.provider["model"],
                    "base_url": app.config.provider["base_url"],
                    "approval": app.approval_mode,
                    "sandbox": sandbox,
                    "auth": bool(token),
                })
                return
            if parts[1:] == ["conversations"]:
                self._json(200, app.list())
                return
            if parts[1:] == ["sessions"]:
                self._json(200, SessionLog.list()[:50])
                return
            if parts[1:] == ["memory"]:
                self._json(200, app.memory_index())
                return
            if parts[1:] == ["fleet"]:
                self._json(200, app.fleet())
                return
            if len(parts) == 4 and parts[1] == "conversations" and parts[3] == "events":
                conv = app.get(parts[2])
                if conv is None:
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
            if not self._guard():
                return
            if not self.headers.get(CSRF_HEADER):
                self._json(403, {"error": f"missing {CSRF_HEADER} header"})
                return
            body = self._body()
            if body is None:
                return
            if parts[1:] == ["tickets"]:
                self._json(200, {"ticket": app.issue_ticket(), "ttl": TICKET_TTL_SECONDS})
                return
            if parts[1:] == ["conversations"]:
                resume = body.get("resume")
                try:
                    conv = app.create(str(resume) if resume else None)
                except Exception as error:  # noqa: BLE001 - reported to the caller
                    self._json(400, {"error": str(error)})
                    return
                self._json(201, conv.summary())
                return
            if len(parts) == 4 and parts[1] == "conversations":
                conv = app.get(parts[2])
                if conv is None:
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
            self._json(404, {"error": "not found"})

    return Handler


def serve(
    config: ResolvedConfig,
    host: str = "127.0.0.1",
    port: int = 3080,
    token: str | None = None,
    approval_mode: str = "prompt",
    open_browser: bool = True,
    harness_factory: Callable[[str | None], Harness] | None = None,
) -> int:
    bare = host.split("%")[0]
    if bare == "::1":
        bare = "[::1]"
    if bare not in LOOPBACK_HOSTS and not token:
        print(
            f"xharness: refusing to bind {host} without --token; "
            "a non-loopback address exposes the harness to the network",
            file=sys.stderr,
        )
        return 2
    app = WebApp(config, approval_mode=approval_mode, harness_factory=harness_factory)
    server = ThreadingHTTPServer((host, port), make_handler(app, token))
    server.daemon_threads = True
    url = f"http://{host}:{server.server_address[1]}/"
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
.nav .in{max-width:1080px;margin:0 auto;padding:12px 16px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.brand{font-weight:800;font-size:20px;letter-spacing:.2px;color:var(--brand);text-decoration:none}
.chips{display:flex;gap:8px;flex-wrap:wrap;flex:1;min-width:0}
.chip{font-size:13px;padding:4px 10px;border-radius:999px;background:var(--tint);color:var(--ink-2);white-space:nowrap;max-width:280px;overflow:hidden;text-overflow:ellipsis}
.btn{font:inherit;font-size:14px;padding:8px 14px;border-radius:10px;border:0;cursor:pointer;background:var(--card);color:var(--ink);box-shadow:var(--shadow-sm)}
.btn.primary{background:var(--brand);color:#fff}
.btn.primary:hover{background:var(--brand-dark)}
.btn:disabled{opacity:.55;cursor:not-allowed}
main{max-width:1080px;margin:0 auto;padding:22px 16px 160px}
.hero{padding:26px 0 8px}
.hero h1{margin:0 0 4px;font-size:30px;line-height:1.2}
.hero p{margin:0;color:var(--ink-3);font-size:15px}
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
.composer .in{max-width:1080px;margin:0 auto;padding:14px 16px;display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;font:inherit;font-size:15px;line-height:1.5;padding:12px 14px;border:0;border-radius:14px;resize:none;min-height:48px;max-height:220px;
  background:var(--card);color:var(--ink);box-shadow:var(--shadow-sm);outline:none}
textarea:focus{box-shadow:0 0 0 3px rgba(191,24,31,.18),var(--shadow-sm)}
.hint{font-size:12px;color:var(--ink-3);max-width:1080px;margin:0 auto;padding:0 16px 10px}
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
#fleet{display:none}
body.fleet #fleet{display:block}
body.fleet #transcript,body.fleet #hero,body.fleet .composer{display:none}
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
@media (max-width:640px){.hero h1{font-size:24px}.chip{max-width:160px}}
</style>
</head>
<body>
<nav class="nav" id="nav"><div class="in">
  <a class="brand" href="#">xHarness</a>
  <div class="chips">
    <span class="chip" id="chip-model">model: -</span>
    <span class="chip" id="chip-usage">usage: 0 tokens</span>
    <span class="chip" id="chip-status">閒置</span>
  </div>
  <button class="btn" id="btn-new">新對話</button>
  <button class="btn" id="btn-fleet">艦隊</button>
  <button class="btn" id="btn-list">對話與紀錄</button>
  <button class="btn" id="btn-theme" aria-label="切換主題">主題</button>
</div></nav>

<main>
  <section class="hero" id="hero">
    <h1>管得住的 agent。</h1>
    <p>輸入任務，模型會用工具讀寫檔案與執行指令；有副作用的動作會先問你。</p>
  </section>
  <div id="transcript"></div>
  <section id="fleet">
    <div class="hero"><h1>艦隊視圖</h1><p>所有進行中的對話與子代理，一眼看完狀態、用量與等待中的許可；可就地審批或停止。</p></div>
    <div class="kpis" id="kpis"></div>
    <div class="grid" id="fleet-grid"></div>
  </section>
</main>

<div class="composer">
  <div class="in">
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
    <div class="sec">記憶（agent 記得什麼）</div>
    <div id="memory"></div>
  </div>
</aside>

<script>
(function(){
  const $=s=>document.querySelector(s);
  const transcript=$('#transcript'),input=$('#input'),hint=$('#hint');
  let conv=null,es=null,token=null,assistantEl=null,assistantText='',composing=false;

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
    if(r.status===401){token=prompt('這個伺服器需要存取權杖，請貼上啟動時設定的 token：');if(token)return api(path,opts);}
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
    if(ev.type==='user'){$('#hero').style.display='none';el('user','你',ev.text);setStatus('執行中…',true);}
    else if(ev.type==='history'){$('#hero').style.display='none';el(ev.role==='user'?'user':'assistant',ev.role==='user'?'你':'xHarness',ev.text);}
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
    conv=c.id;transcript.innerHTML='';assistantEl=null;assistantText='';$('#hero').style.display=resume?'none':'';
    hint.textContent='對話 '+c.id+' · session '+c.session;setStatus('閒置',false);await connect(conv,0);refreshList();closePanel();return c;
  }

  async function send(){
    const text=input.value.trim();if(!text)return;
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
  $('#btn-new').onclick=()=>newConversation();

  // panel
  const panel=$('#panel'),backdrop=$('#backdrop');
  function openPanel(){panel.classList.add('open');backdrop.classList.add('open');refreshList();}
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
        d.onclick=async()=>{if(c.id===conv){closePanel();return;}conv=c.id;transcript.innerHTML='';assistantEl=null;assistantText='';$('#hero').style.display='none';hint.textContent='對話 '+c.id+' · session '+c.session;await connect(conv,0);closePanel();};
        box.appendChild(d);});
      const sessions=await api('/api/sessions');const sb=$('#sessions');sb.innerHTML='';
      sessions.slice(0,30).forEach(s=>{const d=document.createElement('div');d.className='item';
        d.innerHTML='<div class="t"></div><div class="m"><span></span></div>';d.querySelector('.t').textContent=s.id;
        d.querySelector('.m span').textContent=new Date(s.mtime*1000).toLocaleString('zh-TW');d.onclick=()=>newConversation(s.id);sb.appendChild(d);});
      const mem=await api('/api/memory');const mb=$('#memory');mb.innerHTML='';
      if(!mem.length)mb.innerHTML='<div class="item"><div class="m">還沒有記憶</div></div>';
      mem.forEach(m=>{const d=document.createElement('div');d.className='item';d.style.cursor='default';
        d.innerHTML='<div class="t"></div><div class="m"><span></span></div>';d.querySelector('.t').textContent=m.name+' · '+m.description;
        d.querySelector('.m span').textContent=m.kind+(m.updated?' · '+m.updated.slice(0,10):'');mb.appendChild(d);});
    }catch(e){console.error(e)}
  }

  // fleet view
  let fleetTimer=null,fleetOn=false;
  function fmtState(s){return s==='running'?'執行中':s==='waiting'?'等待許可':'閒置';}
  async function renderFleet(){
    try{
      const f=await api('/api/fleet');const k=f.summary;
      $('#kpis').innerHTML=[['對話',k.conversations],['執行中',k.running],['等待許可',k.waiting],['子代理',k.children],['總 tokens',k.total_tokens]]
        .map(([l,n])=>'<div class="kpi"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>').join('');
      const g=$('#fleet-grid');g.innerHTML='';
      if(!f.items.length){g.innerHTML='<div class="empty">還沒有對話。按「新對話」開始。</div>';return;}
      f.items.forEach(it=>{const c=document.createElement('div');c.className='fc '+it.state;
        const u=it.usage||{};
        c.innerHTML='<div class="head"><span class="state '+it.state+'">'+fmtState(it.state)+'</span><span class="meta">'+(it.elapsed!=null?it.elapsed+'s':'')+'</span></div>'
          +'<div class="prev"></div>'
          +'<div class="meta"><span>'+(u.total_tokens||0)+' tokens</span><span>'+(u.calls||0)+' calls</span><span>'+(it.tool_calls||0)+' tools</span><span>'+it.model+'</span><span>session '+it.session+'</span></div>'
          +(it.children.length?'<div class="kids">子代理：'+it.children.map(ch=>'<span title="'+ch.task.replace(/"/g,'')+'">'+ch.name+'</span>').join('')+'</div>':'')
          +'<div class="apprs"></div><div class="acts"></div>';
        c.querySelector('.prev').textContent=it.preview||'(尚未送出訊息)';
        const ap=c.querySelector('.apprs');
        it.pending_approvals.forEach(a=>{const d=document.createElement('div');d.className='appr';d.textContent=a.summary;
          const ok=document.createElement('button');ok.className='btn primary sm';ok.textContent='允許';ok.style.marginLeft='8px';
          const no=document.createElement('button');no.className='btn sm';no.textContent='拒絕';no.style.marginLeft='6px';
          ok.onclick=()=>api('/api/conversations/'+it.id+'/approvals',{method:'POST',body:{id:a.id,allow:true}}).then(renderFleet);
          no.onclick=()=>api('/api/conversations/'+it.id+'/approvals',{method:'POST',body:{id:a.id,allow:false}}).then(renderFleet);
          d.append(ok,no);ap.appendChild(d);});
        const acts=c.querySelector('.acts');
        const open=document.createElement('button');open.className='btn sm';open.textContent='開啟';
        open.onclick=async()=>{conv=it.id;transcript.innerHTML='';assistantEl=null;assistantText='';$('#hero').style.display='none';hint.textContent='對話 '+it.id+' · session '+it.session;await connect(conv,0);toggleFleet(false);};
        acts.appendChild(open);
        if(it.running){const st=document.createElement('button');st.className='btn ghost sm';st.textContent='停止';st.onclick=()=>api('/api/conversations/'+it.id+'/stop',{method:'POST',body:{}}).then(renderFleet);acts.appendChild(st);}
        g.appendChild(c);});
    }catch(e){console.error(e)}
  }
  function toggleFleet(on){fleetOn=on;document.body.classList.toggle('fleet',on);$('#btn-fleet').textContent=on?'回到對話':'艦隊';
    if(fleetTimer){clearInterval(fleetTimer);fleetTimer=null;}
    if(on){renderFleet();fleetTimer=setInterval(renderFleet,2000);}}
  $('#btn-fleet').onclick=()=>toggleFleet(!fleetOn);

  (async function init(){
    try{const m=await api('/api/meta');$('#chip-model').textContent='model: '+m.model+(m.sandbox?' · sandbox '+m.sandbox:'')+' · approval '+m.approval;}
    catch(e){el('error','錯誤',e.message);}
  })();
})();
</script>
</body>
</html>
"""
