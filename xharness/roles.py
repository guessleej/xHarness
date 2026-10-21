"""Agent roles, and the agents running right now.

A role is a named agent definition: what it is for, how it behaves (the body
becomes its system prompt), which tools it may use, which configured model
runs it, and how far it may go. The file layout is Claude Code's subagents --
a Markdown file with front matter in an `agents` directory -- so existing
definitions work unchanged:

    ---
    name: reviewer
    description: Reviews a change for bugs and missing tests
    tools: read, grep, glob        # optional; Claude Code names (Read, Grep) work too
    model: granite                 # optional; a provider name from [providers.*]
    max_turns: 12                  # optional
    max_tokens: 40000              # optional, per agent
    ---
    You are a careful reviewer. ...

Where roles come from, first match wins on a name clash (same order as skills):
  1. <working directory>/.xharness/agents    the person's own
  2. <working directory>/.claude/agents      same, in Claude Code's layout
  3. <server start directory>/.xharness/agents   the site's, for everyone
  4. $XHARNESS_HOME/agents                    installed for this machine
  5. [agents] dirs = [...]                    anything else the operator lists

A role can only narrow what an agent may do. Its tool list is intersected with
the tools the caller already has (a restricted tier's denied tools are gone
before that), and its model must be a configured provider this person may
use; otherwise the caller's model runs it and the result says so.

LiveAgents is the per-conversation register of agents that are working:
children, workflow steps and discussion participants. The Web UI lists them,
shows what each is doing and what it has spent, and stops one or all of them.
Every finished agent leaves an `agent-run` record in the session log, which
the usage report adds up per role.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from .context import Context, Plugin
from .session import harness_home
from .skills import parse_front_matter

ROLE_DIR = "agents"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_ROLES = 80
MAX_INDEX_ENTRIES = 40
MAX_DESCRIPTION_CHARS = 240
MAX_PROMPT_CHARS = 20_000
#: Claude Code tool names in a role's `tools:` line, and what they are here.
CLAUDE_NAMES = {"read": "read", "write": "write", "edit": "edit", "multiedit": "edit", "glob": "glob",
                "grep": "grep", "bash": "bash", "webfetch": "webfetch", "websearch": "web_search",
                "todowrite": "todo_write", "task": "subagent"}
#: Kept for the panel after an agent ends, so a stopped or failed one is still visible.
KEEP_FINISHED = 30


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] | None   # None: the caller's tools
    model: str | None               # a provider name; None: the caller's model
    max_turns: int | None
    max_tokens: int | None
    source: str
    path: str

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "tools": list(self.tools) if self.tools else None,
                "model": self.model, "max_turns": self.max_turns, "max_tokens": self.max_tokens, "source": self.source}


def _int(value: str | None) -> int | None:
    try:
        number = int(str(value).replace(",", "").replace("_", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_role(text: str, fallback_name: str, source: str, path: str) -> Role | None:
    fields, body = parse_front_matter(text)
    name = fields.get("name") or fallback_name
    if not NAME_RE.match(name):
        return None
    tools = None
    if fields.get("tools", "").strip():
        listed = [t.strip() for t in re.split(r"[,\s]+", fields["tools"]) if t.strip()]
        tools = tuple(dict.fromkeys(CLAUDE_NAMES.get(t.lower(), t) for t in listed))
    model = (fields.get("model") or "").strip() or None
    if model in ("inherit", "default"):
        model = None
    prompt = body.strip()
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n[role instructions truncated]"
    description = re.sub(r"\s+", " ", fields.get("description", "")).strip()[:MAX_DESCRIPTION_CHARS]
    return Role(name, description, prompt, tools, model, _int(fields.get("max_turns")),
                _int(fields.get("max_tokens")), source, path)


class RoleStore:
    def __init__(self, settings: dict[str, Any] | None = None, site_dir: str | None = None,
                 providers: dict[str, dict[str, Any]] | None = None, allowed: tuple[str, ...] = (),
                 describer: str = "") -> None:
        settings = settings or {}
        self.enabled = bool(settings.get("enabled", True))
        self.extra_dirs = [os.path.expanduser(str(d)) for d in settings.get("dirs", []) or []]
        self.site_dir = site_dir or os.getcwd()
        self.providers = dict(providers or {})
        self.allowed = tuple(allowed)
        self.describer = describer

    def roots(self, cwd: str | None) -> list[tuple[str, str]]:
        roots: list[tuple[str, str]] = []
        if cwd:
            roots += [(os.path.join(cwd, ".xharness", ROLE_DIR), "工作目錄"),
                      (os.path.join(cwd, ".claude", ROLE_DIR), "工作目錄")]
        roots.append((os.path.join(self.site_dir, ".xharness", ROLE_DIR), "站台"))
        roots.append((os.path.join(harness_home(), ROLE_DIR), "本機"))
        roots += [(d, "設定") for d in self.extra_dirs]
        return roots

    def discover(self, cwd: str | None = None) -> list[Role]:
        if not self.enabled:
            return []
        found: dict[str, Role] = {}
        seen: set[str] = set()
        for root, source in self.roots(cwd):
            real = os.path.realpath(root)
            if real in seen or not os.path.isdir(root):
                continue
            seen.add(real)
            for entry in sorted(os.listdir(root)):
                path = os.path.join(root, entry)
                if not entry.endswith(".md") or not os.path.isfile(path):
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        role = parse_role(handle.read(MAX_PROMPT_CHARS + 4096), entry[:-3], source, path)
                except OSError:
                    continue
                if role and role.name not in found:
                    found[role.name] = role
                if len(found) >= MAX_ROLES:
                    break
        return sorted(found.values(), key=lambda r: r.name)

    def get(self, name: str, cwd: str | None = None) -> Role | None:
        return next((r for r in self.discover(cwd) if r.name == name), None)

    def index(self, cwd: str | None = None) -> str | None:
        """The system-prompt section: names and one-line descriptions only."""
        roles = self.discover(cwd)
        if not roles:
            return None
        lines = [f"- {r.name}: {r.description or '(no description)'}" for r in roles[:MAX_INDEX_ENTRIES]]
        return ("## Agent roles\n\nNamed agents you can hand work to: pass `agent` with the role name to "
                "subagent, subagent_batch, workflow steps, or discussion participants. Each role has its "
                "own instructions, and may have its own tools and model.\n\n" + "\n".join(lines))

    def resolve_model(self, role: Role) -> tuple[Any, str]:
        """The adapter a role's model names, or (None, why not) to run on the caller's model."""
        if not role.model:
            return None, ""
        entry = self.providers.get(role.model)
        if not entry or not entry.get("model") or not entry.get("base_url") or role.model == self.describer:
            return None, f"role {role.name} names model {role.model!r}, which is not configured here; using the caller's model"
        if self.allowed and role.model not in self.allowed:
            return None, f"role {role.name} names model {role.model!r}, which this account may not use; using the caller's model"
        from .llm import OpenAIAdapter

        return OpenAIAdapter(**entry), ""


def exclusions(registry: Any, role: Role | None, always: set[str]) -> set[str]:
    """Tools hidden from an agent: `always` plus everything outside the role's list."""
    hidden = set(always)
    if role is not None and role.tools is not None:
        allowed = set(role.tools)
        hidden |= {tool.name for tool in registry.list() if tool.name not in allowed}
    return hidden


@dataclass
class LiveAgent:
    id: str
    name: str
    role: str
    kind: str          # subagent, workflow, discussion
    task: str
    agent: Any
    room: str = ""
    started: float = 0.0
    ended: float | None = None
    status: str = "running"   # running, done, failed, stopped
    activity: str = ""
    #: Set by stop(). Agent.run() clears its own stop flag when it starts, so a
    #: caller that runs the same agent again (a discussion's next turn) checks this.
    stop_wanted: bool = False

    def public(self) -> dict[str, Any]:
        usage = getattr(self.agent, "total_usage", None)
        tokens = (usage.prompt_tokens + usage.completion_tokens) if usage else 0
        return {"id": self.id, "name": self.name, "role": self.role, "kind": self.kind, "task": self.task[:200],
                "room": self.room, "status": self.status, "activity": self.activity[:160],
                "tokens": tokens, "calls": int(getattr(self.agent, "llm_calls", 0)),
                "model": getattr(getattr(self.agent, "llm", None), "model", "") or "",
                "seconds": round((self.ended or time.time()) - self.started, 1)}


class LiveAgents:
    """The agents working for one conversation (one harness)."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._items: dict[str, LiveAgent] = {}
        self._lock = threading.Lock()

    def start(self, agent: Any, name: str, role: str = "", kind: str = "subagent", task: str = "", room: str = "") -> str:
        live = LiveAgent(secrets.token_hex(4), name, role, kind, task, agent, room, time.time())
        with self._lock:
            self._items[live.id] = live
            finished = [k for k, v in self._items.items() if v.status != "running"]
            for key in finished[:-KEEP_FINISHED] if len(finished) > KEEP_FINISHED else []:
                self._items.pop(key, None)
        self._changed()
        return live.id

    def activity(self, live_id: str, text: str) -> None:
        live = self._items.get(live_id)
        if live is not None and live.status == "running":
            live.activity = text
            self._changed()

    def end(self, live_id: str, ok: bool) -> dict[str, Any] | None:
        live = self._items.get(live_id)
        if live is None:
            return None
        if live.status == "running":
            stopped = live.stop_wanted or bool(getattr(live.agent, "stop_requested", False))
            live.status = "stopped" if stopped else ("done" if ok else "failed")
        live.ended = time.time()
        record = live.public()
        session = self.ctx.optional("session")
        if session is not None:
            session.append({"type": "agent-run", **{k: record[k] for k in (
                "name", "role", "kind", "room", "status", "tokens", "calls", "model", "seconds")}})
        self._changed()
        return record

    def stop(self, live_id: str) -> bool:
        live = self._items.get(live_id)
        if live is None or live.status != "running":
            return False
        live.agent.stop()
        live.stop_wanted = True
        live.activity = "stopping"
        self._changed()
        return True

    def stopping(self, live_id: str) -> bool:
        live = self._items.get(live_id)
        return bool(live and live.stop_wanted)

    def stop_room(self, room: str) -> int:
        return sum(self.stop(k) for k, v in list(self._items.items()) if v.room == room and v.status == "running")

    def stop_all(self) -> int:
        return sum(self.stop(k) for k, v in list(self._items.items()) if v.status == "running")

    def running(self) -> int:
        return sum(1 for v in self._items.values() if v.status == "running")

    def public(self) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._items.values(), key=lambda v: v.started)
        return [item.public() for item in items]

    def _changed(self) -> None:
        self.ctx.emit("agents/changed", None)


def roles_plugin(settings: dict[str, Any] | None = None, site_dir: str | None = None,
                 providers: dict[str, dict[str, Any]] | None = None, allowed: tuple[str, ...] = (),
                 describer: str = "") -> Plugin:
    def apply(ctx: Context, _config: Any) -> None:
        ctx.provide("roles", RoleStore(settings, site_dir, providers, allowed, describer))
        ctx.provide("agents", LiveAgents(ctx))

    return Plugin("roles", apply)
