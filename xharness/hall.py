"""Discussion halls: team-against-team discussions across conversations and machines.

A team-against-team discussion (discuss.py, mode "teams") can leave seats open
for teams that live elsewhere: another conversation on this node, or a
conversation on another node. The hosting conversation opens a hall and shows
an invite code; a conversation that joins with it brings its own members,
tools, data and model.

What crosses between them is only what a team says in public. Each team's
internal talk happens in its own conversation, on its own machine, and never
leaves it. Each round the hall asks every remote team for its public statement
(handing it what the other teams said in public since its last turn), and
relays the statements it receives to everyone.

The joining side always calls the host, never the other way round: a team on
.27 joining a hall on .70 needs only .27 to reach .70.

Credentials, all bearer secrets carried in the Authorization header, never in a
URL:
  - the hall secret, inside the invite code, is good for joining and nothing
    else, and only while the hall is still gathering teams;
  - each team that joins gets its own team token, good only for that team in
    that hall: read the public record, post the team's own statement, leave.
Nothing a remote team sends runs anything on the host; it is text in the
transcript, checked and labelled like any other utterance.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

INVITE_PREFIX = "xhall:"
MAX_HALLS = 20
MAX_STATEMENT_CHARS = 6_000
MAX_MEMBERS = 8
POLL_SECONDS = 25            # long-poll for the joining side
KEEP_ENDED_SECONDS = 3_600
CLIENT_HEADER = "X-XHarness-Client"


@dataclass
class Seat:
    """A team seat in a hall: open until a remote team takes it."""

    name: str
    token: str = ""
    members: list[str] = field(default_factory=list)
    spokesperson: str = ""
    stance: str = ""
    node: str = ""          # what the joining side says it is; shown, not trusted
    joined: float = 0.0
    left: str = ""
    seen: int = 0           # public entries this team has been sent
    asked: int = 0          # the round the hall last asked it for a statement
    inbox: dict[int, dict[str, Any]] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "members": self.members, "spokesperson": self.spokesperson,
                "node": self.node, "joined": bool(self.joined), "left": self.left}


class Hall:
    def __init__(self, topic: str, rounds: int, context: str, host: str, seats: list[str],
                 local_teams: list[dict[str, Any]], join_seconds: float) -> None:
        self.id = secrets.token_hex(6)
        self.secret = secrets.token_urlsafe(24)
        self.topic, self.rounds, self.context, self.host = topic, rounds, context, host
        self.local_teams = local_teams
        self.seats = {name: Seat(name) for name in seats}
        self.public: list[dict[str, Any]] = []
        self.state = "gathering"     # gathering -> running -> ended
        self.summary = ""
        self.created = time.time()
        self.join_until = self.created + join_seconds
        self.ended_at = 0.0
        self.cond = threading.Condition()

    # -- host side ---------------------------------------------------------------
    def open_seats(self) -> list[str]:
        return [s.name for s in self.seats.values() if not s.joined]

    def joined(self) -> list[Seat]:
        return [s for s in self.seats.values() if s.joined and not s.left]

    def start(self) -> None:
        """Close the door: the invite stops working, open seats are dropped."""
        with self.cond:
            self.state = "running"
            for name in [s.name for s in self.seats.values() if not s.joined]:
                del self.seats[name]
            self.cond.notify_all()

    def ask(self, team: str, round_no: int) -> None:
        with self.cond:
            seat = self.seats.get(team)
            if seat is not None and not seat.left:
                seat.asked = round_no
                self.cond.notify_all()

    def publish(self, entry: dict[str, Any]) -> None:
        """A public statement every team will hear."""
        with self.cond:
            self.public.append({k: entry.get(k) for k in (
                "round", "team", "speaker", "text", "unverified", "uncited", "remote")})
            self.cond.notify_all()

    def statement(self, team: str, round_no: int) -> dict[str, Any] | None:
        seat = self.seats.get(team)
        return seat.inbox.get(round_no) if seat else None

    def end(self, summary: str) -> None:
        with self.cond:
            self.state, self.summary, self.ended_at = "ended", summary, time.time()
            self.cond.notify_all()

    def invite(self) -> str:
        data = json.dumps({"u": self.host, "h": self.id, "s": self.secret}, separators=(",", ":"))
        return INVITE_PREFIX + base64.urlsafe_b64encode(data.encode("utf-8")).decode("ascii").rstrip("=")

    # -- remote side, through HTTP ----------------------------------------------------
    def join(self, secret: str, team: str, members: list[str], spokesperson: str, stance: str,
             node: str) -> tuple[Seat | None, str]:
        with self.cond:
            if self.state != "gathering" or time.time() > self.join_until:
                return None, "this hall is no longer taking teams"
            if not secrets.compare_digest(secret, self.secret):
                return None, "wrong invite"
            open_seats = self.open_seats()
            if not open_seats:
                return None, "every seat in this hall is taken"
            if team in self.seats and not self.seats[team].joined:
                seat = self.seats[team]
            elif len(open_seats) == 1 and team not in self.seats and team not in {t["name"] for t in self.local_teams}:
                # One open seat: the joining team takes it under its own name.
                seat = self.seats.pop(open_seats[0])
                seat.name = team
                self.seats[team] = seat
            else:
                return None, f"no open seat called {team!r}; open seats: {'、'.join(open_seats)}"
            seat.token = secrets.token_urlsafe(24)
            seat.members, seat.spokesperson, seat.stance = members[:MAX_MEMBERS], spokesperson, stance[:200]
            seat.node, seat.joined = node[:80], time.time()
            self.cond.notify_all()
            return seat, ""

    def next(self, seat: Seat, wait: float) -> dict[str, Any]:
        """What the remote team should do now: wait, speak for round N (with the news), or stop."""
        deadline = time.time() + wait
        with self.cond:
            while True:
                if self.state == "ended" or seat.left:
                    return {"state": "ended", "summary": self.summary, "news": self._news(seat)}
                if self.state == "running" and seat.asked and seat.asked not in seat.inbox:
                    return {"state": "turn", "round": seat.asked, "news": self._news(seat)}
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {"state": "wait", "phase": self.state, "open_seats": self.open_seats()}
                self.cond.wait(remaining)

    def _news(self, seat: Seat) -> list[dict[str, Any]]:
        fresh = [e for e in self.public[seat.seen:] if e.get("team") != seat.name]
        seat.seen = len(self.public)
        return fresh

    def post(self, seat: Seat, body: dict[str, Any]) -> str:
        with self.cond:
            if self.state != "running":
                return "this hall is not running"
            try:
                round_no = int(body.get("round") or 0)
            except (TypeError, ValueError):
                return "round must be a number"
            if round_no != seat.asked or round_no in seat.inbox:
                return f"the hall did not ask for a statement for round {round_no}"
            text = str(body.get("text") or "").strip()
            if not text:
                return "text is required"
            seat.inbox[round_no] = {
                "round": round_no, "team": seat.name, "scope": "plenary",
                "speaker": str(body.get("speaker") or seat.spokesperson or seat.name)[:40],
                "text": text[:MAX_STATEMENT_CHARS],
                "unverified": [str(x)[:40] for x in (body.get("unverified") or [])][:30],
                "uncited": [str(x)[:80] for x in (body.get("uncited") or [])][:30],
                "tools": [str(x)[:40] for x in (body.get("tools") or [])][:20],
                "remote": seat.node or "另一個對話",
            }
            self.cond.notify_all()
            return ""

    def leave(self, seat: Seat, why: str = "left") -> None:
        with self.cond:
            seat.left = why[:80]
            self.cond.notify_all()

    def public_view(self) -> dict[str, Any]:
        return {"topic": self.topic, "rounds": self.rounds, "context": self.context, "state": self.state,
                "teams": [{"name": t["name"], "members": t.get("members", []), "stance": t.get("stance", ""),
                           "remote": False} for t in self.local_teams]
                         + [{**s.public(), "remote": True} for s in self.seats.values()]}


class HallRegistry:
    """The halls this node hosts. One per process, shared by every conversation."""

    def __init__(self) -> None:
        self.halls: dict[str, Hall] = {}
        self.lock = threading.Lock()

    def create(self, topic: str, rounds: int, context: str, host: str, seats: list[str],
               local_teams: list[dict[str, Any]], join_seconds: float) -> Hall:
        with self.lock:
            now = time.time()
            for key in [k for k, h in self.halls.items() if h.state == "ended" and now - h.ended_at > KEEP_ENDED_SECONDS]:
                del self.halls[key]
            if sum(1 for h in self.halls.values() if h.state != "ended") >= MAX_HALLS:
                raise RuntimeError(f"this node already hosts {MAX_HALLS} open halls")
            hall = Hall(topic, rounds, context, host, seats, local_teams, join_seconds)
            self.halls[hall.id] = hall
            return hall

    def team(self, hall_id: str, token: str) -> tuple[Hall, Seat] | None:
        hall = self.halls.get(hall_id)
        if hall is None or not token:
            return None
        for seat in hall.seats.values():
            if seat.token and secrets.compare_digest(seat.token, token):
                return hall, seat
        return None

    def handle(self, method: str, hall_id: str, action: str, bearer: str, body: dict[str, Any],
               query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        """The HTTP surface, kept here so it can be tested without a server."""
        hall = self.halls.get(hall_id)
        if hall is None:
            return 404, {"error": "no such hall"}
        if method == "POST" and action == "join":
            members = [str(m)[:40] for m in (body.get("members") or []) if str(m).strip()]
            team = str(body.get("team") or "").strip()[:40]
            if not team or not members:
                return 400, {"error": "team and members are required"}
            seat, problem = hall.join(bearer, team, members, str(body.get("spokesperson") or members[0])[:40],
                                      str(body.get("stance") or ""), str(body.get("node") or ""))
            if seat is None:
                return 403 if problem == "wrong invite" else 409, {"error": problem}
            return 200, {"token": seat.token, "team": seat.name, **hall.public_view()}
        found = self.team(hall_id, bearer)
        if found is None:
            return 401, {"error": "unauthorized"}
        _hall, seat = found
        if method == "GET" and action == "next":
            try:
                wait = max(0.0, min(float((query.get("wait") or [POLL_SECONDS])[0]), POLL_SECONDS))
            except ValueError:
                wait = POLL_SECONDS
            return 200, {**hall.next(seat, wait), "teams": hall.public_view()["teams"]}
        if method == "POST" and action == "statement":
            problem = hall.post(seat, body)
            return (409, {"error": problem}) if problem else (200, {"ok": True})
        if method == "POST" and action == "leave":
            hall.leave(seat, str(body.get("why") or "left"))
            return 200, {"ok": True}
        return 404, {"error": "not found"}


# --- the joining side ---------------------------------------------------------------

def parse_invite(code: str) -> dict[str, str]:
    """{"u": host base URL, "h": hall id, "s": hall secret}; ValueError when it is not one."""
    code = code.strip()
    if not code.startswith(INVITE_PREFIX):
        raise ValueError("not an xHarness hall invite (it starts with xhall:)")
    raw = code[len(INVITE_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("the invite code is damaged; copy it again") from error
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data.get(k) for k in ("u", "h", "s")):
        raise ValueError("the invite code is incomplete; copy it again")
    parts = urlsplit(data["u"])
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("the invite names a host that is not an http(s) address")
    return {"u": data["u"].rstrip("/"), "h": data["h"], "s": data["s"]}


class HallClient:
    """Talks to a hall on another conversation or node over its HTTP surface."""

    def __init__(self, invite: dict[str, str], opener: Callable[..., Any] | None = None) -> None:
        self.base = f"{invite['u']}/api/halls/{invite['h']}"
        self.secret = invite["s"]
        self.token: str | None = None      # the team token, once joined
        self.opener = opener or urllib.request.urlopen

    def _call(self, method: str, action: str, body: dict[str, Any] | None = None, bearer: str = "",
              query: str = "", timeout: float = 30) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base}/{action}{query}", method=method,
            data=json.dumps(body or {}).encode("utf-8") if method == "POST" else None,
            headers={"Content-Type": "application/json", CLIENT_HEADER: "hall",
                     "Authorization": f"Bearer {bearer or self.token or ''}"})
        try:
            # The host URL comes from the invite and was checked to be http(s) in parse_invite.
            with self.opener(request, timeout=timeout) as response:  # nosec B310
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8") or "{}").get("error")
            except (ValueError, UnicodeDecodeError):
                detail = ""
            raise RuntimeError(f"the hall answered {error.code}: {detail or error.reason}") from error
        except urllib.error.URLError as error:
            raise ConnectionError(f"cannot reach the hall at {self.base.split('/api/')[0]} ({error.reason})") from error

    def join(self, team: str, members: list[str], spokesperson: str, stance: str, node: str) -> dict[str, Any]:
        reply = self._call("POST", "join", {"team": team, "members": members, "spokesperson": spokesperson,
                                            "stance": stance, "node": node}, bearer=self.secret)
        self.token = str(reply.get("token") or "") or None
        return reply

    def next(self, wait: float = POLL_SECONDS) -> dict[str, Any]:
        return self._call("GET", "next", query=f"?wait={int(wait)}", timeout=wait + 15)

    def statement(self, entry: dict[str, Any]) -> None:
        self._call("POST", "statement", entry)

    def leave(self, why: str) -> None:
        try:
            self._call("POST", "leave", {"why": why}, timeout=10)
        except (RuntimeError, ConnectionError):
            pass        # leaving a hall that is gone needs no answer
