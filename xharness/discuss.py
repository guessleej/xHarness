"""Discussions: several agents talk to each other about one topic.

Four ways to run one:
  roundtable  everyone speaks in turn, round after round; it ends early when
              a whole round has nothing new to add
  debate      the sides argue in turn; the moderator then judges
  review      the first participant writes, the others review; it repeats
              until every reviewer approves or the rounds run out
  free        the moderator decides who speaks next, and when to stop

Each participant is an agent of its own -- own conversation, instructions (a
role from roles.py, or a brief), tools and model -- so it can look things up
before it speaks. Between turns it is told only what was said since it last
spoke. The moderator is a plain model call with no tools.

What keeps a discussion accountable: every utterance is logged with its
speaker, round, and the tools the speaker used (`discussion-turn` events);
the room stops at a round limit and a token allowance; the closing summary
must keep disagreements and say who held which position; and the whole
transcript is in the session log and the conversation export.

Figures are checked, not trusted. Every number a participant states is looked
for in what the room actually has: the topic, the caller's background, the
participants' instructions, and every tool result so far. A participant that
cites numbers nobody looked up, without using a tool, is told which ones and
asked once to check and speak again; whatever is still unsourced is marked on
the utterance, pointed out to the next speakers, and may not appear in the
summary as agreed. Measured 2026-10-21 on gemma-4-31b-xvita: without this, both
sides of a debate quoted registration rates that were not in the data file in
front of them, and the summary listed an invented figure as consensus.

Named sources get the same treatment: a citation ("資料來源：…", "出自「…」") that is
not the background, a file a participant read, or text some tool returned is
marked as an unverified source. Measured the same day: one speaker cited a
"六年制電資系研討會" that exists nowhere, and the next two speakers repeated it.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .agent import Agent, AgentOptions
from .context import Context, Plugin
from .roles import Role, exclusions
from .subagent import CHILD_TOOLS, find_role
from .telemetry import BudgetExceeded
from .tools import Tool, ToolContext, ToolResult, register_tools

MODES = ("roundtable", "debate", "review", "free")
MODE_NAMES = {"roundtable": "圓桌討論", "debate": "辯論", "review": "審查", "free": "自由討論"}
MAX_PARTICIPANTS = 6
DEFAULT_ROUNDS = 3
MAX_ROUNDS = 8
DEFAULT_MAX_TOKENS = 80_000
SPEAK_TURNS = 6               # model calls a participant may use for one utterance (tool lookups included)
MAX_UTTERANCE_CHARS = 4_000
MAX_OUTPUT = 20_000
PASS_RE = re.compile(r"【\s*無補充\s*】|\[\s*PASS\s*\]", re.IGNORECASE)
APPROVE_RE = re.compile(r"【\s*通過\s*】|\[\s*APPROVED?\s*\]", re.IGNORECASE)
NAME_RE = re.compile(r"^[^\n\r\t\[\]]{1,40}$")
STANCES = ("正方", "反方")
#: A comma is a thousands separator only before exactly three digits; "110,95.0" is two CSV cells.
NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _values(text: str) -> set[float]:
    found = set()
    for raw in NUM_RE.findall(text or ""):
        found.add(float(raw.replace(",", "")))
        if "," in raw:     # "110,950" in a CSV is two numbers as often as one
            found |= {float(part) for part in raw.split(",") if part}
    return found


CITE_RE = re.compile(r"(?:資料來源|來源|出處)\s*[：:]\s*([^）)\n]{1,80})"
                     r"|(?:出自|根據|依據|參考|引用|參閱)\s*(?:表格|報告|文件|資料|會議|欄位|檔案)?\s*[「《『]([^」》』]{1,60})[」》』]")
CITE_SPLIT = re.compile(r"[、，,；;/]|與|及|和")
#: Citations that point at what the room was given rather than naming a document.
GENERIC_CITES = ("背景", "資料檔", "工具", "查詢", "紀錄", "上述", "前述", "對方", "發言", "題目", "討論")
CITE_TRIM = re.compile(r"^(?:表格|報告|文件|檔案|資料)|(?:欄位|欄|表格|表|資料|檔案|檔)$")


def uncited(text: str, corpus: str, files: set[str]) -> list[str]:
    """Named sources in `text` that are none of: the background, a file read, or text a tool returned."""
    missing: list[str] = []
    for whole, quoted in CITE_RE.findall(text or ""):
        for part in ([quoted] if quoted else CITE_SPLIT.split(whole)):
            name = part.strip().strip("「」『』\"'()（）。. ")
            if len(name) < 2 or any(word in name for word in GENERIC_CITES):
                continue
            if any(f and f in name for f in files):
                continue
            core = CITE_TRIM.sub("", name) or name
            if name in corpus or core in corpus:
                continue
            if name not in missing:
                missing.append(name)
    return missing


def unsourced(text: str, sources: set[float]) -> list[str]:
    """Numbers in `text` found nowhere in `sources`. Integers below 10 (ordinals,
    small counts) are not checked; a source matches when it rounds to the figure
    as written (73.33 backs 73.3)."""
    missing: list[str] = []
    for shown in NUM_RE.findall(text or ""):
        clean = shown.replace(",", "")
        value = float(clean)
        places = len(clean.split(".")[1]) if "." in clean else 0
        if places == 0 and value < 10:
            continue
        if any(abs(round(source, places) - value) < 1e-9 for source in sources):
            continue
        if shown not in missing:
            missing.append(shown)
    return missing

MODE_RULES = {
    "roundtable": "If you have nothing new to add, reply with exactly 【無補充】 and nothing else.",
    "debate": "Argue for your side. Answer the other side's strongest point directly; do not change sides.",
    "review": "",   # set per seat: the author revises, reviewers review
    "free": "If you have nothing new to add, reply with exactly 【無補充】 and nothing else.",
}
AUTHOR_RULE = ("You are the author. In your first turn write the piece the topic asks for; afterwards revise it, "
               "address every reviewer comment (accept it and change, or explain why not), and give the full revised version.")
REVIEWER_RULE = ("You are a reviewer. List concrete problems with the latest version, each with what to change. "
                 "If nothing needs changing, reply with exactly 【通過】.")


@dataclass
class Seat:
    name: str
    role: Role | None
    brief: str
    stance: str
    agent: Agent | None = None
    live_id: str = ""
    seen: int = 0           # transcript entries this seat has been told about
    left: str = ""          # why it stopped taking part
    tools_used: list[str] = field(default_factory=list)
    can_look: bool = False  # has any tool to check facts with
    harvested: int = 0      # messages already scanned for tool results


def _seat_prompt(seat: Seat, seats: list[Seat], topic: str, mode: str, context: str, author: bool) -> str:
    others = "、".join(s.name + (f"（{s.stance}）" if s.stance else "") for s in seats if s is not seat)
    rule = (AUTHOR_RULE if author else REVIEWER_RULE) if mode == "review" else MODE_RULES[mode]
    lines = [
        f"You are {seat.name}, one participant in a {MODE_NAMES[mode]} ({mode}) with: {others}.",
        f"Topic: {topic}",
    ]
    if seat.stance:
        lines.append(f"Your side: {seat.stance}.")
    if seat.role is not None and seat.role.prompt:
        lines += ["", f"Your role ({seat.role.name}):", seat.role.prompt]
    if seat.brief:
        lines += ["", "Your brief:", seat.brief]
    lines += [
        "",
        "Rules:",
        "- Speak for yourself only. Respond to what the others said: agree, disagree with reasons, or add something new.",
        "- Check facts with your tools before you state them, and say where each fact came from. Never invent figures or sources.",
        "- Cite only what is in front of you: a file you read, a tool result, or the background you were given. "
        "Never name a report, table, meeting or document you have not read.",
        "- Keep each turn short: a few sentences or a short list. Do not repeat what you already said.",
        f"- {rule}" if rule else "",
        "- Write in the language of the topic, in the same script (Traditional Chinese for a Traditional Chinese topic).",
    ]
    if context:
        lines += ["", "Background from the person who started the discussion:", context]
    return "\n".join(line for line in lines if line is not None)


def _line(entry: dict[str, Any]) -> str:
    flags = []
    if entry.get("unverified"):
        flags.append(f"{'、'.join(entry['unverified'])} 在查到的資料裡找不到原文")
    if entry.get("uncited"):
        flags.append(f"引用的「{'」「'.join(entry['uncited'])}」不在任何查到的資料裡")
    flag = f"（主持人註記：{'；'.join(flags)}）" if flags else ""
    return f"[第 {entry['round']} 輪] {entry['speaker']}：{entry['text']}{flag}"


class Room:
    def __init__(self, ctx: Context, tool_ctx: ToolContext, topic: str, mode: str, seats: list[Seat],
                 rounds: int, max_tokens: int, context: str, speak_turns: int) -> None:
        self.ctx, self.tool_ctx = ctx, tool_ctx
        self.id = secrets.token_hex(3)
        self.topic, self.mode, self.seats = topic, mode, seats
        self.rounds, self.max_tokens, self.context, self.speak_turns = rounds, max_tokens, context, speak_turns
        self.transcript: list[dict[str, Any]] = []
        self.moderator_tokens = 0
        self.ended = ""
        self.live = ctx.optional("agents")
        self.session = ctx.optional("session")
        self._approve_lock = threading.Lock()
        #: Numbers the room can stand on: what it was given and what its tools returned.
        self.sources: set[float] = _values(topic) | _values(context)
        for seat in seats:
            self.sources |= _values(seat.brief) | _values(seat.role.prompt if seat.role else "")
        self.corpus = "\n".join([topic, context] + [s.brief for s in seats])
        self.files: set[str] = set()

    # -- bookkeeping -----------------------------------------------------------
    def tokens(self) -> int:
        used = self.moderator_tokens
        for seat in self.seats:
            if seat.agent is not None:
                used += seat.agent.total_usage.prompt_tokens + seat.agent.total_usage.completion_tokens
        return used

    def _log(self, event: dict[str, Any]) -> None:
        if self.session is not None:
            self.session.append(event)

    def _approve(self, seat: Seat, summary: str) -> bool:
        with self._approve_lock:
            return self.tool_ctx.approve(f"[{self.id}/{seat.name}] {summary}")

    def seat_up(self) -> None:
        registry = self.ctx.get("tools")
        roles = self.ctx.optional("roles")
        for index, seat in enumerate(self.seats):
            llm, note = (None, "")
            if seat.role is not None and roles is not None:
                llm, note = roles.resolve_model(seat.role)
            if note:
                self._log({"type": "discussion-note", "room": self.id, "text": note})

            def on_tool(tool: str, _args: str, seat: Seat = seat) -> None:
                seat.tools_used.append(tool)
                if self.live is not None:
                    self.live.activity(seat.live_id, tool)

            hidden = exclusions(registry, seat.role, CHILD_TOOLS)
            seat.can_look = any(tool.name not in hidden for tool in registry.list())
            seat.agent = Agent(self.ctx, AgentOptions(
                name=f"{self.id}/{seat.name}",
                cwd=self.tool_ctx.cwd,
                system_prompt=_seat_prompt(seat, self.seats, self.topic, self.mode, self.context,
                                           author=(self.mode == "review" and index == 0)),
                max_turns=min(seat.role.max_turns or self.speak_turns, self.speak_turns) if seat.role else self.speak_turns,
                approval_mode="prompt",
                prompt=lambda summary, seat=seat: self._approve(seat, summary),
                exclude_tools=hidden,
                project_instructions=False,
                llm=llm,
                max_tokens=seat.role.max_tokens if seat.role else None,
                on_tool_start=on_tool,
            ))
            if self.live is not None:
                seat.live_id = self.live.start(seat.agent, seat.name, seat.role.name if seat.role else "",
                                               "discussion", self.topic, room=self.id)

    # -- one utterance ---------------------------------------------------------
    def speak(self, seat: Seat, round_no: int) -> dict[str, Any] | None:
        if seat.agent is None or seat.left:
            return None
        if self._stop_wanted(seat):
            seat.left = "stopped"
            return None
        if self.tokens() >= self.max_tokens:
            self.ended = self.ended or "budget"
            return None
        news = self.transcript[seat.seen:]
        if seat.seen == 0:
            prompt = ("The discussion so far:\n" + "\n".join(_line(e) for e in news) if news else "You speak first.")
        else:
            prompt = ("Since you last spoke:\n" + "\n".join(_line(e) for e in news)) if news else "Nobody has spoken since your last turn."
        prompt += f"\n\nIt is your turn (round {round_no})."
        seat.seen = len(self.transcript)
        seat.tools_used = []
        if self.live is not None:
            self.live.activity(seat.live_id, f"第 {round_no} 輪發言")
        before = seat.agent.total_usage.prompt_tokens + seat.agent.total_usage.completion_tokens
        text = self._ask(seat, prompt)
        if text is None:
            return None
        missing = unsourced(text, self.sources)
        refs = uncited(text, self.corpus, self.files)
        rechecked = False
        if (missing or refs) and seat.can_look and not seat.tools_used:
            # Figures or sources nobody looked up: one chance to check them before they go on record.
            rechecked = True
            named = "、".join(missing + [f"「{r}」" for r in refs])
            self._log({"type": "discussion-note", "room": self.id,
                       "text": f"{seat.name} 提到 {named} 但沒有查證，請它先查再發言"})
            again = self._ask(seat, f"主持人提醒：你提到的 {named} 在任何查到的資料裡都找不到原文。"
                                    "請先用工具查證，再把這次發言整段重講：新的發言會取代上一段，不要道歉、不要提到上一段，"
                                    "查不到的數字不要寫出來。")
            if again is None:
                return None
            text, missing = again, unsourced(again, self.sources)
            refs = uncited(again, self.corpus, self.files)
        if len(text) > MAX_UTTERANCE_CHARS:
            text = text[:MAX_UTTERANCE_CHARS] + "……（截斷）"
        entry = {"round": round_no, "speaker": seat.name, "role": seat.role.name if seat.role else "",
                 "stance": seat.stance, "text": text or "（沒有發言）", "tools": list(dict.fromkeys(seat.tools_used)),
                 "tokens": seat.agent.total_usage.prompt_tokens + seat.agent.total_usage.completion_tokens - before,
                 "unverified": missing, "uncited": refs, "rechecked": rechecked, "ts": time.time()}
        self.transcript.append(entry)
        seat.seen = len(self.transcript)
        self._log({"type": "discussion-turn", "room": self.id, **{k: v for k, v in entry.items() if k != "ts"}})
        self.ctx.emit("discussion/turn", {"room": self.id, **entry})
        return entry

    def _ask(self, seat: Seat, prompt: str) -> str | None:
        """One run of a participant; None when it stopped, failed, or the room ran out of tokens."""
        runner = seat.agent
        if runner is None:
            return None
        if self._stop_wanted(seat):     # checked before every run, retries included: run() resets the agent's flag
            seat.left = "stopped"
            return None
        try:
            text = runner.run(prompt).strip()
        except BudgetExceeded:
            self.ended = self.ended or "budget"
            return None
        except Exception as error:  # noqa: BLE001 - one participant failing must not end the room
            seat.left = f"failed: {error}"[:200]
            self._log({"type": "discussion-note", "room": self.id, "text": f"{seat.name} left: {seat.left}"})
            return None
        finally:
            for message in runner.messages[seat.harvested:]:
                if message.get("role") == "tool":
                    content = str(message.get("content") or "")
                    self.sources |= _values(content)
                    self.corpus += "\n" + content[:50_000]
                for call in message.get("tool_calls") or []:
                    try:
                        args = json.loads(call.get("arguments") or "{}")
                    except ValueError:
                        continue
                    for key in ("path", "file_path", "pattern", "file"):
                        if isinstance(args, dict) and isinstance(args.get(key), str) and args[key]:
                            self.files.add(args[key].rsplit("/", 1)[-1])
            seat.harvested = len(runner.messages)
        if text.startswith("[stopped"):
            stopped = runner.stop_requested or self._stop_wanted(seat)
            seat.left = "stopped" if stopped else text.strip("[]")
            if not stopped:
                self._log({"type": "discussion-note", "room": self.id, "text": f"{seat.name} left: {seat.left}"})
            return None
        return text

    def _stop_wanted(self, seat: Seat) -> bool:
        if seat.agent is not None and seat.agent.stop_requested:
            return True
        return bool(self.live is not None and seat.live_id and self.live.stopping(seat.live_id))

    def active(self) -> list[Seat]:
        return [s for s in self.seats if not s.left]

    # -- the moderator ---------------------------------------------------------
    def moderator(self, system: str, prompt: str) -> str:
        llm = self.ctx.get("llm")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        reply = self.ctx.invoke("llm/stream", {"messages": messages, "tools": []},
                                lambda current: llm.stream(current["messages"], current["tools"]))
        if reply.usage:
            self.moderator_tokens += reply.usage.prompt_tokens + reply.usage.completion_tokens
        return (reply.content or "").strip()

    def pick_next(self, last: str) -> tuple[Seat | None, bool]:
        """Free mode: (who speaks next, whether the moderator ended it)."""
        seats = self.active()
        if not seats:
            return None, True
        everyone_spoke = {e["speaker"] for e in self.transcript} >= {s.name for s in seats}
        names = [s.name for s in seats]
        try:
            raw = self.moderator(
                "You moderate a discussion. Reply with JSON only.",
                f"Topic: {self.topic}\nParticipants: {', '.join(names)}\n\nTranscript:\n"
                + "\n".join(_line(e) for e in self.transcript[-12:])
                + '\n\nWho should speak next to move the discussion forward? Reply {"next": "<name>"}. '
                  'If the discussion has reached a conclusion or is going in circles, reply {"end": true}.')
        except BudgetExceeded:
            self.ended = self.ended or "budget"
            return None, True
        except Exception:  # noqa: BLE001 - a broken moderator falls back to taking turns
            raw = ""
        match = re.search(r"\{.*\}", raw, re.S)
        try:
            choice = json.loads(match.group()) if match else {}
        except ValueError:
            choice = {}
        if choice.get("end") is True and everyone_spoke:
            return None, True
        wanted = str(choice.get("next") or "")
        pick = next((s for s in seats if s.name == wanted and (s.name != last or len(seats) == 1)), None)
        if pick is None:   # not a participant, or the same speaker again: take turns instead
            order = [s for s in seats if s.name != last] or seats
            spoken = {e["speaker"]: i for i, e in enumerate(self.transcript)}
            pick = min(order, key=lambda s: spoken.get(s.name, -1))
        return pick, False

    # -- the whole room --------------------------------------------------------
    def run(self) -> str:
        self.seat_up()
        self._log({"type": "discussion-start", "room": self.id, "topic": self.topic, "mode": self.mode,
                   "rounds": self.rounds, "max_tokens": self.max_tokens,
                   "participants": [{"name": s.name, "role": s.role.name if s.role else "", "stance": s.stance,
                                     "brief": s.brief[:300]} for s in self.seats]})
        self.ctx.emit("discussion/start", {"room": self.id, "topic": self.topic, "mode": self.mode,
                                           "participants": [s.name for s in self.seats]})
        try:
            self._rounds()
        finally:
            if not self.ended:
                self.ended = "stopped" if not self.active() else "rounds"
        summary = self.close()
        for seat in self.seats:
            if self.live is not None and seat.live_id:
                self.live.end(seat.live_id, ok=not seat.left.startswith("failed"))
        tokens = self.tokens()
        self._log({"type": "discussion-end", "room": self.id, "reason": self.ended, "turns": len(self.transcript),
                   "tokens": tokens, "summary": summary})
        self.ctx.emit("discussion/end", {"room": self.id, "reason": self.ended, "turns": len(self.transcript),
                                         "tokens": tokens, "summary": summary})
        lines = "\n".join(_line(e) for e in self.transcript)
        head = (f"Discussion {self.id} ({MODE_NAMES[self.mode]}): {len(self.transcript)} turns, "
                f"ended because {END_REASONS.get(self.ended, self.ended)}, {tokens:,} tokens.")
        out = f"{head}\n\n## Summary\n{summary}\n\n## Transcript\n{lines}"
        return out[:MAX_OUTPUT] + ("\n[transcript truncated; the full one is in the session log]" if len(out) > MAX_OUTPUT else "")

    def _rounds(self) -> None:
        if self.mode == "free":
            last = ""
            for _ in range(self.rounds * len(self.seats)):
                if self.ended or not self.active():
                    return
                seat, done = self.pick_next(last)
                if done or seat is None:
                    self.ended = self.ended or "moderator"
                    return
                round_no = 1 + sum(1 for e in self.transcript if e["speaker"] == seat.name)
                entry = self.speak(seat, round_no)
                if entry:
                    last = seat.name
            return
        for round_no in range(1, self.rounds + 1):
            said = []
            for index, seat in enumerate(self.seats):
                if self.ended:
                    return
                entry = self.speak(seat, round_no)
                if entry:
                    said.append((index, entry))
            if not self.active():
                return
            if self.mode == "roundtable" and round_no > 1 and said and all(PASS_RE.fullmatch(e["text"].strip()) for _, e in said):
                self.ended = "converged"
                return
            if self.mode == "review":
                verdicts = [e for i, e in said if i > 0]
                if verdicts and all(APPROVE_RE.search(e["text"]) for e in verdicts) \
                        and len(verdicts) == sum(1 for s in self.seats[1:] if not s.left):
                    self.ended = "approved"
                    return

    def close(self) -> str:
        if not self.transcript:
            return "沒有人發言（" + END_REASONS.get(self.ended, self.ended) + "）。"
        extra = {
            "debate": "4. 裁決：依雙方實際提出的論點，判斷哪一方較有說服力、理由為何；若難分高下就直說。",
            "review": "4. 審查結果：是否全數通過、誰仍有意見、還沒處理的問題。",
        }.get(self.mode, "")
        prompt = (f"主題：{self.topic}\n形式：{MODE_NAMES[self.mode]}\n結束原因：{END_REASONS.get(self.ended, self.ended)}\n\n"
                  "逐字紀錄：\n" + "\n".join(_line(e) for e in self.transcript)
                  + "\n\n請用主題的語言寫這場討論的結論，給提出討論的人看。標有「主持人註記」的數字或出處在資料裡找不到原文，"
                    "不可寫成共識或事實，也不要重複那些出處名稱，只能列在待確認：\n"
                    "1. 共識：大家同意的地方。\n2. 分歧：哪裡意見不同、各自是誰、理由為何（點名，不要抹平）。\n"
                    "3. 待確認：沒有人查證過、或還需要補資料的地方。\n" + extra
                  + "\n只根據逐字紀錄，不要加入沒有人說過的事實或論點。")
        try:
            text = self.moderator("You write the closing summary of a discussion. Be faithful to the transcript.", prompt)
        except Exception:  # noqa: BLE001 - the transcript still stands without a summary
            text = ""
        if text:
            missing = unsourced(text, self.sources)
            if missing:
                text += f"\n\n（主持人註記：結論中的 {'、'.join(missing)} 在查到的資料裡找不到原文，使用前請查證。）"
            return text
        last: dict[str, str] = {}
        for entry in self.transcript:
            last[entry["speaker"]] = entry["text"]
        return "（主持人沒有寫出結論，以下為每位參與者最後的發言）\n" + "\n".join(f"- {k}：{v[:300]}" for k, v in last.items())


END_REASONS = {"rounds": "輪數用完", "converged": "整輪都沒有新意見", "approved": "審查者全數通過",
               "moderator": "主持人判斷已有結論", "budget": "token 額度用完", "stopped": "被停止"}


#: Appended to every argument error: local models recover from a malformed call far
#: more often when shown the smallest valid one (measured on gemma-4-31b-xvita, which
#: broke the nested participant objects in one run out of three).
EXAMPLE = ('Simplest valid call: {"topic": "要不要調降招生名額", "mode": "debate", '
           '"participants": ["正方", "反方"], "rounds": 2}')


def parse_participants(ctx: Context, items: Any, mode: str, cwd: str) -> tuple[list[Seat], str]:
    if isinstance(items, str):
        items = [part.strip() for part in re.split(r"[,，、]", items) if part.strip()]
    if not isinstance(items, list) or len(items) < 2:
        return [], "participants needs at least two entries"
    if len(items) > MAX_PARTICIPANTS:
        return [], f"at most {MAX_PARTICIPANTS} participants"
    seats: list[Seat] = []
    roles = ctx.optional("roles")
    for item in items:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            return [], "each participant is a name or an object with name, agent, brief, stance"
        role_name = str(item.get("agent") or "").strip()
        name = str(item.get("name") or role_name).strip()
        if not NAME_RE.match(name):
            return [], f"participant name {name!r} is empty, too long, or has brackets or line breaks"
        if any(s.name == name for s in seats):
            return [], f"two participants are both called {name!r}"
        if not role_name and roles is not None and roles.get(name, cwd) is not None:
            role_name = name   # "reviewer" as a participant means the reviewer role
        role, problem = find_role(ctx, role_name or None, cwd)
        if problem:
            return [], problem
        seats.append(Seat(name, role, str(item.get("brief") or "")[:2000], str(item.get("stance") or "")[:80]))
    if mode == "debate":
        for index, seat in enumerate(seats):
            seat.stance = seat.stance or STANCES[index % 2]
        if len({s.stance for s in seats}) < 2:
            return [], "a debate needs at least two different stances"
    return seats, ""


def discussion_plugin(config: dict[str, Any] | None = None) -> Plugin:
    settings = config or {}
    max_rounds = int(settings.get("max_rounds", MAX_ROUNDS))
    max_tokens = int(settings.get("max_tokens", DEFAULT_MAX_TOKENS))
    speak_turns = int(settings.get("speak_turns", SPEAK_TURNS))

    def apply(ctx: Context, _config: Any) -> None:
        def run(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            def refuse(problem: str) -> ToolResult:
                return ToolResult(f"{problem}. {EXAMPLE}", is_error=True)

            topic = str(args.get("topic") or "").strip()
            if not topic:
                return refuse("topic is required")
            mode = str(args.get("mode") or "roundtable").strip().lower()
            if mode not in MODES:
                return refuse(f"mode must be one of {', '.join(MODES)}")
            seats, problem = parse_participants(ctx, args.get("participants"), mode, tool_ctx.cwd)
            if problem:
                return refuse(problem)
            try:
                rounds = int(args.get("rounds") or DEFAULT_ROUNDS)
            except (TypeError, ValueError):
                return refuse("rounds must be a number")
            room = Room(ctx, tool_ctx, topic[:500], mode, seats, max(1, min(rounds, max_rounds)),
                        max_tokens, str(args.get("context") or "")[:6000], speak_turns)
            return ToolResult(room.run())

        register_tools(ctx, [Tool(
            name="discussion",
            description=(
                "Have several agents discuss a topic with each other and return the transcript and a summary "
                "that keeps their disagreements. Modes: roundtable (everyone in turn), debate (sides argue, then a "
                "verdict), review (the first participant writes, the others review until they approve), free (a "
                "moderator picks who speaks). Each participant is an agent with its own tools. Simplest form: "
                'participants = ["正方", "反方"] (agent role names or plain names); use objects with '
                "name / agent / brief / stance only when a participant needs more. Use it when the person wants "
                f"several viewpoints, an argument tested, or a draft reviewed. 2-{MAX_PARTICIPANTS} participants, "
                f"up to {max_rounds} rounds."),
            parameters={"type": "object", "properties": {
                "topic": {"type": "string", "description": "The question or task, as the person put it"},
                "mode": {"type": "string", "enum": list(MODES)},
                # Plain types only: llama.cpp's Gemma template fails the whole request on a
                # type list such as ["string", "object"] (500 "filter-mapping not implemented").
                "participants": {"type": "array", "description": 'Names, e.g. ["正方", "反方"], or objects',
                                 "items": {"type": "object", "properties": {
                    "name": {"type": "string"},
                    "agent": {"type": "string", "description": "Agent role to run as (see Agent roles)"},
                    "brief": {"type": "string", "description": "Viewpoint or expertise to argue from"},
                    "stance": {"type": "string", "description": "Debate side, e.g. 正方 / 反方"}},
                    "required": ["name"]}},
                "rounds": {"type": "integer", "description": f"1-{max_rounds}, default {DEFAULT_ROUNDS}"},
                "context": {"type": "string", "description": "Background every participant gets"},
            }, "required": ["topic", "participants"]},
            execute=run,
            mutating=True,
        )])

    return Plugin("discussion", apply)
