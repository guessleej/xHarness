"""Multi-node fleet: watch and steer harnesses running on other machines.

Every machine runs its own `xharness web` (a node). One of them, or any
machine with a config listing the nodes, acts as the hub: its fleet view
polls each node's /api/fleet and shows everything together, and forwards
approve / deny / stop actions to the node that owns the conversation.

Trust model: a node must bind a non-loopback address with --token, and the
hub keeps that token only in an environment variable named by `token_env`.
The hub forwards exactly two actions (approvals, stop) on an allow-list;
the browser never sees node tokens and never talks to nodes directly.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 3.0
FORWARDABLE = {"approvals", "stop"}


@dataclass
class Node:
    name: str
    url: str
    token_env: str | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env) if self.token_env else None


def node_name(config: dict[str, Any] | None) -> str:
    return str((config or {}).get("name") or socket.gethostname())


def load_nodes(config: dict[str, Any] | None) -> list[Node]:
    nodes: list[Node] = []
    for name, entry in ((config or {}).get("nodes") or {}).items():
        if not isinstance(entry, dict) or not entry.get("url"):
            raise ValueError(f"fleet node {name!r} needs a url")
        url = str(entry["url"]).rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"fleet node {name!r}: url must be http(s)")
        nodes.append(
            Node(
                name=str(name),
                url=url,
                token_env=str(entry["token_env"]) if entry.get("token_env") else None,
                timeout=float(entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            )
        )
    return nodes


def _request(node: Node, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    headers = {"Accept": "application/json", "X-XHarness-Client": "hub"}
    if node.token:
        headers["Authorization"] = f"Bearer {node.token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"{node.url}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=node.timeout) as response:  # nosec B310 - http(s) enforced in load_nodes
            return response.status, json.loads(response.read().decode("utf-8", errors="replace") or "null")
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8", errors="replace") or "null")
        except ValueError:
            payload = None
        return error.code, payload


def poll_node(node: Node) -> dict[str, Any]:
    """One node's fleet snapshot, or why it could not be read."""
    started = time.monotonic()
    try:
        status, payload = _request(node, "GET", "/api/fleet")
    except (urllib.error.URLError, OSError, ValueError) as error:
        return {"name": node.name, "url": node.url, "ok": False, "error": str(getattr(error, "reason", error))[:160], "summary": None, "items": []}
    latency = round((time.monotonic() - started) * 1000)
    if status != 200 or not isinstance(payload, dict):
        return {"name": node.name, "url": node.url, "ok": False, "error": f"HTTP {status}", "summary": None, "items": [], "latency_ms": latency}
    return {
        "name": node.name,
        "url": node.url,
        "ok": True,
        "node": payload.get("node"),
        "version": payload.get("version"),
        "model": payload.get("model"),
        "summary": payload.get("summary"),
        "items": payload.get("items") or [],
        "latency_ms": latency,
    }


def poll_all(nodes: list[Node]) -> list[dict[str, Any]]:
    if not nodes:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
        return list(pool.map(poll_node, nodes))


def forward(node: Node, conv_id: str, action: str, body: dict[str, Any]) -> tuple[int, Any]:
    """Forward an allow-listed action to the node that owns the conversation."""
    if action not in FORWARDABLE:
        return 404, {"error": "action not forwardable"}
    if not conv_id.isalnum():
        return 400, {"error": "bad conversation id"}
    try:
        return _request(node, "POST", f"/api/conversations/{conv_id}/{action}", body)
    except (urllib.error.URLError, OSError, ValueError) as error:
        return 502, {"error": f"node unreachable: {str(getattr(error, 'reason', error))[:120]}"}


def format_table(node_reports: list[dict[str, Any]]) -> str:
    lines = []
    for report in node_reports:
        if not report["ok"]:
            lines.append(f"{report['name']:16} DOWN   {report['url']}  {report.get('error', '')}")
            continue
        summary = report["summary"] or {}
        lines.append(
            f"{report['name']:16} up     {report['url']}  {report.get('latency_ms', 0)}ms  "
            f"conversations={summary.get('conversations', 0)} running={summary.get('running', 0)} "
            f"waiting={summary.get('waiting', 0)} children={summary.get('children', 0)} "
            f"tokens={summary.get('total_tokens', 0)}  model={report.get('model') or '-'}"
        )
        for item in report["items"]:
            usage = item.get("usage") or {}
            pending = len(item.get("pending_approvals") or [])
            lines.append(
                f"    {item.get('state', '?'):8} {usage.get('total_tokens', 0):>7} tok  "
                f"{'pending=' + str(pending) + '  ' if pending else ''}{(item.get('preview') or '(no message)')[:60]}"
            )
    return "\n".join(lines) if lines else "no fleet nodes configured"


def poll_usage(node: Node, since: str = "7d", bucket: str | None = None) -> dict[str, Any]:
    """One node's usage history, or why it could not be read."""
    query = f"/api/usage?since={since}" + (f"&bucket={bucket}" if bucket else "")
    try:
        status, payload = _request(node, "GET", query)
    except (urllib.error.URLError, OSError, ValueError) as error:
        return {"name": node.name, "url": node.url, "ok": False, "error": str(getattr(error, "reason", error))[:160]}
    if status != 200 or not isinstance(payload, dict):
        return {"name": node.name, "url": node.url, "ok": False, "error": f"HTTP {status}"}
    return {"name": node.name, "url": node.url, "ok": True, "history": payload}


def poll_usage_all(nodes: list[Node], since: str = "7d", bucket: str | None = None) -> list[dict[str, Any]]:
    if not nodes:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
        return list(pool.map(lambda node: poll_usage(node, since, bucket), nodes))
