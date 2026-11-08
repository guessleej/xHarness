"""`xharness a2a-serve`: take tasks from other agent platforms over A2A (Agent2Agent,
protocol 0.3, JSON-RPC over HTTP).

  GET  /.well-known/agent-card.json   who this agent is and what it can do (public)
  POST /                              message/send, tasks/get, tasks/cancel (Bearer token)

One A2A context is one xHarness conversation: tasks sent with the same
contextId continue it. A task runs in its own thread; message/send waits for it
(configuration.blocking, default true) until it finishes or needs an answer,
otherwise it returns at once and the caller polls tasks/get.

Approvals become the A2A "input-required" state: the task stops with the
question as its status message, and the caller answers with message/send on
the same taskId -- 允許 / allow / yes lets it run, anything else refuses. No
answer within ten minutes is a refusal. The caller may itself be an agent, so
what xHarness keeps for a person -- the desktop tools, a click on "送出", Enter
in a form -- is never offered here: tasks run as a child agent would.

The token lives in $XHARNESS_HOME/a2a-token (0600; created on first start) or
`[a2a] token_file`. The server binds loopback only: to reach it from another
machine, put it behind a TLS front end (xConsole, nginx) rather than serve
plain HTTP on the network.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .config import ResolvedConfig

PROTOCOL_VERSION = "0.3.0"
DEFAULT_PORT = 3090
MAX_BODY = 1_000_000
MAX_TASKS = 200
MAX_RUNNING = 4
ANSWER_SECONDS = 600
YES = {"允許", "同意", "可以", "allow", "yes", "y", "ok", "approve"}
TERMINAL = {"completed", "failed", "canceled", "rejected"}
TASK_NOT_FOUND, NOT_CANCELABLE, UNSUPPORTED = -32001, -32002, -32004


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text_message(role: str, text: str, task_id: str = "", context_id: str = "") -> dict[str, Any]:
    message = {"kind": "message", "role": role, "parts": [{"kind": "text", "text": text}], "messageId": uuid.uuid4().hex}
    if task_id:
        message.update(taskId=task_id, contextId=context_id)
    return message


def message_text(message: dict[str, Any]) -> str:
    return "\n".join(str(p.get("text") or "") for p in message.get("parts") or []
                     if isinstance(p, dict) and p.get("kind", "text") == "text").strip()


class Task:
    def __init__(self, context_id: str, text: str) -> None:
        self.id = uuid.uuid4().hex
        self.context_id = context_id
        self.state = "submitted"
        self.status_text = ""
        self.answer = ""
        self.history = [_text_message("user", text, self.id, context_id)]
        self.changed = threading.Condition()
        self.reply: str | None = None
        self.agent: Any = None
        self.finished = 0.0

    def set(self, state: str, text: str = "") -> None:
        with self.changed:
            self.state, self.status_text = state, text
            if state in TERMINAL:
                self.finished = time.time()
            self.changed.notify_all()

    def wait_settled(self, timeout: float) -> None:
        with self.changed:
            self.changed.wait_for(lambda: self.state in TERMINAL or self.state == "input-required", timeout)

    def public(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.state, "timestamp": _now()}
        if self.status_text:
            status["message"] = _text_message("agent", self.status_text, self.id, self.context_id)
        out = {"kind": "task", "id": self.id, "contextId": self.context_id, "status": status, "history": self.history}
        if self.answer:
            out["artifacts"] = [{"artifactId": f"{self.id}-answer", "name": "answer",
                                 "parts": [{"kind": "text", "text": self.answer}]}]
        return out


class A2AService:
    """Tasks and contexts, independent of HTTP (the tests drive it directly too)."""

    def __init__(self, agent_factory: Callable[[Callable[[str], bool]], Any], wait_seconds: float = 120) -> None:
        self.agent_factory = agent_factory
        self.wait_seconds = wait_seconds
        self.tasks: dict[str, Task] = {}
        self.contexts: dict[str, dict[str, Any]] = {}   # contextId -> {"agent", "task": Task | None}
        self.lock = threading.Lock()

    def _prompt_for(self, holder: dict[str, Any]) -> Callable[[str], bool]:
        def prompt(summary: str) -> bool:
            task: Task = holder["task"]
            with task.changed:
                task.reply = None
                task.state, task.status_text = "input-required", f"需要核准：{summary}\n回覆「允許」繼續，其他回覆一律拒絕。"
                task.changed.notify_all()
                task.changed.wait_for(lambda: task.reply is not None or task.state == "canceled", ANSWER_SECONDS)
                allowed = task.state != "canceled" and (task.reply or "").strip().lower() in YES
                if task.state != "canceled":
                    task.state, task.status_text = "working", ""
                    task.changed.notify_all()
                return allowed
        return prompt

    def _evict(self) -> None:
        if len(self.tasks) <= MAX_TASKS:
            return
        done = sorted((t for t in self.tasks.values() if t.state in TERMINAL), key=lambda t: t.finished)
        for task in done[: len(self.tasks) - MAX_TASKS]:
            self.tasks.pop(task.id, None)

    def _run(self, task: Task, holder: dict[str, Any], text: str) -> None:
        task.set("working")
        try:
            answer = holder["agent"].run(text)
            task.answer = str(answer or "")
            task.history.append(_text_message("agent", task.answer, task.id, task.context_id))
            task.set("canceled" if task.state == "canceled" else "completed")
        except Exception as error:  # noqa: BLE001 - reported to the caller as a failed task
            task.set("failed", f"執行失敗：{str(error)[:300]}")
        finally:
            with self.lock:
                holder["task"] = None

    def send(self, params: dict[str, Any]) -> dict[str, Any]:
        message = params.get("message") or {}
        text = message_text(message)
        if not text:
            raise ValueError("message has no text part")
        task_id = str(message.get("taskId") or "")
        if task_id:                                       # an answer to input-required
            task = self.tasks.get(task_id)
            if task is None:
                raise LookupError(task_id)
            if task.state != "input-required":
                raise ValueError(f"task {task_id} is {task.state}, not waiting for an answer")
            task.history.append(_text_message("user", text, task.id, task.context_id))
            with task.changed:
                task.reply = text
                task.changed.notify_all()
                # Let the waiting tool call take the answer before we wait for the next stop.
                task.changed.wait_for(lambda: task.state != "input-required", 5)
        else:
            context_id = str(message.get("contextId") or uuid.uuid4().hex)
            with self.lock:
                running = sum(1 for c in self.contexts.values() if c["task"] is not None)
                holder = self.contexts.get(context_id)
                if holder and holder["task"] is not None:
                    raise ValueError(f"context {context_id} is still working on task {holder['task'].id}")
                if running >= MAX_RUNNING:
                    raise ValueError(f"{MAX_RUNNING} tasks are running; try again later")
                if holder is None:
                    holder = {"agent": None, "task": None}
                    holder["agent"] = self.agent_factory(self._prompt_for(holder))
                    self.contexts[context_id] = holder
                task = Task(context_id, text)
                task.agent = holder["agent"]
                holder["task"] = task
                self.tasks[task.id] = task
                self._evict()
            threading.Thread(target=self._run, args=(task, holder, text), daemon=True).start()
        if (params.get("configuration") or {}).get("blocking", True):
            task.wait_settled(self.wait_seconds)
        return task.public()

    def get(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self.tasks.get(str(params.get("id") or ""))
        if task is None:
            raise LookupError(params.get("id"))
        out = task.public()
        length = params.get("historyLength")
        if isinstance(length, int) and length >= 0:
            out["history"] = out["history"][-length:] if length else []
        return out

    def cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self.tasks.get(str(params.get("id") or ""))
        if task is None:
            raise LookupError(params.get("id"))
        if task.state in TERMINAL:
            raise PermissionError(f"task is already {task.state}")
        task.agent.stop()
        task.set("canceled", "已由呼叫端取消")
        return task.public()


def agent_card(url: str, version: str) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION, "name": "xHarness", "version": version,
        "description": "云碩 xHarness：在這台電腦上查網頁、算數字、畫圖表、讀寫檔案的代理。需要核准時任務會停在 input-required。",
        "url": url, "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}, "security": [{"bearer": []}],
        "skills": [
            {"id": "research", "name": "查網頁", "description": "用真瀏覽器搜尋與閱讀網頁，附來源網址", "tags": ["web", "search"]},
            {"id": "analysis", "name": "資料分析", "description": "讀表格、用程式計算、畫圖表並說明", "tags": ["data", "chart"]},
            {"id": "files", "name": "檔案與指令", "description": "在工作目錄讀寫檔案、執行經核准的指令", "tags": ["files", "shell"]},
        ],
    }


def load_token(path: str) -> str:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
        if token:
            return token
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def make_handler(service: A2AService, token: str, card: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    methods = {"message/send": service.send, "tasks/get": service.get, "tasks/cancel": service.cancel}

    class Handler(BaseHTTPRequestHandler):
        server_version = "xharness-a2a"

        def log_message(self, *_args: Any) -> None:
            pass

        def _json(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
                self._json(200, card)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._json(413, {"error": "request too large"})
                return
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(200, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                return
            rid = request.get("id") if isinstance(request, dict) else None
            method = request.get("method") if isinstance(request, dict) else None
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
            if method == "message/stream" or str(method).startswith("tasks/pushNotificationConfig"):
                reply["error"] = {"code": UNSUPPORTED, "message": f"{method} is not supported (streaming and push are off)"}
            elif method not in methods:
                reply["error"] = {"code": -32601, "message": f"method not found: {method}"}
            else:
                try:
                    reply["result"] = methods[method](request.get("params") or {})
                except LookupError as error:
                    reply["error"] = {"code": TASK_NOT_FOUND, "message": f"task not found: {error}"}
                except PermissionError as error:
                    reply["error"] = {"code": NOT_CANCELABLE, "message": str(error)}
                except ValueError as error:
                    reply["error"] = {"code": -32602, "message": str(error)}
            self._json(200, reply)

    return Handler


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def run_a2a_serve(config: ResolvedConfig, host: str = "127.0.0.1", port: int | None = None) -> int:
    import sys

    from . import __version__
    from .agent import Agent, AgentOptions
    from .presets import build_harness
    from .session import harness_home

    if not _loopback(host):
        print("xharness: a2a-serve binds loopback only; put it behind a TLS front end (xConsole, nginx) "
              "to reach it from another machine", file=sys.stderr)
        return 2
    settings = getattr(config, "a2a", None) or {}
    token_path = str(settings.get("token_file") or os.path.join(harness_home(), "a2a-token"))
    token = load_token(token_path)
    port = port or int(settings.get("port") or DEFAULT_PORT)
    approval = str(settings.get("approval") or config.approval or "prompt")

    def factory(prompt: Callable[[str], bool]) -> Any:
        harness = build_harness(config)
        # "a2a" is not "main": the caller may be an agent, so what needs a person is withheld.
        return Agent(harness.ctx, AgentOptions(
            name="a2a", system_prompt=config.system_prompt, approval_mode=approval, prompt=prompt,
            max_turns=config.max_turns or AgentOptions.max_turns, project_instructions=config.project_instructions,
            compaction=config.compaction or {"auto": True, "threshold": 0.8}))

    url = f"http://{host}:{port}/"
    service = A2AService(factory)
    server = ThreadingHTTPServer((host, port), make_handler(service, token, agent_card(url, __version__)))
    print(f"xharness a2a: {url}  card: {url}.well-known/agent-card.json  token: {token_path}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
