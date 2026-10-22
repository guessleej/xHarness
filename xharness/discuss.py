"""Discussions: several agents talk to each other about one topic.

Four ways to run one:
  roundtable  everyone speaks in turn, round after round; it ends early when
              a whole round has nothing new to add
  debate      the sides argue in turn; the moderator then judges
  review      the first participant writes, the others review; it repeats
              until every reviewer approves or the rounds run out
  free        the moderator decides who speaks next, and when to stop
  teams       team against team: each round every team first talks among
              itself (the other teams cannot see it), then its spokesperson
              speaks for the team to everyone; the next round's internal
              talk starts from what the other teams said in public; a team
              can be `remote`: another conversation, here or on another
              node, joins it with an invite code (see hall.py) and brings its
              own members, tools, data and model

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

MODES = ("roundtable", "debate", "review", "free", "teams")
MODE_NAMES = {"roundtable": "圓桌討論", "debate": "辯論", "review": "審查", "free": "自由討論", "teams": "組對組"}
MAX_PARTICIPANTS = 6
MAX_TEAMS = 4
MAX_TEAM_SEATS = 8            # members across all teams
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
            name = part.strip().strip("「」『』《》〈〉\"'()（）。. ")
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

#: A turn that only says nothing could be found, and so argues nothing.
NODATA_RE = re.compile(r"查不到|找不到|無法(?:查|找|佐證|驗證|提供|確認|查證)|沒有(?:查到|找到|相關資料|資料)|建議(?:先)?補充")
NODATA_MAX_CHARS = 400

MODE_RULES = {
    "roundtable": "If you have nothing new to add, reply with exactly 【無補充】 and nothing else.",
    "debate": ("Argue for your side and answer the other side's strongest point directly; do not change sides. "
               "Finding no data is not a reason to stop: argue from principle, reasoning and concrete cases, "
               "and never reply with only that you cannot find or verify something."),
    "review": "",   # set per seat: the author revises, reviewers review
    "free": "If you have nothing new to add, reply with exactly 【無補充】 and nothing else.",
    "teams": "",    # set per seat: see TEAM_RULE / SPOKESPERSON_RULE
}
TEAM_RULE = ("There are two kinds of turns. Internal turns are heard only by your own team: work with your "
             "teammates on the team's position, what supports it, and how to answer the other teams. Public turns "
             "are given by each team's spokesperson to everyone.")
SPOKESPERSON_RULE = ("You are your team's spokesperson. In a public turn, speak for the team, not for yourself: state "
                     "the position your team settled on and its reasons, and answer the other teams' strongest points.")
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
    team: str = ""          # teams mode: the team this seat belongs to
    spokesperson: bool = False


def _argues(text: str) -> bool:
    """True when the turn makes a case as well as noting missing data (long enough to carry reasons)."""
    return len(NODATA_RE.sub("", text).strip()) > 220


def _seat_prompt(seat: Seat, seats: list[Seat], topic: str, mode: str, context: str, author: bool,
                 teams: list[dict[str, Any]] | None = None) -> str:
    others = "、".join(s.name + (f"（{s.stance}）" if s.stance else "") for s in seats if s is not seat)
    rule = (AUTHOR_RULE if author else REVIEWER_RULE) if mode == "review" else MODE_RULES[mode]
    if mode == "teams":
        mates = "、".join(s.name for s in seats if s.team == seat.team and s is not seat) or "（只有你一人）"
        names = [t["name"] for t in teams] if teams else list(dict.fromkeys(s.team for s in seats))
        rivals = "；".join(f"「{team}」：{'、'.join(s.name for s in seats if s.team == team) or '在另一個對話'}"
                          for team in names if team != seat.team)
        lines = [f"You are {seat.name}, a member of team 「{seat.team}」 in a team-against-team discussion.",
                 f"Your teammates: {mates}. Other teams: {rivals}.", f"Topic: {topic}"]
        if seat.stance:
            lines.append(f"Your team's position: {seat.stance}.")
        rule = TEAM_RULE + (" " + SPOKESPERSON_RULE if seat.spokesperson else "")
    else:
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
        "- Two kinds of statements. A fact (a figure, a date, a rule, what a report says) must come from your tools or "
        "the background, with its source; never invent figures or sources, and never name a report, table, meeting or "
        "document you have not read. An argument (reasoning, a principle, a trade-off, a hypothetical case) needs no "
        "source: state it as your reasoning (「依推論」「假設」), not as a verified fact.",
        "- If a check finds nothing, say so in one clause and carry on with your argument. Never ask the others or the "
        "platform to supply data instead of taking part.",
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
    where = ""
    if entry.get("team"):
        where = f"・{entry['team']}{'組內' if entry.get('scope') == 'caucus' else '代表'}"
    return f"[第 {entry['round']} 輪{where}] {entry['speaker']}：{entry['text']}{flag}"


def _visible(entry: dict[str, Any], seat: Seat) -> bool:
    """A team's internal talk is heard only by that team; everything else by everyone."""
    return entry.get("scope") != "caucus" or entry.get("team") == seat.team


class Room:
    def __init__(self, ctx: Context, tool_ctx: ToolContext, topic: str, mode: str, seats: list[Seat],
                 rounds: int, max_tokens: int, context: str, speak_turns: int,
                 teams: list[dict[str, str]] | None = None) -> None:
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
        self.teams = teams or []
        self.hall: Any = None           # a hall when some teams join from other conversations
        self.flag: Any = None           # the hall's own stop switch, registered with the room's agents
        self.join_seconds = 600.0
        self.turn_seconds = 900.0

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
                                           author=(self.mode == "review" and index == 0), teams=self.teams),
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
    def speak(self, seat: Seat, round_no: int, scope: str = "") -> dict[str, Any] | None:
        if seat.agent is None or seat.left:
            return None
        if self._stop_wanted(seat):
            seat.left = "stopped"
            return None
        if self.tokens() >= self.max_tokens:
            self.ended = self.ended or "budget"
            return None
        news = [e for e in self.transcript[seat.seen:] if _visible(e, seat)]
        if seat.seen == 0:
            prompt = ("The discussion so far:\n" + "\n".join(_line(e) for e in news) if news else "You speak first.")
        else:
            prompt = ("Since you last spoke:\n" + "\n".join(_line(e) for e in news)) if news else "Nobody has spoken since your last turn."
        if scope == "caucus":
            prompt += (f"\n\nIt is your turn in team 「{seat.team}」's internal discussion (round {round_no}); "
                       "only your team hears this.")
        elif scope == "plenary":
            prompt += (f"\n\nIt is team 「{seat.team}」's public turn (round {round_no}): speak for your team to everyone.")
        else:
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
        if (self.mode in ("debate", "teams") and len(text) <= NODATA_MAX_CHARS and NODATA_RE.search(text)
                and not _argues(text)):
            # "No data, so nothing to say" is a stall, not a position: ask once for the argument itself.
            self._log({"type": "discussion-note", "room": self.id,
                       "text": f"{seat.name} 只說查不到資料而沒有表達立場，請它依推論論述"})
            again = self._ask(seat, "主持人提醒：查不到資料不是不論述的理由。你的立場是「" + (seat.stance or seat.team or "指定方") +
                                    "」，請直接從原則、推論、利弊與具體情境提出你的論點，並回應對方最強的一點。"
                                    "資料查不到就用一句話帶過，推論性的內容標明「依推論」，不要把推論說成已查證的事實。"
                                    "新的發言會取代上一段，不要道歉、不要提到上一段。")
            if again is None:
                return None
            text, missing = again, unsourced(again, self.sources)
            refs = uncited(again, self.corpus, self.files)
            rechecked = True
        if len(text) > MAX_UTTERANCE_CHARS:
            text = text[:MAX_UTTERANCE_CHARS] + "……（截斷）"
        entry = {"round": round_no, "speaker": seat.name, "role": seat.role.name if seat.role else "",
                 "stance": seat.stance, "text": text or "（沒有發言）", "tools": list(dict.fromkeys(seat.tools_used)),
                 "tokens": seat.agent.total_usage.prompt_tokens + seat.agent.total_usage.completion_tokens - before,
                 "unverified": missing, "uncited": refs, "rechecked": rechecked, "ts": time.time()}
        if seat.team:
            entry.update(team=seat.team, scope=scope)
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

    def stopped(self) -> bool:
        """The whole room was stopped: its hall switch, or every local seat."""
        if self.flag is not None and (self.flag.stop_requested or (self.live is not None and self.live.stopping(self.flag.live_id))):
            return True
        return bool(self.seats) and all(self._stop_wanted(s) or s.left == "stopped" for s in self.seats)

    # -- teams from other conversations ---------------------------------------------
    def remote_names(self) -> list[str]:
        return [t["name"] for t in self.teams if t.get("remote")]

    def open_hall(self) -> bool:
        halls, host = self.ctx.optional("halls"), self.ctx.optional("host_url")
        base = host() if callable(host) else host
        if halls is None or not base:
            self._log({"type": "discussion-note", "room": self.id,
                       "text": "跨對話的組別只能在網頁介面開：這裡沒有討論廳可以接受其他對話加入"})
            self.ended = "no-hall"
            return False
        local = [{"name": t["name"], "stance": t.get("stance", ""),
                  "members": [s.name for s in self.seats if s.team == t["name"]]} for t in self.teams if not t.get("remote")]
        self.hall = halls.create(self.topic, self.rounds, self.context, base, self.remote_names(), local, self.join_seconds)
        self.flag = _HallSwitch()
        if self.live is not None:
            self.flag.live_id = self.live.start(self.flag, "討論廳主持人", "", "discussion", self.topic, room=self.id)
        # The invite is a credential: it goes to the screen, not into the session log.
        self.ctx.emit("discussion/invite", {"room": self.id, "invite": self.hall.invite(), "open": self.remote_names(),
                                            "until": self.hall.join_until})
        self._log({"type": "discussion-note", "room": self.id,
                   "text": f"討論廳開放加入，等待 {'、'.join(self.remote_names())}（邀請碼只顯示在畫面上，不寫入紀錄）"})
        seen = 0
        while time.time() < self.hall.join_until and self.hall.open_seats() and not self.stopped():
            with self.hall.cond:
                self.hall.cond.wait(1.0)
            joined = self.hall.joined()
            if len(joined) != seen:
                seen = len(joined)
                self.ctx.emit("discussion/joined", {"room": self.id, "teams": [s.public() for s in joined],
                                                    "open": self.hall.open_seats()})
        missing = self.hall.open_seats()
        self.hall.start()
        if missing:
            self._log({"type": "discussion-note", "room": self.id, "text": f"{'、'.join(missing)} 沒有在時限內加入"})
        self.teams = [t for t in self.teams if not t.get("remote")] + [
            {"name": s.name, "stance": s.stance, "remote": True, "members": s.members, "node": s.node}
            for s in self.hall.joined()]
        if self.stopped():
            self.ended = "stopped"
            return False
        if len(self.teams) < 2:
            self.ended = "not-enough"
            return False
        self.ctx.emit("discussion/joined", {"room": self.id, "teams": [s.public() for s in self.hall.joined()],
                                            "open": [], "started": True})
        return True

    def await_remote(self, team: str, round_no: int) -> dict[str, Any] | None:
        """Wait for a remote team's public statement for this round."""
        seat = self.hall.seats.get(team)
        deadline = time.time() + self.turn_seconds
        while seat is not None and not seat.left:
            entry = self.hall.statement(team, round_no)
            if entry is not None:
                entry = {**entry, "role": "", "stance": seat.stance, "tokens": 0, "rechecked": False, "ts": time.time()}
                self.transcript.append(entry)
                self._log({"type": "discussion-turn", "room": self.id, **{k: v for k, v in entry.items() if k != "ts"}})
                self.ctx.emit("discussion/turn", {"room": self.id, **entry})
                self.hall.publish(entry)
                return entry
            if self.stopped():
                self.ended = self.ended or "stopped"
                return None
            if time.time() > deadline:
                self.hall.leave(seat, "沒有在時限內發言")
                self._log({"type": "discussion-note", "room": self.id, "text": f"{team} 沒有在時限內發言，之後不再等它"})
                return None
            with self.hall.cond:
                self.hall.cond.wait(1.0)
        if seat is not None and seat.left:
            self._log({"type": "discussion-note", "room": self.id, "text": f"{team} 離開了討論（{seat.left}）"})
        return None

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
                                     "brief": s.brief[:300], "team": s.team, "spokesperson": s.spokesperson}
                                    for s in self.seats]})
        self.ctx.emit("discussion/start", {"room": self.id, "topic": self.topic, "mode": self.mode,
                                           "participants": [s.name for s in self.seats],
                                           "teams": [{"name": t["name"], "stance": t.get("stance", ""),
                                                      "members": [s.name for s in self.seats if s.team == t["name"]]}
                                                     for t in self.teams]})
        try:
            self._rounds()
        finally:
            if not self.ended:
                still = self.active() or (self.hall is not None and self.hall.joined())
                self.ended = "stopped" if not still else "rounds"
        summary = ""
        try:
            summary = self.close()
        finally:
            if self.hall is not None:      # remote teams get the summary, or at least hear that it is over
                self.hall.end(summary)
            if self.flag is not None and self.live is not None:
                self.live.end(self.flag.live_id, ok=True)
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

    def _team_rounds(self) -> None:
        if self.remote_names() and not self.open_hall():
            return
        names = [t["name"] for t in self.teams]
        remote = {t["name"] for t in self.teams if t.get("remote")}
        for round_no in range(1, self.rounds + 1):
            for team in remote:    # remote teams start their internal talk on their own side, now
                self.hall.ask(team, round_no)
            for team in names:     # internal talk first; the spokesperson speaks last, so it can sum up
                members = [s for s in self.seats if s.team == team and not s.left]
                if len(members) < 2:
                    continue
                for seat in sorted(members, key=lambda s: s.spokesperson):
                    if self.ended:
                        return
                    self.speak(seat, round_no, "caucus")
            for team in names:     # then each team speaks to everyone
                if self.ended or self.stopped():
                    self.ended = self.ended or "stopped"
                    return
                if team in remote:
                    self.await_remote(team, round_no)
                    continue
                members = [s for s in self.seats if s.team == team and not s.left]
                voice = next((s for s in members if s.spokesperson), None) or (members[0] if members else None)
                if voice is None:
                    continue
                entry = self.speak(voice, round_no, "plenary")
                if entry and self.hall is not None:
                    self.hall.publish(entry)
            if not self.active() and not (self.hall is not None and self.hall.joined()):
                return

    def _rounds(self) -> None:
        if self.mode == "teams":
            self._team_rounds()
            return
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
            "teams": "4. 各組最後的立場：逐組寫出（點名組名），並說明立場在討論中有沒有改變、因為哪一組的哪個論點。",
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


def parse_teams(ctx: Context, items: Any, cwd: str) -> tuple[list[Seat], list[dict[str, str]], str]:
    """Teams mode: [{"name", "stance"?, "members": [...], "spokesperson"?}], 2-4 teams."""
    if not isinstance(items, list) or len(items) < 2:
        return [], [], "teams needs at least two teams, each with members"
    if len(items) > MAX_TEAMS:
        return [], [], f"at most {MAX_TEAMS} teams"
    seats: list[Seat] = []
    teams: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            return [], [], "each team is an object with name and members"
        name = str(item.get("name") or "").strip()
        if not NAME_RE.match(name) or any(t["name"] == name for t in teams):
            return [], [], f"team name {name!r} is empty, repeated, too long, or has brackets or line breaks"
        if item.get("remote"):
            # Filled by another conversation through an invite; its members live there.
            teams.append({"name": name, "stance": str(item.get("stance") or "").strip()[:200], "remote": True})
            continue
        members = item.get("members")
        if isinstance(members, str):
            members = [part.strip() for part in re.split(r"[,，、]", members) if part.strip()]
        if isinstance(members, list):     # a blank member is a model's placeholder, not a person
            members = [m for m in members if (str(m).strip() if not isinstance(m, dict)
                                              else str(m.get("name") or m.get("agent") or "").strip())]
        if not isinstance(members, list) or not members:
            return [], [], (f"team {name!r} has no members; if it will join from another conversation, write "
                            f'{{"name": "{name}", "remote": true}}')
        # The participant rules (names, roles) apply; a team may have a single member.
        found, problem = parse_participants(ctx, members, "roundtable", cwd, limit=MAX_TEAM_SEATS, minimum=1)
        if problem:
            return [], [], f"team {name!r}: {problem}"
        stance = str(item.get("stance") or "").strip()[:200]
        wanted = str(item.get("spokesperson") or "").strip()
        for index, seat in enumerate(found):
            if any(s.name == seat.name for s in seats):
                return [], [], f"{seat.name!r} is on two teams"
            seat.team, seat.stance = name, stance or seat.stance
            seat.spokesperson = (seat.name == wanted) if wanted else index == 0
        if wanted and not any(s.spokesperson for s in found):
            return [], [], f"spokesperson {wanted!r} is not a member of team {name!r}"
        seats += found
        teams.append({"name": name, "stance": stance})
    if len(seats) > MAX_TEAM_SEATS:
        return [], [], f"at most {MAX_TEAM_SEATS} members across all teams"
    return seats, teams, ""


class GuestRoom(Room):
    """This conversation's team in a hall hosted elsewhere: talk inside here, speak there."""

    def __init__(self, ctx: Context, tool_ctx: ToolContext, client: Any, joined: dict[str, Any],
                 seats: list[Seat], speak_turns: int, max_tokens: int) -> None:
        team = str(joined.get("team"))
        teams = [{"name": t["name"], "stance": t.get("stance", ""), "remote": t["name"] != team,
                  "members": t.get("members") or []} for t in joined.get("teams") or []]
        super().__init__(ctx, tool_ctx, str(joined.get("topic") or "")[:500], "teams", seats,
                         int(joined.get("rounds") or DEFAULT_ROUNDS), max_tokens, str(joined.get("context") or "")[:6000],
                         speak_turns, teams)
        self.client, self.team, self.host = client, team, client.base.split("/api/")[0]

    def _hear(self, entry: dict[str, Any]) -> None:
        """Another team's public statement, as the hall relayed it."""
        said = {"round": entry.get("round"), "speaker": str(entry.get("speaker") or ""), "role": "", "stance": "",
                "text": str(entry.get("text") or ""), "tools": [], "tokens": 0, "rechecked": False,
                "unverified": list(entry.get("unverified") or []), "uncited": list(entry.get("uncited") or []),
                "team": str(entry.get("team") or ""), "scope": "plenary",
                "remote": str(entry.get("remote") or self.host), "ts": time.time()}
        self.transcript.append(said)
        self._log({"type": "discussion-turn", "room": self.id, **{k: v for k, v in said.items() if k != "ts"}})
        self.ctx.emit("discussion/turn", {"room": self.id, **said})

    def run(self) -> str:
        self.seat_up()
        self._log({"type": "discussion-start", "room": self.id, "topic": self.topic, "mode": "teams",
                   "rounds": self.rounds, "max_tokens": self.max_tokens, "hall": self.host,
                   "participants": [{"name": s.name, "role": s.role.name if s.role else "", "stance": s.stance,
                                     "team": s.team, "spokesperson": s.spokesperson} for s in self.seats]})
        self.ctx.emit("discussion/start", {"room": self.id, "topic": self.topic, "mode": "teams", "hall": self.host,
                                           "participants": [s.name for s in self.seats],
                                           "teams": [{"name": t["name"], "stance": t.get("stance", ""),
                                                      "members": t.get("members") or
                                                      [s.name for s in self.seats if s.team == t["name"]]}
                                                     for t in self.teams]})
        summary, failures = "", 0
        while True:
            if self.stopped():
                self.client.leave("stopped")
                self.ended = "stopped"
                break
            try:
                reply = self.client.next()
                failures = 0
            except (ConnectionError, RuntimeError) as error:
                failures += 1
                if failures >= 4:
                    self._log({"type": "discussion-note", "room": self.id, "text": f"連不上討論廳：{error}"})
                    self.ended = "host-lost"
                    break
                time.sleep(5)
                continue
            for entry in reply.get("news") or []:
                self._hear(entry)
            if reply.get("state") == "ended":
                summary, self.ended = str(reply.get("summary") or ""), "host"
                break
            if reply.get("state") != "turn":
                continue
            round_no = int(reply.get("round") or 1)
            members = [s for s in self.seats if not s.left]
            if len(members) > 1:
                for seat in sorted(members, key=lambda s: s.spokesperson):
                    if self.stopped():
                        break
                    self.speak(seat, round_no, "caucus")
            voice = next((s for s in members if s.spokesperson and not s.left), None) or next(iter(self.active()), None)
            entry = self.speak(voice, round_no, "plenary") if voice is not None and not self.stopped() else None
            if entry is None:
                continue        # stopped or out of tokens: the next pass leaves the hall
            try:
                self.client.statement({k: entry.get(k) for k in ("round", "speaker", "text", "unverified", "uncited", "tools")})
            except (ConnectionError, RuntimeError) as error:
                self._log({"type": "discussion-note", "room": self.id, "text": f"發言沒有送到討論廳：{error}"})
        for seat in self.seats:
            if self.live is not None and seat.live_id:
                self.live.end(seat.live_id, ok=not seat.left.startswith("failed"))
        tokens = self.tokens()
        summary = summary or "（討論廳沒有傳回結論）"
        self._log({"type": "discussion-end", "room": self.id, "reason": self.ended, "turns": len(self.transcript),
                   "tokens": tokens, "summary": summary})
        self.ctx.emit("discussion/end", {"room": self.id, "reason": self.ended, "turns": len(self.transcript),
                                         "tokens": tokens, "summary": summary})
        lines = "\n".join(_line(e) for e in self.transcript)
        out = (f"Joined the hall at {self.host} as {self.team}: {len(self.transcript)} turns, ended because "
               f"{END_REASONS.get(self.ended, self.ended)}, {tokens:,} tokens on this side.\n\n## Summary from the hall\n"
               f"{summary}\n\n## Transcript (your team's internal talk and everyone's public statements)\n{lines}")
        return out[:MAX_OUTPUT]


END_REASONS = {"rounds": "輪數用完", "converged": "整輪都沒有新意見", "approved": "審查者全數通過",
               "moderator": "主持人判斷已有結論", "budget": "token 額度用完", "stopped": "被停止",
               "no-hall": "這裡不能開討論廳（只有網頁介面可以）", "not-enough": "加入的組別不足兩組",
               "host": "討論廳結束", "host-lost": "連不上討論廳"}


class _HallSwitch:
    """Stands in for an agent in the live-agent register, so Stop reaches a hall that is waiting."""

    def __init__(self) -> None:
        self.stop_requested = False
        self.live_id = ""
        self.llm_calls = 0
        self.llm = None
        from .llm import Usage

        self.total_usage = Usage()

    def stop(self) -> None:
        self.stop_requested = True


#: Appended to every argument error: local models recover from a malformed call far
#: more often when shown the smallest valid one (measured on gemma-4-31b-xvita, which
#: broke the nested participant objects in one run out of three).
EXAMPLE = ('Simplest valid call: {"topic": "要不要調降招生名額", "mode": "debate", '
           '"participants": ["正方", "反方"], "rounds": 2}')
TEAM_EXAMPLE = ('Simplest valid team call: {"topic": "要不要開夜間班", "mode": "teams", "rounds": 2, "teams": '
                '[{"name": "教務組", "members": ["正方", "查資料員"]}, {"name": "學務組", "members": ["反方", "審查員"]}]}. '
                'When a team will join from another conversation, give it no members and set remote: '
                '{"name": "學務組", "remote": true}; every team needs a name.')


def parse_participants(ctx: Context, items: Any, mode: str, cwd: str,
                       limit: int = MAX_PARTICIPANTS, minimum: int = 2) -> tuple[list[Seat], str]:
    if isinstance(items, str):
        items = [part.strip() for part in re.split(r"[,，、]", items) if part.strip()]
    if not isinstance(items, list) or len(items) < minimum:
        return [], "participants needs at least two entries" if minimum == 2 else "a team needs at least one member"
    if len(items) > limit:
        return [], f"at most {limit} participants"
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
    join_seconds = float(settings.get("join_minutes", 10)) * 60
    turn_seconds = float(settings.get("turn_minutes", 15)) * 60

    def apply(ctx: Context, _config: Any) -> None:
        def run(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            def refuse(problem: str) -> ToolResult:
                return ToolResult(f"{problem}. {EXAMPLE}", is_error=True)

            topic = str(args.get("topic") or "").strip()
            if not topic:
                return refuse("topic is required")
            mode = str(args.get("mode") or ("teams" if args.get("teams") else "roundtable")).strip().lower()
            if mode not in MODES:
                return refuse(f"mode must be one of {', '.join(MODES)}")
            teams: list[dict[str, str]] = []
            if mode == "teams":
                seats, teams, problem = parse_teams(ctx, args.get("teams"), tool_ctx.cwd)
                if problem:
                    return ToolResult(f"{problem}. {TEAM_EXAMPLE}", is_error=True)
            else:
                seats, problem = parse_participants(ctx, args.get("participants"), mode, tool_ctx.cwd)
                if problem:
                    return refuse(problem)
            try:
                rounds = int(args.get("rounds") or DEFAULT_ROUNDS)
            except (TypeError, ValueError):
                return refuse("rounds must be a number")
            room = Room(ctx, tool_ctx, topic[:500], mode, seats, max(1, min(rounds, max_rounds)),
                        max_tokens, str(args.get("context") or "")[:6000], speak_turns, teams)
            room.join_seconds, room.turn_seconds = join_seconds, turn_seconds
            return ToolResult(room.run())

        def join(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            from .hall import HallClient, parse_invite

            try:
                invite = parse_invite(str(args.get("invite") or ""))
            except ValueError as error:
                return ToolResult(f"{error}. Ask the person for the invite code the hosting conversation shows "
                                  "(it starts with xhall:).", is_error=True)
            team = str(args.get("team") or "").strip()
            seats, problem = parse_participants(ctx, args.get("members"), "roundtable", tool_ctx.cwd,
                                                limit=MAX_TEAM_SEATS, minimum=1)
            if not team or problem:
                return ToolResult((problem or "team is required") + '. Example: {"invite": "xhall:...", "team": "學務組", '
                                  '"members": ["反方", "審查員"]}', is_error=True)
            wanted = str(args.get("spokesperson") or "").strip()
            stance = str(args.get("stance") or "").strip()[:200]
            for index, seat in enumerate(seats):
                seat.team, seat.stance = team, stance or seat.stance
                seat.spokesperson = (seat.name == wanted) if wanted else index == 0
            node = ctx.optional("node_name")
            client = HallClient(invite)
            try:
                joined = client.join(team, [s.name for s in seats], next(s.name for s in seats if s.spokesperson),
                                     stance, (node() if callable(node) else node) or "")
            except (ConnectionError, RuntimeError) as error:
                return ToolResult(f"could not join: {error}", is_error=True)
            room = GuestRoom(ctx, tool_ctx, client, joined, seats, speak_turns, max_tokens)
            return ToolResult(room.run())

        register_tools(ctx, [Tool(
            name="discussion",
            description=(
                "Have several agents discuss a topic with each other and return the transcript and a summary "
                "that keeps their disagreements. Modes: roundtable (everyone in turn), debate (sides argue, then a "
                "verdict), review (the first participant writes, the others review until they approve), free (a "
                "moderator picks who speaks), teams (team against team: each team talks internally, then its "
                "spokesperson -- the first member unless named -- speaks for it to everyone; give `teams` instead "
                "of `participants`). When the person says a team will come from another conversation or another "
                'machine, list it as {"name": "<team>", "remote": true} with no members: the screen then shows an '
                "invite code for that conversation to join with. Each participant is an agent with its own tools. "
                "Simplest form: "
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
                "teams": {"type": "array", "description": "teams mode only: 2-4 teams", "items": {
                    "type": "object", "properties": {
                        "name": {"type": "string"},
                        "stance": {"type": "string", "description": "The team's position"},
                        "members": {"type": "array", "description": 'e.g. ["正方", "查資料員"]', "items": {
                            "type": "object", "properties": {"name": {"type": "string"}, "agent": {"type": "string"},
                                                             "brief": {"type": "string"}}, "required": ["name"]}},
                        "spokesperson": {"type": "string", "description": "Member who speaks for the team"},
                        "remote": {"type": "boolean", "description": "Filled by another conversation through an invite"}},
                    "required": ["name"]}},
                "rounds": {"type": "integer", "description": f"1-{max_rounds}, default {DEFAULT_ROUNDS}"},
                "context": {"type": "string", "description": "Background every participant gets"},
            }, "required": ["topic"]},
            execute=run,
            mutating=True,
        ), Tool(
            name="discussion_join",
            description=(
                "Join a team-against-team discussion hosted by another conversation (here or on another machine) "
                "with the invite code it shows (starts with xhall:). Your team talks internally here with its own "
                "tools and data; only its spokesperson's public statements go to the hall. Returns the hall's summary."),
            parameters={"type": "object", "properties": {
                "invite": {"type": "string", "description": "The invite code, starting with xhall:"},
                "team": {"type": "string", "description": "Your team's name, e.g. 學務組"},
                "members": {"type": "array", "description": 'e.g. ["反方", "審查員"]', "items": {
                    "type": "object", "properties": {"name": {"type": "string"}, "agent": {"type": "string"},
                                                     "brief": {"type": "string"}}, "required": ["name"]}},
                "stance": {"type": "string", "description": "Your team's position"},
                "spokesperson": {"type": "string", "description": "Member who speaks for the team"},
            }, "required": ["invite", "team", "members"]},
            execute=join,
            mutating=True,
        )])

    return Plugin("discussion", apply)
