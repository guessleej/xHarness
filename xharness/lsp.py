"""Language servers: go to definition, find references, hover, diagnostics
and symbol outlines, from the same servers editors use.

grep finds text; a language server knows that `run` on line 40 is the method
defined in agent.py and not the five other `run`s, and it reports type errors
before anything is executed. The `lsp` tool starts the right server for a
file on first use (pyright or pylsp for Python, typescript-language-server,
gopls, rust-analyzer -- whichever is installed, or whatever [lsp.servers.*]
names), keeps it running for the conversation, and stops it at the end.

Models are poor at column numbers, so a position can be given as a line and
the symbol's name on that line; the tool finds the column itself.

The client is a small JSON-RPC-over-stdio implementation (Content-Length
framing), standard library only, like everything else here.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess  # nosec B404 - starting language servers is this module's purpose
import threading
import time
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .context import Context, Plugin
from .tools import Tool, ToolContext, ToolResult, register_tools

REQUEST_TIMEOUT = 30.0
DIAGNOSTICS_WAIT = 4.0
MAX_RESULTS = 50
#: Tried in order when [lsp] auto is on (the default): the first installed one serves the extension.
AUTO_SERVERS = [
    ("pyright", ["pyright-langserver", "--stdio"], [".py"], "python"),
    ("basedpyright", ["basedpyright-langserver", "--stdio"], [".py"], "python"),
    ("pylsp", ["pylsp"], [".py"], "python"),
    ("typescript", ["typescript-language-server", "--stdio"], [".ts", ".tsx", ".js", ".jsx"], "typescript"),
    ("gopls", ["gopls"], [".go"], "go"),
    ("rust-analyzer", ["rust-analyzer"], [".rs"], "rust"),
]
LANGUAGE_IDS = {".py": "python", ".ts": "typescript", ".tsx": "typescriptreact", ".js": "javascript",
                ".jsx": "javascriptreact", ".go": "go", ".rs": "rust", ".java": "java", ".c": "c", ".cpp": "cpp"}
SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def to_uri(path: str) -> str:
    return "file://" + quote(os.path.abspath(path))


def from_uri(uri: str) -> str:
    return unquote(urlsplit(uri).path)


class LspClient:
    def __init__(self, command: list[str], root: str) -> None:
        self.root = root
        self.process = subprocess.Popen(  # nosec B603 - configured or well-known server binary, no shell
            command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._next = 0
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}
        self.diagnostics_seen: dict[str, float] = {}
        self.opened: dict[str, tuple[int, float]] = {}   # uri -> (version, mtime)
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.capabilities = self.request("initialize", {
            "processId": os.getpid(), "rootUri": to_uri(root),
            "workspaceFolders": [{"uri": to_uri(root), "name": os.path.basename(root) or "root"}],
            "capabilities": {"textDocument": {
                "synchronization": {"didSave": False},
                "publishDiagnostics": {}, "definition": {}, "references": {},
                "hover": {"contentFormat": ["plaintext", "markdown"]},
                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}},
                "workspace": {"workspaceFolders": True, "configuration": True}},
        }).get("capabilities", {})
        self.notify("initialized", {})

    # --- transport --------------------------------------------------------------
    def _send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message).encode("utf-8")
        with self._write_lock:
            stdin = self.process.stdin
            if stdin is None:
                raise RuntimeError("language server has no stdin")
            stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
            stdin.flush()

    def _read_loop(self) -> None:
        stream = self.process.stdout
        if stream is None:
            return
        while True:
            length = 0
            while True:
                line = stream.readline()
                if not line:
                    for slot in list(self._pending.values()):
                        slot.put({"error": {"message": "language server exited"}})
                    return
                line = line.strip()
                if not line:
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            if not length:
                continue
            try:
                message = json.loads(stream.read(length).decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            self._dispatch(message)

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message) and "method" not in message:
            slot = self._pending.pop(message["id"], None)
            if slot:
                slot.put(message)
            return
        method = message.get("method")
        if method == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            self.diagnostics[params.get("uri", "")] = params.get("diagnostics") or []
            self.diagnostics_seen[params.get("uri", "")] = time.monotonic()
        elif "id" in message:   # a request from the server: answer so it does not wait on us
            result: Any = None
            if method == "workspace/configuration":
                result = [None for _ in (message.get("params") or {}).get("items", [])]
            elif method == "workspace/workspaceFolders":
                result = [{"uri": to_uri(self.root), "name": os.path.basename(self.root)}]
            self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def request(self, method: str, params: dict[str, Any], timeout: float = REQUEST_TIMEOUT) -> Any:
        with self._lock:
            self._next += 1
            request_id = self._next
            slot: queue.Queue = queue.Queue()
            self._pending[request_id] = slot
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            reply = slot.get(timeout=timeout)
        except queue.Empty as error:
            self._pending.pop(request_id, None)
            raise TimeoutError(f"{method} took longer than {timeout:g}s") from error
        if "error" in reply:
            raise RuntimeError(str((reply["error"] or {}).get("message") or reply["error"]))
        return reply.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # --- documents --------------------------------------------------------------
    def sync(self, path: str) -> tuple[str, bool]:
        """Open the file, or send its new text if it changed since. (uri, sent)."""
        uri = to_uri(path)
        mtime = os.path.getmtime(path)
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        known = self.opened.get(uri)
        if known is None:
            language = LANGUAGE_IDS.get(os.path.splitext(path)[1].lower(), "plaintext")
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language, "version": 1, "text": text}})
            self.opened[uri] = (1, mtime)
            return uri, True
        if known[1] != mtime:
            version = known[0] + 1
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version},
                                                   "contentChanges": [{"text": text}]})
            self.opened[uri] = (version, mtime)
            return uri, True
        return uri, False

    def close(self) -> None:
        try:
            self.request("shutdown", {}, timeout=3)
            self.notify("exit", {})
        except Exception:  # noqa: BLE001 - the server is being stopped anyway  # nosec B110
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()


class LspManager:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        settings = settings or {}
        self.servers: list[tuple[str, list[str], list[str]]] = []
        for name, spec in (settings.get("servers") or {}).items():
            command = spec.get("command")
            if isinstance(command, str):
                command = command.split()
            if command:
                self.servers.append((name, [str(c) for c in command], [str(e).lower() for e in spec.get("extensions", [])]))
        if settings.get("auto", True):
            for name, command, extensions, _language in AUTO_SERVERS:
                if shutil.which(command[0]) and not any(set(extensions) & set(exts) for _n, _c, exts in self.servers):
                    self.servers.append((name, command, extensions))
        self.clients: dict[tuple[str, str], LspClient] = {}
        self._lock = threading.Lock()

    def server_for(self, path: str) -> tuple[str, list[str]] | None:
        extension = os.path.splitext(path)[1].lower()
        for name, command, extensions in self.servers:
            if extension in extensions:
                return name, command
        return None

    def client(self, path: str, root: str) -> LspClient:
        found = self.server_for(path)
        if found is None:
            known = ", ".join(sorted({e for _n, _c, exts in self.servers for e in exts})) or "none installed"
            raise LookupError(f"no language server for {os.path.splitext(path)[1] or 'this file'} (available: {known}); "
                              "install one (e.g. pip install python-lsp-server) or set [lsp.servers.<name>]")
        name, command = found
        with self._lock:
            key = (name, root)
            client = self.clients.get(key)
            if client is None or client.process.poll() is not None:
                client = self.clients[key] = LspClient(command, root)
            return client

    def shutdown(self) -> None:
        for client in list(self.clients.values()):
            client.close()
        self.clients.clear()


def _location_lines(locations: Any, root: str) -> list[str]:
    if not locations:
        return []
    if isinstance(locations, dict):
        locations = [locations]
    lines = []
    for loc in locations[:MAX_RESULTS]:
        uri = loc.get("uri") or loc.get("targetUri", "")
        span = loc.get("range") or loc.get("targetSelectionRange") or {}
        path = from_uri(uri)
        line = int((span.get("start") or {}).get("line", 0))
        col = int((span.get("start") or {}).get("character", 0))
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read().splitlines()[line].strip()
        except (OSError, IndexError):
            text = ""
        real = os.path.realpath(path)
        shown = os.path.relpath(real, root) if real.startswith(root + os.sep) else path
        lines.append(f"{shown}:{line + 1}:{col + 1}  {text[:160]}".rstrip())
    return lines


def _hover_text(result: Any) -> str:
    if not result:
        return ""
    contents = result.get("contents") if isinstance(result, dict) else result
    if isinstance(contents, dict):
        return str(contents.get("value") or "")
    if isinstance(contents, list):
        return "\n".join(str(c.get("value") if isinstance(c, dict) else c) for c in contents)
    return str(contents)


def _symbols(items: Any, depth: int = 0) -> list[str]:
    lines: list[str] = []
    kinds = {5: "class", 6: "method", 12: "function", 13: "variable", 14: "constant", 23: "struct", 11: "interface"}
    for item in items or []:
        span = item.get("selectionRange") or item.get("range") or (item.get("location") or {}).get("range") or {}
        line = int((span.get("start") or {}).get("line", 0)) + 1
        lines.append(f"{'  ' * depth}{kinds.get(item.get('kind'), 'symbol')} {item.get('name')}  (line {line})")
        lines += _symbols(item.get("children"), depth + 1)
    return lines


def lsp_plugin(settings: dict[str, Any] | None = None) -> Plugin:
    def apply(ctx: Context, _config: Any) -> None:
        manager = LspManager(settings)
        ctx.provide("lsp", manager)
        ctx.on_dispose(manager.shutdown)

        def run(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            action = str(args.get("action") or "").strip()
            rel = str(args.get("path") or "").strip()
            if action not in ("definition", "references", "hover", "diagnostics", "symbols"):
                return ToolResult("action must be definition, references, hover, diagnostics or symbols", is_error=True)
            path = rel if os.path.isabs(rel) else os.path.join(tool_ctx.cwd, rel)
            if not os.path.isfile(path):
                return ToolResult(f"no such file: {rel}", is_error=True)
            root = os.path.realpath(tool_ctx.cwd)
            try:
                client = manager.client(path, root)
                sent_at = time.monotonic()
                uri, sent = client.sync(path)
                if action == "diagnostics":
                    # Servers publish after an open or a change; wait for that report, briefly.
                    deadline = sent_at + DIAGNOSTICS_WAIT
                    while sent and time.monotonic() < deadline and client.diagnostics_seen.get(uri, 0) < sent_at:
                        time.sleep(0.05)
                    found = client.diagnostics.get(uri, [])
                    if not found:
                        return ToolResult(f"no diagnostics reported for {rel}")
                    return ToolResult("\n".join(
                        f"{rel}:{int(d['range']['start']['line']) + 1}:{int(d['range']['start']['character']) + 1} "
                        f"{SEVERITY.get(d.get('severity'), 'note')}: {d.get('message')}" for d in found[:MAX_RESULTS]))
                if action == "symbols":
                    lines = _symbols(client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}))
                    return ToolResult("\n".join(lines) if lines else "no symbols")
                line = int(args.get("line") or 0)
                if line < 1:
                    return ToolResult("line is required (1-based)", is_error=True)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    source = handle.read().splitlines()
                if line > len(source):
                    return ToolResult(f"{rel} has only {len(source)} lines", is_error=True)
                column = int(args.get("column") or 0)
                symbol = str(args.get("symbol") or "").strip()
                if symbol:
                    at = source[line - 1].find(symbol)
                    if at < 0:
                        return ToolResult(f"{symbol!r} is not on line {line}: {source[line - 1].strip()}", is_error=True)
                    column = at + 1
                if column < 1:
                    return ToolResult("give symbol (its name on that line) or column (1-based)", is_error=True)
                position = {"textDocument": {"uri": uri}, "position": {"line": line - 1, "character": column - 1}}
                if action == "hover":
                    text = _hover_text(client.request("textDocument/hover", position))
                    return ToolResult(text.strip() or "no information at that position")
                if action == "definition":
                    lines = _location_lines(client.request("textDocument/definition", position), root)
                else:
                    lines = _location_lines(client.request("textDocument/references",
                                                           {**position, "context": {"includeDeclaration": True}}), root)
                return ToolResult("\n".join(lines) if lines else f"no {action} found")
            except LookupError as error:
                return ToolResult(str(error), is_error=True)
            except (TimeoutError, RuntimeError, OSError, ValueError) as error:
                return ToolResult(f"language server: {error}", is_error=True)

        register_tools(ctx, [Tool(
            name="lsp",
            description=("Ask the language server about code: definition (where a name is defined), references "
                         "(every use), hover (type and docs), diagnostics (errors and warnings in a file), symbols "
                         "(outline of a file). For a position give line (1-based) and symbol, the name as it "
                         "appears on that line."),
            parameters={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["definition", "references", "hover", "diagnostics", "symbols"]},
                "path": {"type": "string"}, "line": {"type": "integer"},
                "symbol": {"type": "string"}, "column": {"type": "integer"}},
                "required": ["action", "path"]},
            execute=run,
        )])

    return Plugin("lsp", apply)
