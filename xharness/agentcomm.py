"""Agents talking to each other: list who is working, send a message, report
back to whoever started you, stop a child.

Messages go into the receiving agent's inbox and are read at the start of its
next turn (agent.py), so a running agent never gets interrupted in the middle
of a step. An agent waiting inside a synchronous `subagent` call reads them
when that call returns; a background child (`subagent` with background) is
the case where reports arrive while the parent keeps working.

Only agents of the same conversation can reach each other. A message is text,
capped in size, and each agent holds a few; the receiver treats it as input
from another agent, not as an instruction from the person.
"""

from __future__ import annotations

import threading
import weakref
from typing import Any

from .context import Context, Plugin
from .tools import Tool, ToolContext, ToolResult, register_tools

MAX_MESSAGE_CHARS = 4_000
MAX_LOGGED_CHARS = 1_000
#: Hidden from discussion participants: they talk through the discussion, not around it.
COMM_TOOLS = {"send_message", "report", "list_agents", "interrupt_agent"}


class AgentMail:
    """Every agent of the conversation by name, so a message can find its receiver."""

    def __init__(self) -> None:
        self._agents: dict[str, weakref.ReferenceType[Any]] = {}
        self._lock = threading.Lock()

    def add(self, agent: Any) -> None:
        with self._lock:
            self._agents[agent.options.name] = weakref.ref(agent)

    def get(self, name: str) -> Any | None:
        with self._lock:
            ref = self._agents.get(name)
        return ref() if ref else None


def agentcomm_plugin() -> Plugin:
    def apply(ctx: Context, _config: Any) -> None:
        mail = AgentMail()
        ctx.provide("agentmail", mail)

        def live():
            return ctx.optional("agents")

        def deliver(sender: str, target: str, text: str) -> ToolResult:
            text = text.strip()
            if not text:
                return ToolResult("message is required", is_error=True)
            if len(text) > MAX_MESSAGE_CHARS:
                return ToolResult(f"message is {len(text)} characters; the limit is {MAX_MESSAGE_CHARS}. "
                                  "Say it shorter, or write it to a file and send the path.", is_error=True)
            if target == sender:
                return ToolResult("an agent cannot message itself", is_error=True)
            agents = live()
            record = agents.find(target) if agents is not None else None
            if target != "main" and (record is None or record.status != "running"):
                names = ", ".join(a["name"] for a in agents.public() if a["status"] == "running") if agents else ""
                return ToolResult(f"no running agent named {target!r}; running now: {names or '(none)'}", is_error=True)
            receiver = mail.get(target)
            if receiver is None:
                return ToolResult(f"agent {target!r} cannot be reached", is_error=True)
            if not receiver.deliver(sender, text):
                return ToolResult(f"{target}'s inbox is full; wait for it to read what it has", is_error=True)
            session = ctx.optional("session")
            if session is not None:
                session.append({"type": "agent-message", "from": sender, "to": target, "chars": len(text),
                                "text": text[:MAX_LOGGED_CHARS]})
            ctx.emit("agent/message", {"from": sender, "to": target})
            return ToolResult(f"sent to {target}; it reads the message at the start of its next step")

        def send_message(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            target = str(args.get("to") or "").strip()
            if target in ("parent", "上層"):
                agents = live()
                me = agents.find(tool_ctx.agent) if agents is not None else None
                if me is None or not me.parent:
                    return ToolResult("you were not started by another agent, so you have no parent", is_error=True)
                target = me.parent
            if not target:
                return ToolResult("to is required: an agent name from list_agents, or \"parent\"", is_error=True)
            return deliver(tool_ctx.agent, target, str(args.get("message") or ""))

        def report(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            agents = live()
            me = agents.find(tool_ctx.agent) if agents is not None else None
            if me is None or not me.parent:
                return ToolResult("only an agent that another agent started can report", is_error=True)
            return deliver(tool_ctx.agent, me.parent, "Report: " + str(args.get("message") or "").strip())

        def list_agents(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            agents = live()
            rows = agents.public() if agents is not None else []
            if str(args.get("scope") or "running") == "running":
                rows = [r for r in rows if r["status"] == "running"]
            lines = [f"You are {tool_ctx.agent}."]
            for row in rows:
                lines.append(f"- {row['name']} ({row['status']}"
                             + (f", role {row['role']}" if row["role"] else "")
                             + (f", started by {row['parent']}" if row["parent"] else "")
                             + f"): {row['activity'] or row['task'][:80] or 'idle'}")
            if len(lines) == 1:
                lines.append("No other agent is running.")
            return ToolResult("\n".join(lines))

        def interrupt_agent(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            who = str(args.get("agent") or "").strip()
            agents = live()
            record = agents.find(who) if agents is not None and who else None
            if record is None or record.status != "running":
                return ToolResult(f"no running agent named {who!r}", is_error=True)
            if tool_ctx.agent != "main" and not agents.is_descendant(record.name, tool_ctx.agent):
                return ToolResult("you can only stop agents that you started", is_error=True)
            if not tool_ctx.approve(f"stop agent {record.name}"):
                return ToolResult("denied by approval policy", is_error=True)
            agents.stop(record.id)
            return ToolResult(f"asked {record.name} to stop; it ends before its next model call or tool")

        register_tools(ctx, [
            Tool(
                name="list_agents",
                description="List the agents working in this conversation: who they are, who started them, what each is doing.",
                parameters={"type": "object", "properties": {
                    "scope": {"type": "string", "enum": ["running", "all"], "description": "default running"}}},
                execute=list_agents,
            ),
            Tool(
                name="send_message",
                description=("Send a short message to another running agent of this conversation (a name from "
                             "list_agents, or \"parent\" for the agent that started you). It reads it at the start "
                             "of its next step. Use it to hand over a finding or ask for one; do not use it to wait for an answer."),
                parameters={"type": "object", "properties": {
                    "to": {"type": "string"}, "message": {"type": "string", "description": f"up to {MAX_MESSAGE_CHARS} characters"}},
                    "required": ["to", "message"]},
                execute=send_message,
            ),
            Tool(
                name="report",
                description=("Tell the agent that started you something it needs before you finish: a partial finding "
                             "that changes what it should do next, or progress on a long task. Your final answer is "
                             "returned on its own; do not repeat it here."),
                parameters={"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
                execute=report,
            ),
            Tool(
                name="interrupt_agent",
                description="Stop a running agent that you started (the main agent may stop any). It ends before its next model call or tool.",
                parameters={"type": "object", "properties": {"agent": {"type": "string"}}, "required": ["agent"]},
                execute=interrupt_agent,
                mutating=True,
            ),
        ])

    return Plugin("agentcomm", apply)
