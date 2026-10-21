"""The default agent loop: call the model, execute tool calls, feed results
back, and stop when the model answers without tool calls or max_turns hits.
Model calls go through the interceptable "llm/stream" seam.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .context import Context
from .llm import AssistantTurn, Usage
from .session import SessionLog
from .tools import ToolContext, ToolRegistry, ToolResult

DEFAULT_MAX_TURNS = 40
#: Tool output beyond this many characters is saved to a file and the model
#: gets the head, the tail and the path. Granite on .70 serves 32k tokens; one
#: 50k-character build log used to fill most of that by itself.
INLINE_OUTPUT_CHARS = 16_000
SPILL_HEAD_CHARS = 12_000
SPILL_TAIL_CHARS = 3_000
SPILL_KEEP_FILES = 20
#: Same tool, same arguments, this many times in a row: tell the model the
#: answer will not change. Advice only -- the call still runs.
REPEAT_WARNINGS = (3, 5, 8)
#: Tool results kept verbatim when the conversation no longer fits the model.
KEEP_RECENT_TOOL_RESULTS = 2
PRUNED_OUTPUT = "[output removed to fit the context window; run the tool again if you need it]"
#: Compaction: the model rewrites everything before the latest request as a
#: summary, so a long conversation keeps going instead of hitting the wall.
SUMMARY_PREFIX = "[Summary of the earlier conversation, written by xHarness to save context]\n"
SUMMARY_ACK = "Understood. I will continue from this summary."
SUMMARY_SYSTEM = (
    "You compact a conversation between a person and an AI agent so the work can continue with less "
    "context. Write a summary in the language and script the person used: the goal, what has been done, "
    "facts learned (file paths, commands, results, decisions) and what is still open. Keep names, paths "
    "and numbers exact; never add anything that is not in the conversation. Plain text, at most 400 words."
)
MAX_SUMMARY_SOURCE_CHARS = 60_000
#: Plan mode: look, propose, and wait for approval before changing anything.
PLAN_CONTEXT = (
    "Plan mode is on: do not change anything yet. Tools that write, edit or run commands are switched off. "
    "Investigate with the read-only tools, then reply with a numbered plan: what you will change and where, "
    "in what order, and how you will check the result. End your turn there; the person approves the plan "
    "before anything is changed."
)
_CONTEXT_OVERFLOW = re.compile(
    r"exceed\w*.{0,40}context|context.{0,20}(length|size|window)|maximum context|too many tokens|prompt is too long",
    re.IGNORECASE,
)
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_INSTRUCTIONS_CHARS = 24_000


def _read_instructions(directory: str) -> tuple[str, str] | None:
    """(filename, text) of the first instruction file in this directory."""
    for name in INSTRUCTION_FILES:
        try:
            with open(os.path.join(directory, name), encoding="utf-8", errors="replace") as handle:
                text = handle.read().strip()
        except OSError:
            continue
        if text:
            if len(text) > MAX_INSTRUCTIONS_CHARS:
                text = text[:MAX_INSTRUCTIONS_CHARS] + "\n[instructions truncated]"
            return name, text
    return None


def load_project_instructions(cwd: str, site_dir: str | None = None) -> str | None:
    """Agent instructions for this task: the site's, then the working directory's.

    `site_dir` is where the server itself was started. It matters once each
    person works in their own directory: an operator's AGENTS.md would
    otherwise stop reaching anybody the moment accounts are switched on,
    which is exactly when a shared policy is most wanted. The working
    directory's own file comes second so it can refine the site's.

    Content is treated like any other repo content: useful context, but a
    trust decision — see docs/ssdlc.md.
    """
    sections: list[str] = []
    seen: set[str] = set()
    for directory, label in ((site_dir, "site"), (cwd, "project")):
        if not directory:
            continue
        resolved = os.path.realpath(directory)
        if resolved in seen:
            continue
        seen.add(resolved)
        found = _read_instructions(directory)
        if found:
            name, text = found
            heading = "Site instructions" if label == "site" else "Project instructions"
            sections.append(f"## {heading} ({name})\n\n{text}")
    return "\n\n".join(sections) if sections else None


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def current_date_line(now: datetime | None = None) -> str:
    """Today in the server's local time zone, with the weekday.

    Without it the model assumes a date from its training data: asked on
    2026-10-14 to move a notice to "next Friday", granite stated today was
    2025-04-21. This line fixes the year and the day. It does not make the
    model good at date arithmetic -- measured 2026-10-14, granite still got
    "下週五" wrong in 8 of 8 runs even with every date of this week and next
    listed beside it, so that calendar was removed rather than kept as a
    remedy that does not work.
    """
    now = now or datetime.now().astimezone()
    # Offset only: zone abbreviations are ambiguous (Taipei reports "CST",
    # which is also US Central and China Standard Time).
    offset = now.strftime("%z")
    zone = f" (UTC{offset[:3]}:{offset[3:]})" if offset else ""
    return (f"Today is {now:%Y-%m-%d} ({WEEKDAYS[now.weekday()]}), local time {now:%H:%M}{zone}. "
            "Use this date; do not assume one.")


def spill_output(output: str, cwd: str) -> str:
    """Keep an oversized tool result out of the context, but not out of reach.

    The full text goes to .xharness/spill/ in the working directory, where the
    read tool can page through it; the model sees the head, the tail (where
    build errors usually are) and the path. Dropping the rest, as the tools
    used to, left the model nothing to go back to.
    """
    if len(output) <= INLINE_OUTPUT_CHARS:
        return output
    directory = os.path.join(cwd, ".xharness", "spill")
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.txt"
    try:
        os.makedirs(directory, exist_ok=True)
        ignore = os.path.join(cwd, ".xharness", ".gitignore")
        if not os.path.exists(ignore):
            with open(ignore, "w", encoding="utf-8") as handle:
                handle.write("*\n")  # never shows up in the user's git status
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(output)
        old = sorted(entry for entry in os.listdir(directory) if entry.endswith(".txt"))
        for entry in old[:-SPILL_KEEP_FILES]:
            os.remove(os.path.join(directory, entry))
        where = (f"the full output ({len(output):,} characters) is in .xharness/spill/{name}; "
                 "read it with offset and limit if you need the middle")
    except OSError:
        where = f"the middle was dropped ({len(output):,} characters in all)"
    return (f"{output[:SPILL_HEAD_CHARS]}\n\n[... {where} ...]\n\n{output[-SPILL_TAIL_CHARS:]}")


def is_context_overflow(error: Exception) -> bool:
    return bool(_CONTEXT_OVERFLOW.search(str(error)))


def default_system_prompt(cwd: str, model: str | None = None) -> str:
    identity = (
        f"You run on the model '{model}', an open-weight model served on the operator's own "
        "infrastructure. If asked which model or vendor you are, state exactly that; never claim "
        "to be GPT, Claude, Gemini, or any other vendor's model."
        if model
        else "You run on a model served on the operator's own infrastructure; never claim to be "
        "GPT, Claude, Gemini, or any other vendor's model."
    )
    return "\n".join(
        [
            "You are xHarness, a coding agent by xCloudinfo.",
            identity,
            f"Working directory: {cwd}",
            "Use the available tools to inspect and change files and to run commands.",
            "Prefer small verified steps: read before you edit, run checks after you change.",
            # Measured: half the runs of an ambiguous file task asked without
            # listing the directory, where the candidates were sitting.
            "Look before you ask: when the answer might be in the working directory (which file, "
            "what is already there), check with your tools first, then ask only about what is "
            "still unclear.",
            # Without this the model asks in prose and ends its turn, which a
            # headless run never sees and a phone gets as a wall of text.
            "If you need something only the person can tell you -- which of several things they "
            "meant, a preference, a fact you cannot find with your tools -- and an ask_user tool is "
            "available, call it instead of ending your turn with a question. Never fill such a gap "
            "with an invented command, file path or fact.",
            # Measured 2026-10-14: granite turned "下週五" into the wrong date in
            # 8 of 8 runs, even with the calendar beside it. A person catches
            # that in one glance; a file does not.
            "When the task gives a relative date (next Friday, in two weeks, 下週五) and you will "
            "write it into a file or message, work it out from today's date, then confirm the exact "
            "date with the person before writing it -- with ask_user if available, otherwise by "
            "stating the date you used in your reply.",
            # "Chinese" is one language to the model; left alone it answers a
            # Traditional-Chinese user in Simplified.
            "When the task is done, reply with a concise summary in the language the user used, "
            "in the same script: Traditional Chinese for a user writing Traditional Chinese.",
        ]
    )


@dataclass
class AgentOptions:
    system_prompt: str | None = None
    system_suffix: str | None = None  # appended to whichever system prompt applies (channel guidance etc.)
    max_turns: int = DEFAULT_MAX_TURNS
    cwd: str | None = None
    #: "auto" approves mutating tools silently; "prompt" asks via `prompt`.
    approval_mode: str = "auto"
    #: Asks the human. Absent under approval_mode "prompt" means deny.
    prompt: Callable[[str], bool] | None = None
    #: Puts a question from the ask_user tool to the human; returns their reply
    #: or None. Absent means nobody is there to answer, and the tool says so.
    ask: Callable[[str, list[dict[str, str]]], str | None] | None = None
    on_delta: Callable[[str], None] | None = None
    on_tool_start: Callable[[str, str], None] | None = None
    on_tool_end: Callable[[str, str, bool], None] | None = None
    #: Resume: prior messages, including their system prompt if any.
    initial_messages: list[dict[str, Any]] = field(default_factory=list)
    #: Read AGENTS.md / CLAUDE.md from cwd into the system prompt.
    project_instructions: bool = True
    #: Where the server was started; its AGENTS.md applies to everyone, which
    #: matters once each person works in a directory of their own.
    site_instructions_dir: str | None = None
    #: Agent name; session events of non-main agents are tagged with it.
    name: str = "main"
    #: Tool names hidden from this agent (e.g. children may not spawn children).
    exclude_tools: set[str] = field(default_factory=set)
    #: auto: compact before the next model call once the prompt passes
    #: `threshold` of the endpoint's context_tokens.
    compaction: dict[str, Any] = field(default_factory=lambda: {"auto": True, "threshold": 0.8})
    #: Describes an image for a model that cannot see: (path, request) -> text.
    describe_image: Callable[[str, str], str] | None = None
    describer_name: str = ""


def render_transcript(messages: list[dict[str, Any]]) -> str:
    """Messages as plain text for the summariser, newest kept when too long."""
    parts: list[str] = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "user":
            parts.append(f"PERSON: {content}")
        elif role == "assistant":
            if content:
                parts.append(f"AGENT: {content}")
            for call in message.get("tool_calls") or []:
                parts.append(f"AGENT CALLS {call.get('name')}: {str(call.get('arguments') or '')[:400]}")
        elif role == "tool":
            parts.append(f"TOOL RESULT: {content[:1500]}")
    text = "\n\n".join(parts)
    return text[-MAX_SUMMARY_SOURCE_CHARS:]


def with_context(text: str, context: str | None) -> str:
    """A user message as the model receives it, with any harness context."""
    return f"{text}\n\n[{context}]" if context else text


class Agent:
    def __init__(self, ctx: Context, options: AgentOptions | None = None) -> None:
        self.ctx = ctx
        self.options = options or AgentOptions()
        self.messages: list[dict[str, Any]] = []
        self.total_usage = Usage()
        self._stop = threading.Event()
        #: The date the model was last told; None means it has not been told.
        self._told_date: Any = None
        self._system_logged = False
        self._repeat: tuple[str, int] = ("", 0)
        self._compact_due = False
        #: Plan mode (see PLAN_CONTEXT): the change tools are hidden until it is switched off.
        self.plan_mode = False
        if self.options.initial_messages:
            self.messages.extend(self.options.initial_messages)
        if not any(message.get("role") == "system" for message in self.messages):
            self.messages.insert(0, {"role": "system", "content": self.build_system_prompt()})

    def build_system_prompt(self) -> str:
        cwd = self.options.cwd or os.getcwd()
        llm = self.ctx.optional("llm")
        system = self.options.system_prompt or default_system_prompt(cwd, getattr(llm, "model", None))
        # Always, even under a custom system prompt: without it the model
        # assumes a date from its training data. Measured 2026-10-14 -- asked
        # to move a notice to "next Friday", granite stated today was
        # 2025-04-21 and wrote that wrong date into the file.
        system = f"{system}\n{current_date_line()}"
        self._told_date = datetime.now().astimezone().date()
        if self.options.system_suffix:
            system = f"{system}\n{self.options.system_suffix}"
        if self.options.project_instructions:
            instructions = load_project_instructions(cwd, self.options.site_instructions_dir)
            if instructions:
                system = f"{system}\n\n{instructions}"
        skills = self.ctx.optional("skills")
        if skills is not None and "skill" not in self.options.exclude_tools:
            index = skills.index(cwd)
            if index:
                system = f"{system}\n\n{index}"
        return system

    def refresh_system_prompt(self) -> None:
        """Rebuild the system prompt (after a model switch, say) and log it again."""
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {"role": "system", "content": self.build_system_prompt()}
            self._system_logged = False

    def compact(self, reason: str = "manual") -> dict[str, Any] | None:
        """Replace everything before the latest request with a model-written summary.

        Returns what happened, or None when there is nothing worth compacting
        (a single request, or only a previous summary before it). The cut is
        always at a person's message, so tool calls and their results are never
        separated. The summary is logged, and resume rebuilds the same history.
        """
        body = self.messages[1:] if self.messages and self.messages[0].get("role") == "system" else self.messages[:]
        head = self.messages[: len(self.messages) - len(body)]
        cut = max((i for i, m in enumerate(body) if m.get("role") == "user"), default=-1)
        middle = body[:cut] if cut > 0 else []
        if len(middle) < 2 or (len(middle) == 2 and str(middle[0].get("content", "")).startswith(SUMMARY_PREFIX)):
            return None
        llm = self.ctx.get("llm")
        request = [{"role": "system", "content": SUMMARY_SYSTEM},
                   {"role": "user", "content": render_transcript(middle)}]
        reply: AssistantTurn = self.ctx.invoke(
            "llm/stream", {"messages": request, "tools": []},
            lambda current: llm.stream(current["messages"], current["tools"]),
        )
        summary = (reply.content or "").strip()
        if not summary:
            return None
        kept = body[cut:]
        self.messages[:] = head + [
            {"role": "user", "content": SUMMARY_PREFIX + summary},
            {"role": "assistant", "content": SUMMARY_ACK},
        ] + kept
        result = {"reason": reason, "replaced": len(middle), "kept": len(kept), "summary": summary}
        session: SessionLog | None = self.ctx.optional("session")
        if session:
            session.append({"type": "compaction", **result, **self._tag()})
        self.ctx.emit("agent/compacted", result)
        return result

    def reset(self) -> None:
        """Forget the conversation, keeping a fresh system prompt."""
        self.messages[:] = [{"role": "system", "content": self.build_system_prompt()}]
        self._system_logged = False
        self._repeat = ("", 0)

    def stop(self) -> None:
        """Ask a running task to stop before its next model call or tool.

        An in-flight model request cannot be interrupted; the loop exits as
        soon as it returns.
        """
        self._stop.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def hidden_tools(self, registry: ToolRegistry) -> set[str]:
        """Tools this agent may not use right now: the configured exclusions, the
        change tools in plan mode, and goal_done when no goal is active."""
        hidden = set(self.options.exclude_tools)
        if self.plan_mode:
            # Subagents too: a child does not inherit plan mode and could change things.
            hidden |= {tool.name for tool in registry.list() if tool.mutating} | {"subagent", "subagent_batch"}
        goal = self.ctx.optional("goal")
        if goal is None or not goal.active:
            hidden.add("goal_done")
        return hidden

    def _image_context(self, task: str, images: list[str], llm: Any) -> str | None:
        if not images or getattr(llm, "vision", False):
            return None
        names = ", ".join(os.path.basename(p) for p in images)
        if self.options.describe_image is None:
            return (f"The person attached {len(images)} image(s) ({names}), but this model cannot see images and no "
                    "image-describing model is configured. Say so instead of guessing what they show.")
        lines = []
        for index, path in enumerate(images, 1):
            try:
                text = self.options.describe_image(path, task)
            except Exception as error:  # noqa: BLE001 - one failed description must not stop the request
                text = f"(could not be described: {error})"
            lines.append(f"Image {index} ({os.path.basename(path)}), as described by {self.options.describer_name or 'a vision model'}:\n{text}")
        return "\n\n".join(lines)

    def run(self, task: str, extra_context: str | None = None, images: list[str] | None = None) -> str:
        self._stop.clear()
        llm = self.ctx.get("llm")
        registry: ToolRegistry = self.ctx.get("tools")
        session: SessionLog | None = self.ctx.optional("session")

        # Everything the model sees is in the log (a rule taken from DeepSeek
        # Harness): the system prompt -- identity, date, AGENTS.md -- included,
        # so a transcript shows what the model was actually told.
        if session and not self._system_logged and self.messages and self.messages[0].get("role") == "system":
            session.append({"type": "context", "source": "system", "content": self.messages[0]["content"], **self._tag()})
        self._system_logged = True
        # Everything the harness adds to a request travels with it as context,
        # logged with the request: a new date (a conversation left open
        # overnight), background jobs that finished, plan mode, goal rounds,
        # @-attached files, and what images show for a model that cannot see.
        parts: list[str] = []
        today = datetime.now().astimezone().date()
        if self._told_date != today:
            parts.append(current_date_line())
            self._told_date = today
        jobs = self.ctx.optional("jobs")
        for job in jobs.finished_unannounced() if jobs is not None and self.options.name == "main" else []:
            parts.append(f"Background job {job.id} ({job.command[:120]}) ended: {job.state}"
                         + (f", exit {job.exit_code}" if job.exit_code is not None else "")
                         + f". Log: {job.log_path}")
        if self.plan_mode:
            parts.append(PLAN_CONTEXT)
        if extra_context:
            parts.append(extra_context)
        image_note = self._image_context(task, list(images or []), llm)
        if image_note:
            parts.append(image_note)
        context = "\n\n".join(parts) if parts else None
        message: dict[str, Any] = {"role": "user", "content": with_context(task, context)}
        if images:
            message["images"] = list(images)
        self.messages.append(message)
        if session:
            event = {"type": "message", "role": "user", "content": task, **self._tag()}
            if context:
                event["context"] = context
            if images:
                event["images"] = list(images)
            session.append(event)
        self.ctx.emit("agent/task-start", {"task": task})

        for turn in range(self.options.max_turns):
            if self._stop.is_set():
                self.ctx.emit("agent/task-end", {"turns": turn, "usage": self.total_usage, "stopped": True})
                return "[stopped by operator]"
            hidden = self.hidden_tools(registry)
            schemas = [
                {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                for tool in registry.list()
                if tool.name not in hidden
            ]
            if self._compact_due:
                self._compact_due = False
                try:
                    self.compact(reason="auto")
                except Exception as error:  # noqa: BLE001 - a failed summary must not end the task
                    print(f"[xharness] compaction failed: {error}", file=sys.stderr)
            reply = self._call_model(llm, schemas, session)
            window = getattr(llm, "context_tokens", None)
            if (window and reply.usage and self.options.compaction.get("auto", True)
                    and reply.usage.prompt_tokens >= float(self.options.compaction.get("threshold", 0.8)) * window):
                self._compact_due = True
            if reply.usage:
                self.total_usage.prompt_tokens += reply.usage.prompt_tokens
                self.total_usage.completion_tokens += reply.usage.completion_tokens

            assistant: dict[str, Any] = {"role": "assistant", "content": reply.content}
            if reply.tool_calls:
                assistant["tool_calls"] = reply.tool_calls
            self.messages.append(assistant)
            if session:
                session.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": reply.content,
                        "tool_calls": reply.tool_calls or None,
                        "usage": reply.usage.__dict__ if reply.usage else None,
                        **self._tag(),
                    }
                )

            if not reply.tool_calls:
                self.ctx.emit("agent/task-end", {"turns": turn + 1, "usage": self.total_usage})
                return reply.content

            for call in reply.tool_calls:
                if self._stop.is_set():
                    result = ToolResult("skipped: stopped by operator", is_error=True)
                else:
                    result = self._execute_call(registry, call)
                    note = self._repeat_note(call)
                    if note:
                        result = ToolResult(f"{result.output}\n\n{note}", is_error=result.is_error)
                self.messages.append(
                    {"role": "tool", "content": result.output, "tool_call_id": call["id"]}
                )
                if session:
                    session.append(
                        {
                            "type": "tool-result",
                            "call_id": call["id"],
                            "name": call["name"],
                            "output": result.output,
                            "is_error": result.is_error,
                            **self._tag(),
                        }
                    )

        self.ctx.emit(
            "agent/task-end",
            {"turns": self.options.max_turns, "usage": self.total_usage, "stopped": True},
        )
        return "[stopped: max turns reached]"

    def _call_model(self, llm: Any, schemas: list[dict[str, Any]], session: SessionLog | None) -> AssistantTurn:
        def send() -> AssistantTurn:
            return self.ctx.invoke(
                "llm/stream",
                {"messages": self.messages, "tools": schemas},
                lambda current: llm.stream(current["messages"], current["tools"], on_delta=self.options.on_delta),
            )

        try:
            return send()
        except RuntimeError as error:
            if not is_context_overflow(error):
                raise
            try:
                if self.compact(reason="overflow"):
                    return send()
            except RuntimeError as again:
                if not is_context_overflow(again):
                    raise
            pruned = self._prune_tool_results()
            if not pruned:
                raise
            if session:
                session.append({"type": "context-pruned", "tool_results": pruned, **self._tag()})
            print(f"[xharness] context full; removed {pruned} older tool results and retried", file=sys.stderr)
            return send()

    def _prune_tool_results(self) -> int:
        """Blank all but the latest tool results. Returns how many changed.

        Deterministic, so a resumed session that overflows again prunes the
        same way; the log keeps the originals.
        """
        results = [message for message in self.messages if message.get("role") == "tool"]
        changed = 0
        for message in results[:-KEEP_RECENT_TOOL_RESULTS] if KEEP_RECENT_TOOL_RESULTS else results:
            if message.get("content") != PRUNED_OUTPUT:
                message["content"] = PRUNED_OUTPUT
                changed += 1
        return changed

    def _repeat_note(self, call: dict[str, str]) -> str | None:
        try:
            canonical = json.dumps(json.loads(call["arguments"] or "{}"), sort_keys=True, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            canonical = call["arguments"] or ""
        key = f"{call['name']}\0{canonical}"
        count = self._repeat[1] + 1 if self._repeat[0] == key else 1
        self._repeat = (key, count)
        if count not in REPEAT_WARNINGS:
            return None
        return (f"[note: this is call {count} in a row to {call['name']} with the same arguments, and the "
                "result will not change. Try something different, or ask the person if you are stuck.]")

    def _tag(self) -> dict[str, str]:
        return {"agent": self.options.name} if self.options.name != "main" else {}

    def _execute_call(self, registry: ToolRegistry, call: dict[str, str]) -> ToolResult:
        tool = registry.get(call["name"])
        if tool is None or call["name"] in self.options.exclude_tools:
            return ToolResult(f"unknown tool: {call['name']}", is_error=True)
        if call["name"] in self.hidden_tools(registry):
            reason = "plan mode is on: nothing may be changed until the plan is approved" if self.plan_mode else "not available now"
            return ToolResult(f"{call['name']} is {reason}", is_error=True)
        try:
            args = json.loads(call["arguments"]) if call["arguments"] else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must decode to an object")
        except (json.JSONDecodeError, ValueError):
            return ToolResult("tool arguments are not valid JSON", is_error=True)

        def approve(summary: str) -> bool:
            if self.options.approval_mode == "auto":
                return True
            if self.options.prompt is None:
                return False
            return self.options.prompt(summary)

        tool_ctx = ToolContext(
            cwd=self.options.cwd or os.getcwd(),
            approve=approve,
            agent=self.options.name,
            ask=self.options.ask,
        )
        if self.options.on_tool_start:
            self.options.on_tool_start(call["name"], call["arguments"])
        try:
            result = tool.execute(args, tool_ctx)
        except Exception as error:  # noqa: BLE001 - a broken tool must not kill the loop
            result = ToolResult(f"tool failed: {error}", is_error=True)
        result = ToolResult(spill_output(result.output, tool_ctx.cwd), is_error=result.is_error)
        if self.options.on_tool_end:
            self.options.on_tool_end(call["name"], result.output, result.is_error)
        return result
