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
    describer = str((getattr(config, "vision", None) or {}).get("provider") or "")
    for name, entry in (getattr(config, "providers", {}) or {}).items():
        if allowed and name not in allowed:
            continue
        if name == describer:
            continue   # it describes images for the others; nobody should chat with it
        if not entry.get("model") or not entry.get("base_url"):
            continue
        rows.append({
            "name": name,
            "model": entry["model"],
            "hosting": hosting_of(entry.get("base_url")),
            "endpoint": endpoint_host(entry.get("base_url")),
            "context_tokens": entry.get("context_tokens"),
            "vision": bool(entry.get("vision")),
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


AGENT_WORDS = {"running": "進行中", "done": "完成", "failed": "失敗", "stopped": "已停止"}
KIND_WORDS = {"subagent": "子代理", "workflow": "工作流程步驟", "discussion": "討論參與者"}


def _agents(host: CommandHost, args: str) -> CommandResult:
    store = host.ctx.optional("roles")
    live = host.ctx.optional("agents")
    if args.strip() == "stop":
        count = live.stop_all() if live is not None else 0
        return CommandResult(f"已要求 {count} 個代理停止。" if count else "沒有進行中的代理。")
    cwd = getattr(host.agent.options, "cwd", None) or os.getcwd()
    roles = store.discover(cwd) if store else []
    working = live.public() if live is not None else []
    lines = []
    if working:
        lines.append("這段對話的代理：")
        lines += [f"{a['name']}（{KIND_WORDS.get(a['kind'], a['kind'])}{'・' + a['role'] if a['role'] else ''}）"
                  f"　{AGENT_WORDS.get(a['status'], a['status'])}・{a['tokens']:,} token・{a['calls']} 次呼叫"
                  + (f"・{a['activity']}" if a["status"] == "running" and a["activity"] else "") for a in working]
        lines.append("")
    if roles:
        lines.append("可以指派的代理角色（請模型「交給 名稱」或「讓 A、B 辯論」）：")
        lines += [f"{r.name}（{r.source}）　{r.description}"
                  + (f"　工具：{'、'.join(r.tools)}" if r.tools else "") + (f"　模型：{r.model}" if r.model else "")
                  for r in roles]
    else:
        lines.append("目前沒有代理角色。在工作目錄的 .xharness/agents/ 放一個 名稱.md（開頭寫 name、description、"
                     "tools、model），就會出現；Claude Code 的 .claude/agents/ 也讀得到。")
    if working and any(a["status"] == "running" for a in working):
        lines.append("\n用 /agents stop 停止這段對話所有進行中的代理。")
    return CommandResult("\n".join(lines), data={"roles": [r.public() for r in roles], "agents": working})


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


def _plan(host: CommandHost, args: str) -> CommandResult:
    word = args.strip().lower()
    if word in ("on", "開"):
        on = True
    elif word in ("off", "關"):
        on = False
    elif not word:
        on = not getattr(host.agent, "plan_mode", False)
    else:
        return CommandResult("用法：/plan、/plan on 或 /plan off", ok=False)
    host.agent.plan_mode = on
    session = host.ctx.optional("session")
    if session:
        session.append({"type": "plan-mode", "on": on})
    text = ("計畫模式開啟：模型只會查看、提出計畫，不會改任何東西；你核准後再執行。" if on
            else "計畫模式關閉：模型可以照計畫動手（有副作用的動作仍會先問你）。")
    return CommandResult(text, data={"plan_mode": on})


def _goal(host: CommandHost, args: str) -> CommandResult:
    from .goal import DEFAULT_ROUNDS, MAX_ROUNDS

    text = args.strip()
    state = host.ctx.optional("goal")
    if not text:
        if state is None or not state.objective:
            return CommandResult("用法：/goal 目標（預設最多 5 輪），或 /goal --rounds 8 目標")
        return CommandResult(f"目標：{state.objective}（第 {state.round}/{state.max_rounds} 輪，狀態 {state.status or '未開始'}）"
                             + (f"\n{state.summary}" if state.summary else ""))
    if host.running:
        return CommandResult("任務還在進行，等它結束再設目標。", ok=False)
    # The round count is explicit: a leading number would swallow goals like
    # "2026 年的報表" or "3 個檔案要改".
    rounds = DEFAULT_ROUNDS
    match = re.match(r"^--rounds\s+(\d+)\s+(.+)$", text, flags=re.DOTALL)
    if match:
        rounds, text = max(1, min(int(match.group(1)), MAX_ROUNDS)), match.group(2).strip()
    return CommandResult(f"開始目標模式（最多 {rounds} 輪）：{text}", action="goal",
                         data={"objective": text, "rounds": rounds})


def _jobs(host: CommandHost, _args: str) -> CommandResult:
    manager = host.ctx.optional("jobs")
    jobs = list(manager.jobs.values()) if manager else []
    if not jobs:
        return CommandResult("沒有背景工作。模型用 job_start 啟動的長指令會列在這裡。")
    words = {"running": "執行中", "done": "完成", "failed": "失敗", "stopped": "已停止", "timeout": "逾時被停止"}
    return CommandResult("\n".join(f"{j.id}　{words.get(j.state, j.state)}　{j.runtime()} 秒　{j.command[:80]}" for j in jobs),
                         data={"jobs": [j.public() for j in jobs]})


def _remind(host: CommandHost, args: str) -> CommandResult:
    import time as _time

    scheduler = host.ctx.optional("schedule")
    if scheduler is None or not scheduler.deliverable:
        return CommandResult("提醒只在網頁介面開著這段對話時才會送出，這裡無法使用。", ok=False)
    minutes, _, message = args.strip().partition(" ")
    try:
        value = float(minutes)
    except ValueError:
        value = 0
    if value < 1 or not message.strip():
        if not args.strip():
            pending = scheduler.pending()
            if not pending:
                return CommandResult("沒有待送的提醒。用法：/remind 30 檢查訓練進度")
            return CommandResult("\n".join(f"{r.id}　{r.public()['due_local']}　{r.message}" for r in pending))
        return CommandResult("用法：/remind 分鐘 要做的事，例如 /remind 30 檢查訓練進度", ok=False)
    try:
        reminder = scheduler.add(message.strip()[:1000], _time.time() + value * 60)
    except ValueError as error:
        return CommandResult(str(error), ok=False)
    return CommandResult(f"好，{reminder.public()['due_local']} 會把「{reminder.message}」交給模型（{reminder.id}）。")


def _hooks(host: CommandHost, _args: str) -> CommandResult:
    runner = host.ctx.optional("hooks")
    if not runner:
        return CommandResult("沒有設定 hooks。在設定檔的 [hooks] 加上 Claude Code 的 settings 檔或 [[hooks.PreToolUse]]。")
    lines = [f"{h.event}　{h.matcher or '*'}　（{'設定檔' if h.source == 'config' else os.path.basename(h.source)}）" for h in runner.hooks]
    if runner.notify_argv:
        lines.append("notify　每個任務結束後通知")
    return CommandResult("\n".join(lines))


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
    Command("agents", "[stop]", "列出代理角色與這段對話的代理；stop 停止全部", _agents),
    Command("plan", "[on|off]", "計畫模式：先看、提計畫，你核准後才動手", _plan),
    Command("goal", "[--rounds N] 目標", "目標模式：一輪接一輪做到完成或卡住為止", _goal),
    Command("jobs", "", "列出背景工作", _jobs),
    Command("remind", "分鐘 內容", "幾分鐘後把一件事交給模型（網頁介面）", _remind, surfaces=("web",)),
    Command("hooks", "", "列出設定的 hooks", _hooks),
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
