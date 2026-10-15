"""ask_user: let the model ask the person a short multiple-choice question
instead of guessing.

The reason this exists is hallucination, not convenience. A model that is
missing a fact -- what a system it has never seen can do, which of two
reasonable readings the person meant -- will otherwise fill the gap with
something plausible, and an invented command or path costs far more than a
question. Giving it a legitimate way to ask makes "I need to know X" a
normal move rather than a failure.

The tool never decides who answers. The agent hands it whatever front end is
attached (Web UI card, terminal prompt, Telegram buttons, a hub forwarding
for a node); with nobody attached -- a headless run, an eval, a subagent --
it says so at once, so the model proceeds on a stated assumption instead of
waiting for a reply that will never come.
"""

from __future__ import annotations

from typing import Any

from ..context import Context, Plugin
from . import Tool, ToolContext, ToolResult, register_tools

MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_QUESTION_CHARS = 500
MAX_LABEL_CHARS = 80
MAX_DESCRIPTION_CHARS = 200
#: An answer is user input headed for the model's context; keep it bounded.
MAX_ANSWER_CHARS = 4000

NOBODY = (
    "No person is attached to this run (unattended, an evaluation, or a sub-task), "
    "so the question was not shown to anyone. Do not wait for a reply. Either proceed "
    "on the most reasonable assumption and state that assumption plainly in your "
    "final answer, or stop and say exactly what you need to know."
)
UNANSWERED = (
    "The person did not answer (the question timed out or the task was stopped). "
    "Do not repeat the question. Proceed on the most reasonable assumption and state "
    "it plainly, or stop and say what you need."
)

DESCRIPTION = (
    "Ask the person a short multiple-choice question and wait for the answer. "
    "Use it when you genuinely cannot proceed well without their input: the request "
    "has two or more reasonable readings that lead to different work; you need a fact "
    "about their environment, preferences or this system that you cannot find with "
    "read, grep or glob; or you are about to do something hard to undo and their "
    "intent is unclear. "
    "If you do not know how something works -- a command, a file path, a feature of "
    "this system -- ask rather than invent it: a made-up answer is worse than a question. "
    "Do not use it for things you can look up yourself, for trivial choices that have "
    "a sensible default, to confirm each step, or to ask permission to run a tool "
    "(approvals for side effects are handled separately). "
    "Give 2 to 4 options with short labels; the person can also type their own answer. "
    "Put the option you recommend first. Ask one question at a time."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The question, complete and specific, ending with a question mark.",
        },
        "options": {
            "type": "array",
            "minItems": MIN_OPTIONS,
            "maxItems": MAX_OPTIONS,
            "description": "2 to 4 distinct choices. The person may also answer in their own words.",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "A few words; what the person picks."},
                    "description": {"type": "string", "description": "Optional: what choosing this means."},
                },
                "required": ["label"],
            },
        },
    },
    "required": ["question", "options"],
}


def clean_options(raw: Any) -> list[dict[str, str]] | str:
    """Validated options, or an error message for the model."""
    if not isinstance(raw, list):
        return "options must be a list"
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):  # tolerate a bare label
            item = {"label": item}
        if not isinstance(item, dict):
            return "each option must be an object with a label"
        label = str(item.get("label") or "").strip()
        if not label:
            return "every option needs a non-empty label"
        if len(label) > MAX_LABEL_CHARS:
            return f"option labels must be at most {MAX_LABEL_CHARS} characters"
        if label in seen:
            return f"duplicate option label: {label}"
        seen.add(label)
        description = str(item.get("description") or "").strip()[:MAX_DESCRIPTION_CHARS]
        options.append({"label": label, "description": description})
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        return f"give between {MIN_OPTIONS} and {MAX_OPTIONS} options, not {len(options)}"
    return options


def resolve_answer(raw: Any, options: list[dict[str, str]]) -> str | None:
    """A front end's raw reply -> what the model is told. A bare number picks
    that option, so "2" in a terminal or a chat means the second choice."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit() and 1 <= int(text) <= len(options):
        return options[int(text) - 1]["label"]
    return text[:MAX_ANSWER_CHARS]


def _ask(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    question = str(args.get("question") or "").strip()
    if not question:
        return ToolResult("question is required", is_error=True)
    if len(question) > MAX_QUESTION_CHARS:
        return ToolResult(f"keep the question under {MAX_QUESTION_CHARS} characters", is_error=True)
    options = clean_options(args.get("options"))
    if isinstance(options, str):
        return ToolResult(options, is_error=True)
    if ctx.ask is None:
        return ToolResult(NOBODY)
    answer = resolve_answer(ctx.ask(question, options), options)
    if answer is None:
        return ToolResult(UNANSWERED)
    chosen = next((o for o in options if o["label"] == answer), None)
    if chosen is not None:
        return ToolResult(f"The person chose: {answer}")
    return ToolResult(f"The person answered in their own words: {answer}")


def ask_user_tool() -> Tool:
    return Tool(name="ask_user", description=DESCRIPTION, parameters=PARAMETERS, execute=_ask)


def _apply(ctx: Context, _config: Any) -> None:
    register_tools(ctx, [ask_user_tool()])


ask_tool_plugin = Plugin("tool-ask", _apply)
