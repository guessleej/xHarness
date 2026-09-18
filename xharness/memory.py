"""Memory layer: persistent, auditable memory across sessions.

Memories are plain Markdown files a human can read, edit, grep, and delete —
one file per fact under $XHARNESS_HOME/memory/, plus a generated MEMORY.md
index. The model gets the index (names and one-line descriptions) in its
system prompt on every call through an "llm/stream" middleware, and reads a
memory in full only when it asks. Every write and delete goes through the
approval policy and lands in memory/audit.jsonl with the agent and session
that made it, so "what does the agent remember and who taught it" is always
answerable from disk.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .context import Context, Plugin
from .session import harness_home
from .tools import Tool, ToolContext, ToolResult, register_tools

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_MEMORY_CHARS = 8_000
MAX_DESCRIPTION_CHARS = 200
DEFAULT_MAX_INDEX_CHARS = 6_000
DEFAULT_SEARCH_LIMIT = 8
INDEX_FILE = "MEMORY.md"
TOPICS_DIR = "topics"
AUDIT_FILE = "audit.jsonl"


@dataclass
class Memory:
    name: str
    description: str
    kind: str
    updated: str
    body: str
    topic: str = ""

    def __post_init__(self) -> None:
        self.topic = _slugify(self.topic) or _slugify(self.kind) or "note"

    def to_text(self) -> str:
        return (
            "---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            f"kind: {self.kind}\n"
            f"topic: {self.topic}\n"
            f"updated: {self.updated}\n"
            "---\n\n"
            f"{self.body.rstrip()}\n"
        )


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9-]+", "-", (text or "").strip().lower()).strip("-")
    return text[:48]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_memory(name: str, text: str) -> Memory:
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                key, _, value = line.partition(":")
                if key.strip():
                    meta[key.strip()] = value.strip()
            body = text[end + 4 :].lstrip("\n")
    return Memory(
        name=meta.get("name", name),
        description=meta.get("description", body.strip().splitlines()[0][:MAX_DESCRIPTION_CHARS] if body.strip() else ""),
        kind=meta.get("kind", "note"),
        updated=meta.get("updated", ""),
        body=body,
        topic=meta.get("topic", ""),
    )


class MemoryStore:
    def __init__(self, directory: str, max_index_chars: int = DEFAULT_MAX_INDEX_CHARS) -> None:
        self.directory = directory
        self.max_index_chars = max_index_chars
        self._lock = threading.Lock()
        os.makedirs(directory, exist_ok=True)

    # --- files ---------------------------------------------------------
    def _path(self, name: str) -> str:
        return os.path.join(self.directory, f"{name}.md")

    def list(self) -> list[Memory]:
        memories: list[Memory] = []
        for filename in sorted(os.listdir(self.directory)):
            if not filename.endswith(".md") or filename == INDEX_FILE:
                continue
            name = filename[:-3]
            try:
                with open(self._path(name), encoding="utf-8", errors="replace") as handle:
                    memories.append(parse_memory(name, handle.read()))
            except OSError:
                continue
        return memories

    def read(self, name: str) -> Memory | None:
        if not SLUG.match(name):
            return None
        try:
            with open(self._path(name), encoding="utf-8", errors="replace") as handle:
                return parse_memory(name, handle.read())
        except OSError:
            return None

    def write(
        self,
        name: str,
        description: str,
        body: str,
        kind: str = "note",
        actor: dict[str, Any] | None = None,
        topic: str = "",
        action: str | None = None,
    ) -> Memory:
        if not SLUG.match(name):
            raise ValueError("name must be a slug: lowercase letters, digits, and dashes (max 64)")
        description = " ".join(description.split())[:MAX_DESCRIPTION_CHARS]
        if not description:
            raise ValueError("description is required")
        if len(body) > MAX_MEMORY_CHARS:
            raise ValueError(f"content exceeds {MAX_MEMORY_CHARS} characters")
        kind = " ".join(kind.split())[:32] or "note"
        memory = Memory(name=name, description=description, kind=kind, updated=_now(), body=body, topic=topic)
        with self._lock:
            existed = os.path.exists(self._path(name))
            with open(self._path(name), "w", encoding="utf-8") as handle:
                handle.write(memory.to_text())
            self._rebuild_index()
            self._audit(action or ("update" if existed else "create"), name, actor)
        return memory

    def delete(self, name: str, actor: dict[str, Any] | None = None, action: str = "delete") -> bool:
        if not SLUG.match(name):
            return False
        with self._lock:
            try:
                os.remove(self._path(name))
            except FileNotFoundError:
                return False
            self._rebuild_index()
            self._audit(action, name, actor)
        return True

    # --- topics ----------------------------------------------------------
    def topics(self) -> dict[str, list[Memory]]:
        grouped: dict[str, list[Memory]] = {}
        for memory in self.list():
            grouped.setdefault(memory.topic, []).append(memory)
        return dict(sorted(grouped.items()))

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        return list(reversed(self.audit(limit)))

    # --- index and search ----------------------------------------------
    def index_lines(self) -> list[str]:
        lines: list[str] = []
        for topic, memories in self.topics().items():
            lines.append(f"[{topic}]")
            lines.extend(f"- {memory.name} ({memory.kind}): {memory.description}" for memory in memories)
        return lines

    def index_text(self) -> str:
        """The index the model sees; capped so a large memory never floods the prompt."""
        lines = self.index_lines()
        if not lines:
            return ""
        text = "\n".join(lines)
        if len(text) > self.max_index_chars:
            kept: list[str] = []
            used = 0
            for line in lines:
                if used + len(line) + 1 > self.max_index_chars:
                    break
                kept.append(line)
                used += len(line) + 1
            text = "\n".join(kept) + f"\n- ... {len(lines) - len(kept)} more; use memory_search"
        return text

    def _rebuild_index(self) -> None:
        topics = self.topics()
        content = "# Memory index\n\nGenerated by xHarness; edit the individual files, not this list.\n"
        if not topics:
            content += "\n(no memories yet)\n"
        for topic, memories in topics.items():
            content += f"\n## {topic} ({len(memories)})\n\n"
            content += "".join(
                f"- [{memory.name}]({memory.name}.md) ({memory.kind}): {memory.description}\n" for memory in memories
            )
        with open(os.path.join(self.directory, INDEX_FILE), "w", encoding="utf-8") as handle:
            handle.write(content)
        # Topic pages: one generated Markdown page per topic with every memory in full,
        # so a human can read a whole subject without opening a dozen files.
        topics_dir = os.path.join(self.directory, TOPICS_DIR)
        os.makedirs(topics_dir, exist_ok=True)
        wanted = set()
        for topic, memories in topics.items():
            wanted.add(f"{topic}.md")
            page = f"# {topic}\n\nGenerated by xHarness from {len(memories)} memories; edit the memory files, not this page.\n"
            for memory in memories:
                page += f"\n## {memory.name}\n\n_{memory.description}_ ({memory.kind}, {memory.updated[:10]})\n\n{memory.body.rstrip()}\n"
            with open(os.path.join(topics_dir, f"{topic}.md"), "w", encoding="utf-8") as handle:
                handle.write(page)
        for stale in os.listdir(topics_dir):
            if stale.endswith(".md") and stale not in wanted:
                os.remove(os.path.join(topics_dir, stale))

    def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[tuple[Memory, int, str]]:
        terms = [term for term in query.lower().split() if term]
        if not terms:
            return []
        results: list[tuple[Memory, int, str]] = []
        for memory in self.list():
            haystack = f"{memory.name} {memory.description} {memory.body}".lower()
            if not all(term in haystack for term in terms):
                continue
            score = sum(haystack.count(term) for term in terms)
            score += 5 * sum(term in memory.name.lower() or term in memory.description.lower() for term in terms)
            first = min(haystack.find(term) for term in terms)
            start = max(0, first - 60)
            snippet = " ".join(memory.body[start : start + 160].split()) if memory.body else memory.description
            results.append((memory, score, snippet))
        results.sort(key=lambda item: (-item[1], item[0].name))
        return results[:limit]

    # --- audit ---------------------------------------------------------
    def _audit(self, action: str, name: str, actor: dict[str, Any] | None) -> None:
        record = {"ts": _now(), "action": action, "name": name, **(actor or {})}
        with open(os.path.join(self.directory, AUDIT_FILE), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def audit(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            with open(os.path.join(self.directory, AUDIT_FILE), encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return []
        records = []
        for line in lines[-limit:]:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records


def default_memory_dir() -> str:
    return os.path.join(harness_home(), "memory")


def memory_plugin(config: dict[str, Any] | None = None) -> Plugin:
    settings = config or {}

    def apply(ctx: Context, _config: Any) -> None:
        store = MemoryStore(
            str(settings.get("dir") or default_memory_dir()),
            max_index_chars=int(settings.get("max_index_chars", DEFAULT_MAX_INDEX_CHARS)),
        )
        ctx.provide("memory", store)

        def actor(tool_ctx: ToolContext) -> dict[str, Any]:
            session = ctx.optional("session")
            return {"agent": tool_ctx.agent, "session": session.id if session else None}

        def note_session(action: str, name: str) -> None:
            session = ctx.optional("session")
            if session:
                session.append({"type": "memory", "action": action, "name": name})

        # The model sees the index on every call, injected into the system
        # message of a shallow copy; the agent's own message list is untouched.
        def inject(payload: Any, next_call: Any) -> Any:
            index = store.index_text()
            messages = payload["messages"]
            if index and messages and messages[0].get("role") == "system":
                head = dict(messages[0])
                head["content"] = (
                    f"{head['content']}\n\n## Memory index\n"
                    "Facts you saved earlier (name: description). Call memory_read for the full "
                    "text, memory_search to find more, memory_write to save something worth keeping.\n"
                    f"{index}"
                )
                payload = {**payload, "messages": [head, *messages[1:]]}
            return next_call(payload)

        ctx.intercept("llm/stream", inject)

        def write(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            name = str(args.get("name") or "").strip().lower()
            if not SLUG.match(name):
                return ToolResult("name must be a slug: lowercase letters, digits, dashes (max 64)", is_error=True)
            if not tool_ctx.approve(f"memory_write: {name}"):
                return ToolResult("denied by approval policy", is_error=True)
            try:
                memory = store.write(
                    name,
                    str(args.get("description") or ""),
                    str(args.get("content") or ""),
                    kind=str(args.get("kind") or "note"),
                    actor=actor(tool_ctx),
                    topic=str(args.get("topic") or ""),
                )
            except ValueError as error:
                return ToolResult(str(error), is_error=True)
            note_session("write", name)
            return ToolResult(f"saved memory {memory.name} ({memory.kind}): {memory.description}")

        def read(args: dict[str, Any], _tool_ctx: ToolContext) -> ToolResult:
            memory = store.read(str(args.get("name") or "").strip().lower())
            if memory is None:
                return ToolResult("no such memory", is_error=True)
            return ToolResult(memory.to_text())

        def search(args: dict[str, Any], _tool_ctx: ToolContext) -> ToolResult:
            limit = min(int(args.get("limit") or DEFAULT_SEARCH_LIMIT), 25)
            hits = store.search(str(args.get("query") or ""), limit=limit)
            if not hits:
                return ToolResult("no matching memories")
            return ToolResult(
                "\n".join(f"- {memory.name} ({memory.kind}): {memory.description}\n    {snippet}" for memory, _score, snippet in hits)
            )

        def list_all(_args: dict[str, Any], _tool_ctx: ToolContext) -> ToolResult:
            lines = store.index_lines()
            return ToolResult("\n".join(lines) if lines else "no memories yet")

        def delete(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            name = str(args.get("name") or "").strip().lower()
            if not tool_ctx.approve(f"memory_delete: {name}"):
                return ToolResult("denied by approval policy", is_error=True)
            if not store.delete(name, actor=actor(tool_ctx)):
                return ToolResult("no such memory", is_error=True)
            note_session("delete", name)
            return ToolResult(f"deleted memory {name}")

        def consolidate(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            from .consolidate import apply_plan, build_plan  # local import: avoids a cycle at package load

            topic = _slugify(str(args.get("topic") or ""))
            if not topic:
                return ToolResult("topic is required", is_error=True)
            plan = build_plan(store, topic, llm=ctx.optional("llm") if args.get("use_model", True) else None)
            if plan.empty:
                return ToolResult(plan.describe())
            if not args.get("apply"):
                return ToolResult(plan.describe() + "\n\n(dry run; call again with apply=true to execute)")
            if not tool_ctx.approve(f"memory_consolidate: {topic} ({len(plan.merges)} merges, {len(plan.deletes)} deletes)"):
                return ToolResult("denied by approval policy", is_error=True)
            done = apply_plan(store, plan, actor=actor(tool_ctx))
            for line in done:
                note_session("consolidate", line)
            return ToolResult(plan.describe() + "\n\napplied:\n" + ("\n".join(f"- {line}" for line in done) or "- nothing"))

        slug_param = {"type": "string", "description": "Slug: lowercase letters, digits, dashes"}
        register_tools(
            ctx,
            [
                Tool(
                    name="memory_write",
                    description=(
                        "Save or update a durable memory for future sessions: a fact about the user, "
                        "the project, a decision, or a correction. Keep one fact per memory. Writing "
                        "the same name again replaces it."
                    ),
                    mutating=True,
                    parameters={
                        "type": "object",
                        "properties": {
                            "name": slug_param,
                            "description": {"type": "string", "description": "One line shown in the index"},
                            "content": {"type": "string", "description": "The memory body (Markdown)"},
                            "kind": {"type": "string", "description": "e.g. user, project, feedback, reference"},
                            "topic": {"type": "string", "description": "Topic page to file it under; defaults to kind"},
                        },
                        "required": ["name", "description", "content"],
                    },
                    execute=write,
                ),
                Tool(
                    name="memory_read",
                    description="Read one memory in full by name.",
                    parameters={"type": "object", "properties": {"name": slug_param}, "required": ["name"]},
                    execute=read,
                ),
                Tool(
                    name="memory_search",
                    description="Search memories by keywords (all terms must match).",
                    parameters={
                        "type": "object",
                        "properties": {"query": {"type": "string"}, "limit": {"type": "number"}},
                        "required": ["query"],
                    },
                    execute=search,
                ),
                Tool(
                    name="memory_list",
                    description="List every memory: name, kind, and description.",
                    parameters={"type": "object", "properties": {}},
                    execute=list_all,
                ),
                Tool(
                    name="memory_consolidate",
                    description=(
                        "Tidy one memory topic: find near-duplicate, contradictory, or stale memories and "
                        "merge or delete them. Without apply=true it only returns the plan."
                    ),
                    mutating=True,
                    parameters={
                        "type": "object",
                        "properties": {
                            "topic": {"type": "string", "description": "Topic to consolidate (see memory_list)"},
                            "apply": {"type": "boolean", "description": "Execute the plan (default: dry run)"},
                            "use_model": {"type": "boolean", "description": "Also ask the model for proposals (default true)"},
                        },
                        "required": ["topic"],
                    },
                    execute=consolidate,
                ),
                Tool(
                    name="memory_delete",
                    description="Delete a memory that is wrong or obsolete.",
                    mutating=True,
                    parameters={"type": "object", "properties": {"name": slug_param}, "required": ["name"]},
                    execute=delete,
                ),
            ],
        )

    return Plugin("memory", apply)
