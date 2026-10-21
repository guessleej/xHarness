"""Human commands: `/compact`, `/model`, `/skills` and the rest.

A command is typed by a person and handled by the harness; it never becomes
a message to the model (DeepSeek Harness draws the same line). The terminal
and the Web UI dispatch through this one registry, so a command behaves the
same everywhere and the help text cannot drift between them.

Commands that change the conversation (compact, model) are refused while a
task is running: the model is mid-turn and the history is not settled.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm import OpenAIAdapter, endpoint_host, hosting_of
from .session import clean_title

FEEDBACK_RATINGS = {"up": "up", "down": "down", "好": "up", "讚": "up", "不好": "down", "差": "down"}
MAX_FEEDBACK_NOTE = 500

TITLE_SYSTEM = (
    "Write a short title for the request below: at most 16 Chinese characters or 6 English words, in the "
    "same language and script as the request (Traditional Chinese for Traditional Chinese). Output only "
    "the title, no quotes, no punctuation at the end."
)


@dataclass
class CommandResult:
    text: str
    ok: bool = True
    #: What the front end should do next: "new" (start a fresh conversation),
    #: "stop", "export". The terminal handles them itself.
    action: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandHost:
    """What a command may touch. Front ends fill in what they have."""

    agent: Any
    ctx: Any
    config: Any
    running: bool = False
    #: Providers this person may switch to; empty means every configured one.
    allowed_providers: tuple[str, ...] = ()
    model_switch: bool = True
    set_title: Callable[[str], None] | None = None
    get_title: Callable[[], str] | None = None
    rate: Callable[[str, str], int | None] | None = None   # (rating, note) -> turn rated
    surface: str = "web"


@dataclass(frozen=True)
class Command:
    name: str
    args: str
    help: str
    handler: Callable[[CommandHost, str], CommandResult]
    surfaces: tuple[str, ...] = ("web", "cli")


def providers_for(config: Any, allowed: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Configured providers this person may use, without keys or full URLs."""
    rows = []
    for name, entry in (getattr(config, "providers", {}) or {}).items():
        if allowed and name not in allowed:
            continue
        if not entry.get("model") or not entry.get("base_url"):
            continue
        rows.append({
            "name": name,
            "model": entry["model"],
            "hosting": hosting_of(entry.get("base_url")),
            "endpoint": endpoint_host(entry.get("base_url")),
            "context_tokens": entry.get("context_tokens"),
        })
    return rows


def switch_model(host: CommandHost, name: str) -> CommandResult:
    if not host.model_switch:
        return CommandResult("這台伺服器沒有開放切換模型（[web] model_switch = false）。", ok=False)
    choices = {row["name"]: row for row in providers_for(host.config, host.allowed_providers)}
    if name not in choices:
        known = "、".join(choices) or "（沒有）"
        return CommandResult(f"沒有這個模型設定：{name}。可以用的有：{known}", ok=False)
    entry = host.config.providers[name]
    adapter = OpenAIAdapter(**entry)
    host.ctx.replace("llm", adapter)
    host.agent.refresh_system_prompt()   # the identity line names the model
    session = host.ctx.optional("session")
    if session:
        session.append({"type": "model-switch", "provider": name, "model": adapter.model,
                        "endpoint": endpoint_host(adapter.base_url), "hosting": hosting_of(adapter.base_url)})
    return CommandResult(f"已切換到 {name}（{adapter.model}），接下來的回覆由它處理。",
                         data={"provider": name, "model": adapter.model})


def generate_title(ctx: Any, request: str) -> str:
    """A short title for a conversation, written by its own model."""
    llm = ctx.get("llm")
    messages = [{"role": "system", "content": TITLE_SYSTEM}, {"role": "user", "content": request[:2000]}]
    reply = ctx.invoke("llm/stream", {"messages": messages, "tools": []},
                       lambda current: llm.stream(current["messages"], current["tools"]))
    return clean_title(reply.content or "")


# --- handlers -----------------------------------------------------------------

def _help(host: CommandHost, _args: str) -> CommandResult:
    lines = [f"/{c.name}{(' ' + c.args) if c.args else ''}　{c.help}" for c in COMMANDS.values()
             if host.surface in c.surfaces]
    return CommandResult("可以用的指令：\n" + "\n".join(lines))


def _compact(host: CommandHost, _args: str) -> CommandResult:
    if host.running:
        return CommandResult("任務還在進行，等它結束再壓縮。", ok=False)
    result = host.agent.compact(reason="command")
    if not result:
        return CommandResult("對話還很短，不需要壓縮。")
    return CommandResult(f"已把較早的 {result['replaced']} 則訊息整理成摘要，保留最近 {result['kept']} 則。",
                         data={"summary": result["summary"]})


def _model(host: CommandHost, args: str) -> CommandResult:
    name = args.strip()
    if not name:
        current = getattr(host.ctx.optional("llm"), "model", "")
        rows = providers_for(host.config, host.allowed_providers)
        if not rows:
            return CommandResult(f"目前模型：{current}。設定檔裡沒有其他可切換的模型。")
        lines = [f"{'＊' if r['model'] == current else '　'}{r['name']}　{r['model']}（{'地端' if r['hosting'] == 'self-hosted' else '外部'}）"
                 for r in rows]
        return CommandResult("目前模型：" + current + "\n" + "\n".join(lines) + "\n用 /model 名稱 切換。")
    if host.running:
        return CommandResult("任務還在進行，等它結束再換模型。", ok=False)
    return switch_model(host, name)


def _skills(host: CommandHost, _args: str) -> CommandResult:
    store = host.ctx.optional("skills")
    cwd = getattr(host.agent.options, "cwd", None) or os.getcwd()
    skills = store.discover(cwd) if store else []
    if not skills:
        return CommandResult("目前沒有技能。把含 SKILL.md 的資料夾放進工作目錄的 .xharness/skills/ 就會出現。")
    return CommandResult("可以用的技能（模型會在任務符合時自己載入）：\n"
                         + "\n".join(f"{s.name}（{s.source}）　{s.description}" for s in skills),
                         data={"skills": [{"name": s.name, "description": s.description, "source": s.source} for s in skills]})


def _title(host: CommandHost, args: str) -> CommandResult:
    text = clean_title(args)
    if not text:
        current = host.get_title() if host.get_title else ""
        return CommandResult(f"目前標題：{current or '（尚未命名）'}。用 /title 新標題 修改。")
    if host.set_title:
        host.set_title(text)
    return CommandResult(f"標題改為「{text}」。", data={"title": text})


def _tools(host: CommandHost, _args: str) -> CommandResult:
    registry = host.ctx.get("tools")
    lines = [f"{t.name}{'（會改東西，走審批）' if t.mutating else ''}" for t in registry.list()
             if t.name not in getattr(host.agent.options, "exclude_tools", set())]
    return CommandResult(f"{len(lines)} 個工具：\n" + "\n".join(lines))


def _memory(host: CommandHost, _args: str) -> CommandResult:
    store = host.ctx.optional("memory")
    lines = store.index_lines() if store else []
    return CommandResult("\n".join(lines) if lines else "還沒有記憶。")


def _usage(host: CommandHost, _args: str) -> CommandResult:
    telemetry = host.ctx.optional("telemetry")
    if not telemetry:
        return CommandResult("沒有掛上用量統計。")
    r = telemetry.report()
    return CommandResult(f"這段對話：{r['total_tokens']:,} tokens（輸入 {r['prompt_tokens']:,}、輸出 {r['completion_tokens']:,}），"
                         f"{r['calls']} 次模型呼叫、{r['tool_calls']} 個步驟。")


def _feedback(host: CommandHost, args: str) -> CommandResult:
    words = args.strip().split(maxsplit=1)
    rating = FEEDBACK_RATINGS.get(words[0].lower()) if words else None
    if not rating:
        return CommandResult("用法：/feedback up 或 /feedback down，後面可以加一句備註。", ok=False)
    note = words[1][:MAX_FEEDBACK_NOTE] if len(words) > 1 else ""
    turn = host.rate(rating, note) if host.rate else None
    if turn is None:
        return CommandResult("還沒有回覆可以評價。", ok=False)
    return CommandResult(f"已記下對第 {turn} 則回覆的評價，謝謝。")


def _new(_host: CommandHost, _args: str) -> CommandResult:
    return CommandResult("開始新的任務。", action="new")


def _stop(_host: CommandHost, _args: str) -> CommandResult:
    return CommandResult("已要求停止。", action="stop")


def _export(_host: CommandHost, _args: str) -> CommandResult:
    return CommandResult("匯出這段對話。", action="export")


COMMANDS: dict[str, Command] = {c.name: c for c in (
    Command("help", "", "列出所有指令", _help),
    Command("new", "", "開始新的任務（終端機會清空目前對話）", _new),
    Command("compact", "", "把較早的對話整理成摘要，讓長對話可以繼續", _compact),
    Command("model", "[名稱]", "看目前的模型，或切換到設定檔裡的另一個", _model),
    Command("skills", "", "列出可以用的技能", _skills),
    Command("title", "[標題]", "看或修改這段對話的標題", _title),
    Command("feedback", "up|down [備註]", "評價上一則回覆", _feedback),
    Command("export", "", "把這段對話匯出成 Markdown", _export),
    Command("stop", "", "停止進行中的任務", _stop, surfaces=("web",)),
    Command("tools", "", "列出模型可以用的工具", _tools),
    Command("memory", "", "列出記住的事", _memory),
    Command("usage", "", "這段對話用了多少", _usage),
)}


def listing(surface: str) -> list[dict[str, str]]:
    return [{"name": c.name, "args": c.args, "help": c.help} for c in COMMANDS.values() if surface in c.surfaces]


COMMAND_LIKE = re.compile(r"^/[A-Za-z][\w-]*$")


def dispatch(host: CommandHost, line: str, surface: str = "web") -> CommandResult | None:
    """Run `/name args`. None when the line is ordinary text for the model.

    A bare `/word` that is not a command is answered here rather than sent:
    a model handed "/stop" as a request will happily invent work it never did
    (measured: xVITA reported editing two files after receiving "/stop").
    A path such as "/etc/hosts 是什麼" still goes to the model.
    """
    text = (line or "").strip()
    if not text.startswith("/"):
        return None
    name, _, args = text[1:].partition(" ")
    command = COMMANDS.get(name.lower())
    if command is None or surface not in command.surfaces:
        if COMMAND_LIKE.match(text):
            return CommandResult(f"沒有 {text} 這個指令。輸入 /help 看全部。", ok=False)
        return None
    host.surface = surface
    try:
        return command.handler(host, args)
    except Exception as error:  # noqa: BLE001 - a command failure is reported, never fatal
        from .redact import scrub

        return CommandResult(f"/{command.name} 失敗：{scrub(error)}", ok=False)
