"""Agent Client Protocol: run xHarness inside an editor (Zed, and any other
ACP client) with `xharness acp`.

The editor starts the process and talks JSON-RPC over stdin/stdout, one JSON
message per line. The agent streams its reply as it is written, reports each
tool call (with a diff for writes and edits, so the editor can show the
change), and asks the editor for permission before anything with side effects
-- the same approval policy as everywhere else, with the editor as the person.

Supported: initialize, authenticate (no methods), session/new, session/prompt
(text, resource links, embedded text resources, images), session/cancel,
session/update (agent_message_chunk, tool_call, tool_call_update) and
session/request_permission. Sessions are fresh (no session/load); everything
is still written to the usual session log.

Stdout carries protocol messages only; diagnostics go to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import IO, Any

from . import __version__
from .agent import Agent, AgentOptions
from .config import ResolvedConfig
from .presets import build_harness
from .vision import IMAGE_TYPES, describer_from_config, save_images

PROTOCOL_VERSION = 1
MAX_TOOL_TEXT = 4000
MAX_DIFF_SOURCE = 200_000
KINDS = {"read": "read", "write": "edit", "edit": "edit", "bash": "execute", "terminal": "execute",
         "job_start": "execute", "glob": "search", "grep": "search", "lsp": "search", "webfetch": "fetch",
         "web_search": "fetch", "browser_open": "fetch", "desktop_look": "read", "desktop_act": "execute", "todo_write": "think", "skill": "think",
         "goal_done": "think"}


def tool_title(name: str, args: dict[str, Any]) -> str:
    target = args.get("path") or args.get("command") or args.get("pattern") or args.get("url") or args.get("query") or ""
    return f"{name} {str(target)[:120]}".strip()


def edit_texts(name: str, args: dict[str, Any], cwd: str) -> tuple[str, str | None, str] | None:
    """(path, old text or None for a new file, new text) for write/edit."""
    if name not in ("write", "edit") or not args.get("path"):
        return None
    path = str(args["path"])
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    try:
        if os.path.getsize(full) > MAX_DIFF_SOURCE:
            return None
        with open(full, encoding="utf-8", errors="replace") as handle:
            before: str | None = handle.read()
    except FileNotFoundError:
        before = None
    except OSError:
        return None
    if name == "write":
        return full, before, str(args.get("content") or "")
    old, new = str(args.get("old_string") or ""), str(args.get("new_string") or "")
    if before is None or not old or old not in before:
        return None
    after = before.replace(old, new) if args.get("replace_all") else before.replace(old, new, 1)
    return full, before, after


@dataclass
class AcpSession:
    id: str
    agent: Agent
    harness: Any
    cwd: str
    running: bool = False
    always_allow: set[str] = field(default_factory=set)
    current_call: str = ""


class AcpServer:
    def __init__(self, config: ResolvedConfig, stdin: IO[str] | None = None, stdout: IO[str] | None = None,
                 harness_factory: Any = None) -> None:
        self.config = config
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.harness_factory = harness_factory or (lambda: build_harness(config))
        self.sessions: dict[str, AcpSession] = {}
        self._write = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._next = 0
        self._counter = 0
        describer = describer_from_config(config)
        self.describer = describer
        self.images = bool(describer) or bool((config.provider or {}).get("vision"))

    # --- wire -------------------------------------------------------------------
    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False)
        with self._write:
            self.stdout.write(line + "\n")
            self.stdout.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any] | None:
        with self._write:
            self._next += 1
            request_id = self._next
        done = threading.Event()
        box: dict[str, Any] = {}
        self._pending[request_id] = (done, box)
        self.send({"id": request_id, "method": method, "params": params})
        done.wait(timeout)
        self._pending.pop(request_id, None)
        return box.get("result")

    def update(self, session: AcpSession, update: dict[str, Any]) -> None:
        self.notify("session/update", {"sessionId": session.id, "update": update})

    def serve(self) -> int:
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.send({"id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if "method" not in message and "id" in message:      # a reply to our request
                pending = self._pending.get(message["id"])
                if pending:
                    pending[1]["result"] = message.get("result")
                    pending[0].set()
                continue
            # Prompts run on their own thread so this loop keeps reading the
            # client's permission answers and cancellations.
            if message.get("method") == "session/prompt":
                threading.Thread(target=self.handle, args=(message,), daemon=True).start()
            else:
                self.handle(message)
        for session in self.sessions.values():
            session.agent.stop()
            session.harness.dispose()
        return 0

    def handle(self, message: dict[str, Any]) -> None:
        method, params, request_id = message.get("method"), message.get("params") or {}, message.get("id")
        try:
            result = self.dispatch(str(method), params)
        except AcpError as error:
            if request_id is not None:
                self.send({"id": request_id, "error": {"code": error.code, "message": str(error)}})
            return
        except Exception as error:  # noqa: BLE001 - reported to the client, never fatal to the server
            if request_id is not None:
                self.send({"id": request_id, "error": {"code": -32603, "message": f"{type(error).__name__}: {error}"}})
            return
        if request_id is not None:
            self.send({"id": request_id, "result": result})

    # --- methods ----------------------------------------------------------------
    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {"loadSession": False,
                                      "promptCapabilities": {"image": self.images, "audio": False, "embeddedContext": True}},
                "agentInfo": {"name": "xharness", "title": "xHarness", "version": __version__},
                "authMethods": [],
            }
        if method == "authenticate":
            return {}
        if method == "session/new":
            return {"sessionId": self.new_session(params)}
        if method == "session/prompt":
            return self.prompt(params)
        if method == "session/cancel":
            session = self.sessions.get(str(params.get("sessionId")))
            if session:
                session.agent.stop()
                for done, box in list(self._pending.values()):
                    box["result"] = {"outcome": {"outcome": "cancelled"}}
                    done.set()
            return None
        raise AcpError(-32601, f"method not found: {method}")

    def new_session(self, params: dict[str, Any]) -> str:
        cwd = str(params.get("cwd") or "")
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise AcpError(-32602, "cwd must be an absolute path to an existing directory")
        if params.get("mcpServers"):
            raise AcpError(-32602, "client-provided MCP servers are not supported; configure them in xharness.toml")
        self._counter += 1
        session_id = f"acp-{os.getpid()}-{self._counter}"
        harness = self.harness_factory()
        holder: dict[str, AcpSession] = {}

        def approve(summary: str) -> bool:
            session = holder["s"]
            call_id = session.current_call
            tool = summary.split(":", 1)[0].strip()
            if tool in session.always_allow:
                return True
            reply = self.request("session/request_permission", {
                "sessionId": session.id,
                "toolCall": {"toolCallId": call_id, "title": summary[:200], "status": "pending"},
                "options": [
                    {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "allow-always", "name": f"Always allow {tool}", "kind": "allow_always"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                ],
            })
            outcome = (reply or {}).get("outcome") or {}
            if outcome.get("outcome") != "selected":
                return False
            if outcome.get("optionId") == "allow-always":
                session.always_allow.add(tool)
            return outcome.get("optionId") in ("allow-once", "allow-always")

        def tool_start(name: str, raw: str) -> None:
            session = holder["s"]
            self._counter += 1
            session.current_call = f"call-{self._counter}"
            try:
                args = json.loads(raw or "{}")
            except json.JSONDecodeError:
                args = {}
            update: dict[str, Any] = {"sessionUpdate": "tool_call", "toolCallId": session.current_call,
                                      "title": tool_title(name, args if isinstance(args, dict) else {}),
                                      "kind": KINDS.get(name, "other"), "status": "in_progress",
                                      "rawInput": args}
            if isinstance(args, dict) and args.get("path"):
                full = str(args["path"]) if os.path.isabs(str(args["path"])) else os.path.join(session.cwd, str(args["path"]))
                update["locations"] = [{"path": full}]
            change = edit_texts(name, args if isinstance(args, dict) else {}, session.cwd)
            if change:
                path, before, after = change
                update["content"] = [{"type": "diff", "path": path, "oldText": before, "newText": after}]
            self.update(session, update)

        def tool_end(name: str, output: str, is_error: bool) -> None:
            session = holder["s"]
            self.update(session, {"sessionUpdate": "tool_call_update", "toolCallId": session.current_call,
                                  "status": "failed" if is_error else "completed",
                                  "content": [{"type": "content", "content": {"type": "text", "text": output[:MAX_TOOL_TEXT]}}]})

        agent = Agent(harness.ctx, AgentOptions(
            cwd=cwd,
            system_prompt=self.config.system_prompt,
            max_turns=self.config.max_turns or AgentOptions.max_turns,
            approval_mode="auto" if self.config.approval == "auto" else "prompt",
            prompt=approve,
            ask=None,       # ACP has no question flow; the tool tells the model nobody can answer
            project_instructions=self.config.project_instructions,
            compaction=getattr(self.config, "compaction", None) or {"auto": True, "threshold": 0.8},
            describe_image=self.describer[1] if self.describer else None,
            describer_name=self.describer[0] if self.describer else "",
            on_delta=lambda text: self.update(holder["s"], {"sessionUpdate": "agent_message_chunk",
                                                            "content": {"type": "text", "text": text}}),
            on_tool_start=tool_start,
            on_tool_end=tool_end,
        ))
        session = AcpSession(session_id, agent, harness, cwd)
        holder["s"] = session
        self.sessions[session_id] = session
        return session_id

    def prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.sessions.get(str(params.get("sessionId")))
        if session is None:
            raise AcpError(-32602, "unknown sessionId")
        if session.running:
            raise AcpError(-32600, "a prompt is already running in this session")
        texts: list[str] = []
        context: list[str] = []
        images: list[dict[str, str]] = []
        for block in params.get("prompt") or []:
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text") or ""))
            elif kind == "resource_link":
                texts.append(f"[resource_link name={block.get('name', '')} uri={block.get('uri', '')}]")
            elif kind == "resource":
                resource = block.get("resource") or {}
                if "text" in resource:
                    context.append(f"### {resource.get('uri', 'resource')}\n```\n{str(resource['text'])[:20_000]}\n```")
            elif kind == "image":
                if not self.images:
                    raise AcpError(-32602, "images were not advertised for this model")
                mime = str(block.get("mimeType") or "")
                if mime not in IMAGE_TYPES:
                    raise AcpError(-32602, f"unsupported image type {mime}")
                images.append({"type": mime, "data": str(block.get("data") or "")})
            else:
                raise AcpError(-32602, f"unsupported content block: {kind}")
        text = "".join(texts).strip()
        if not text and not images:
            raise AcpError(-32602, "empty prompt")
        try:
            paths = save_images(images, session.cwd) if images else []
        except ValueError as error:
            raise AcpError(-32602, str(error)) from error
        session.running = True
        try:
            answer = session.agent.run(text or "請看這張圖片。",
                                       extra_context=("Content the editor attached:\n\n" + "\n\n".join(context)) if context else None,
                                       images=paths)
        finally:
            session.running = False
        if session.agent.stop_requested or answer == "[stopped by operator]":
            return {"stopReason": "cancelled"}
        if answer == "[stopped: max turns reached]":
            return {"stopReason": "max_turn_requests"}
        return {"stopReason": "end_turn"}


class AcpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def run_acp(config: ResolvedConfig) -> int:
    return AcpServer(config).serve()
