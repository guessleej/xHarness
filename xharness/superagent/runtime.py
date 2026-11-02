"""The resident agent running inside an xHarness node.

One loop, every few seconds: poll each Source when its interval comes round,
let the Engine expire and start Evals, then do the stored work that is due:

  relay    a candidate, follow-up, report or notice for the main Session. While
           the main Session is busy they wait, and the next turn takes every one
           that arrived meanwhile in one message (batching: one longer prefill
           instead of one turn per event).
  launch   prepare a ticket's directory and open its worker conversation, when
           a worker slot is free (one extra slot is kept for a person's message,
           so new work never keeps a person waiting).
  deliver  hand a ticket's undelivered Inputs to its worker.
  stop     stop a worker.
  reply    send an outbox message through its Source; with writes="approve"
           each one waits for a person's approval first.

Failed work is retried after 2 s, doubling up to a minute. The host (the web
server) supplies conversations, approvals and notifications; see `Host`.
"""

from __future__ import annotations

import contextlib
import os
import queue
import statistics
import threading
import time
import traceback
from typing import Any, Callable, Protocol

from ..tools import Tool, ToolContext, ToolResult
from . import github as gh
from . import prompts
from . import telegram_source as tg
from .engine import Engine
from .model import Doc, UserError, key, live, now_iso, reason_text, running_assignment, thread_key, ticket_stage
from .policy import Policy, SettingsError, assessment_outcome, policy_effects, policy_version, validate
from .store import Store
from .watchdog import Watchdog, open_problems

TICK_SECONDS = 2.0
RETRY_FIRST, RETRY_MAX = 2.0, 60.0
#: Items per main-Session turn. Measured on granite-4.2-30b: batches of about 5 were all decided, batches of
#: about 20 left most undecided even after the reminder, so the rest wait for the next turn.
MAX_BATCH = 8
SECRET_FIELDS = ("token",)
NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-"


class Handle(Protocol):
    id: str

    def run(self, text: str) -> str: ...

    def cancel(self) -> None: ...


class Host(Protocol):
    def open_conversation(self, name: str, cwd: str, system: str, tools: list[Tool], model: str = "",
                          role: str = "worker") -> Handle: ...

    def approve(self, summary: str) -> bool: ...

    def notify(self, event: str, text: str) -> None: ...


def slug(text: str, fallback: str) -> str:
    out = "".join(c if c in NAME_CHARS else "-" for c in text.lower().strip())
    out = "-".join(p for p in out.split("-") if p)[:40]
    return out or fallback


class Worker:
    """One ticket's conversation, fed by a queue so a message that arrives mid-turn waits for the next turn."""

    def __init__(self, runtime: "Superagent", ticket_id: str, handle: Handle, cwd: str) -> None:
        self.runtime = runtime
        self.ticket_id = ticket_id
        self.handle = handle
        self.cwd = cwd
        self.inbox: "queue.Queue[tuple[str, list[str]]]" = queue.Queue()
        self.busy = False
        self.stopped = False
        self.last = ""
        self.thread = threading.Thread(target=self._loop, name=f"superagent-worker-{ticket_id[:8]}", daemon=True)
        self.thread.start()

    def enqueue(self, text: str, input_ids: list[str]) -> None:
        self.inbox.put((text, input_ids))

    def _loop(self) -> None:
        while not self.stopped:
            try:
                text, ids = self.inbox.get(timeout=1)
            except queue.Empty:
                continue
            # Everything waiting goes in one turn.
            while True:
                try:
                    more, more_ids = self.inbox.get_nowait()
                except queue.Empty:
                    break
                text, ids = f"{text}\n\n{more}", ids + more_ids
            self.busy = True
            try:
                self.runtime.engine.inputs_read(self.ticket_id, ids)
                self.last = self.handle.run(text) or ""
            except Exception as error:  # noqa: BLE001 - a crashed turn becomes a failed report
                self.last = f"error: {error}"
                self.runtime.worker_crashed(self.ticket_id, str(error))
            finally:
                self.busy = False
            if not self.stopped:
                self.runtime.worker_turn_ended(self.ticket_id, self.last)

    def stop(self) -> None:
        self.stopped = True
        with contextlib.suppress(Exception):  # a conversation that is already gone needs no stopping
            self.handle.cancel()


class Superagent:
    def __init__(self, home: str, host: Host, *, sources: dict[str, Callable[..., Any]] | None = None,
                 token_reader: Callable[[str, Doc], str] | None = None, now: Callable[[], str] | None = None) -> None:
        self.home = home
        self.host = host
        self.store = Store(os.path.join(home, "superagent.db"))
        self.kinds = sources if sources is not None else {"github": self._github, "telegram": self._telegram}
        self._token_reader = token_reader
        self.lock = threading.RLock()
        self.settings = validate(self.store.kv("settings", {}) or {})
        self.sources: dict[str, Any] = {}
        self.workers: dict[str, Worker] = {}
        self.main: Handle | None = None
        self.main_busy = False
        self.main_last = ""
        self.status_polls = 0                              # unanswered status asks in this main turn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._polled: dict[str, float] = {}
        self._approving: set[str] = set()
        self.engine = Engine(self.store, policy=self.policy, scope=lambda t: self.scope(t["source"], t.get("meta", {})),
                             assessor=self._Assessor(self), executor=self._Executor(self), outbox=self._Outbox(self),
                             emit=self._emit, now=now, prepare_ticket=self._prepare_ticket)
        self._build_sources()
        self.watchdog = Watchdog(self)

    # Ports: each only stores the work, inside the Engine's transaction.

    class _Assessor:
        def __init__(self, sa: "Superagent") -> None:
            self.sa = sa

        def request(self, candidate: Doc) -> None:
            self.sa.store.effect_add("relay", candidate["evalId"], {"kind": "candidate", "evalId": candidate["evalId"],
                                                                     "triggerIds": [t["id"] for t in candidate["triggers"]]})

        def release(self, eval_id: str) -> None:
            self.sa.store.effect_drop("relay", eval_id)

    class _Executor:
        def __init__(self, sa: "Superagent") -> None:
            self.sa = sa

        def start(self, ticket: Doc, assignment: Doc) -> None:
            self.sa.store.effect_add("launch", ticket["id"], {"ticketId": ticket["id"], "assignmentId": assignment["id"]})

        def deliver(self, ticket: Doc, assignment: Doc) -> None:
            self.sa.store.effect_add("deliver", ticket["id"], {"ticketId": ticket["id"], "assignmentId": assignment["id"]})

        def stop(self, ticket: Doc, assignment: Doc, reason: str) -> None:
            self.sa.store.effect_add("stop", f"{ticket['id']}:{assignment['id']}",
                                     {"ticketId": ticket["id"], "assignmentId": assignment["id"], "reason": reason})

    class _Outbox:
        def __init__(self, sa: "Superagent") -> None:
            self.sa = sa

        def send(self, message: Doc) -> None:
            if self.sa.store.outbox_add(message):
                self.sa.store.effect_add("reply", message["id"], {"id": message["id"]})

        def status(self, message_id: str) -> str | None:
            return self.sa.store.outbox_status(message_id)

        def withdraw(self, message_id: str) -> None:
            if self.sa.store.outbox_status(message_id) == "pending":
                self.sa.store.outbox_set(message_id, "dropped", reason="withdrawn")
                self.sa.store.effect_drop("reply", message_id)

    def _emit(self, kind: str, data: Doc) -> None:
        self.store.log_event(kind, now_iso(), data)
        if kind == "trigger.followup":
            self.store.effect_add("relay", f"followup:{data['triggerId']}", {"kind": "follow_up", **data})
        elif kind == "ticket.ended" and data.get("outcome") in ("merged", "closed"):
            self.store.effect_add("relay", f"ended:{data['ticketId']}", {"kind": "notice", "event": "ticket.ended", **data})

    # Settings, Sources and secrets

    def policy(self) -> Policy:
        return Policy(self.settings, self.scope)

    def scope(self, source: str, meta: Doc) -> Doc:
        instance = self.sources.get(source)
        if instance is None:
            return {"listened": False, "reason": "unknownSource"}
        return instance.scope(meta)

    def _scope_for(self, settings: Doc) -> Callable[[str, Doc], Doc]:
        return self.scope

    def token(self, source_id: str) -> str:
        config = self.settings["sources"].get(source_id, {}).get("config", {})
        if self._token_reader:
            return self._token_reader(source_id, config)
        path = config.get("token_file")
        if path:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
        sealed = self.store.kv(f"secret:{source_id}:token")
        if sealed:
            from ..channels import decrypt
            return decrypt(sealed)
        fallback = os.environ.get(f"XHARNESS_SUPERAGENT_{source_id.upper().replace('-', '_')}_TOKEN", "")
        if fallback:
            print(f"warning: source {source_id} reads its token from an environment variable; "
                  "store it on the settings page or mount it as a file instead", flush=True)
        return fallback

    def has_token(self, source_id: str) -> bool:
        config = self.settings["sources"].get(source_id, {}).get("config", {})
        return bool(config.get("token_file") or self.store.kv(f"secret:{source_id}:token")
                    or os.environ.get(f"XHARNESS_SUPERAGENT_{source_id.upper().replace('-', '_')}_TOKEN"))

    def _github(self, source_id: str, config: Doc) -> Any:
        checked = gh.check_config(config)
        client = gh.Client(checked["api"], lambda: self.token(source_id))
        return gh.GitHubSource(source_id, config, client, self.store, self.engine.receive, self.engine.bind_thread,
                               self.engine.thread_closed)

    def _telegram(self, source_id: str, config: Doc) -> Any:
        bridge = lambda: getattr(self.host, "telegram", lambda: None)()  # noqa: E731
        return tg.TelegramGroupSource(source_id, config, self.store, self.engine.receive, bridge)

    def _build_sources(self) -> None:
        built: dict[str, Any] = {}
        for sid, instance in self.settings["sources"].items():
            if not instance["enabled"] or instance["kind"] not in self.kinds:
                continue
            try:
                built[sid] = self.kinds[instance["kind"]](sid, instance["config"])
            except Exception as error:  # noqa: BLE001 - a bad Source shows its problem, the rest run
                built[sid] = _Broken(sid, str(error))
        old, self.sources = self.sources, built
        for source in old.values():
            if hasattr(source, "detach"):
                source.detach()        # a removed or rebuilt Telegram Source stops taking the bot's group messages

    def save_settings(self, raw: Doc, origin: str = "page", user: str = "") -> Doc:
        """Validate, move secrets out of the config, keep a version, and tell open tickets how a Policy change bears on them."""
        raw = {**raw, "sources": {sid: dict(v) for sid, v in (raw.get("sources") or {}).items()}}
        secrets_given: dict[str, str] = {}
        for sid, instance in raw["sources"].items():
            config = dict(instance.get("config") or {})
            for field in SECRET_FIELDS:
                value = config.pop(field, None)
                if isinstance(value, str) and value.strip():
                    secrets_given[sid] = value.strip()
            instance["config"] = config
        new = validate(raw)
        for sid, instance in new["sources"].items():
            if instance["kind"] == "github":
                gh.check_config(instance["config"])
            elif instance["kind"] == "telegram":
                tg.check_config(instance["config"])
            else:
                raise SettingsError(f"source {sid}: kind github or telegram")
        with self.lock:
            old = self.settings
            if secrets_given:
                from ..channels import encrypt
                for sid, value in secrets_given.items():
                    self.store.set_kv(f"secret:{sid}:token", encrypt(value))
            revision = int(self.store.kv("settings-revision", 0)) + 1
            self.store.set_kv("settings-revision", revision)
            self.store.set_kv(f"settings-version:{revision}", {"at": now_iso(), "origin": origin, "user": user,
                                                               "settings": new, "policyVersion": policy_version(new)})
            self.store.set_kv("settings", new)
            self.settings = new
            self._build_sources()
            before, after = policy_version(old), policy_version(new)
            if before != after:
                effects = policy_effects(self.store.tickets_open(), old, new, self._scope_for)
                self.engine.policy_changed(before, after, effects)
            if self.main is not None and prompts.orchestrator_prompt(old) != prompts.orchestrator_prompt(new):
                self.main = None        # the next turn opens a main Session with the new rules
        return new

    def public_settings(self) -> Doc:
        out = {**self.settings, "sources": {}}
        for sid, instance in self.settings["sources"].items():
            out["sources"][sid] = {**instance, "tokenSet": self.has_token(sid)}
        return out

    # Lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._recover()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="superagent", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for worker in list(self.workers.values()):
            worker.stop()
        for source in self.sources.values():
            if hasattr(source, "detach"):
                source.detach()
        if self._thread:
            self._thread.join(timeout=10)

    def _recover(self) -> None:
        """After a restart: assignments that were running are interrupted -- restarted with a recovery note unless settled."""
        for ticket in self.store.tickets_open():
            running = running_assignment(ticket)
            if running:
                self.engine.assignment_interrupted(ticket["id"], running["id"])
                ticket = self.store.ticket(ticket["id"])
                again = running_assignment(ticket)
                if again:                 # settled: it keeps its state, and its worker opens on the next Input
                    self.store.effect_add("launch", ticket["id"], {"ticketId": ticket["id"], "assignmentId": again["id"],
                                                                   "reopen": True})

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must not die; the error is shown as a problem
                self.store.set_kv("problem:loop", {"at": now_iso(), "error": traceback.format_exc()[-1500:]})
            self._stop.wait(TICK_SECONDS)

    def tick(self) -> None:
        if not self.settings["paused"]:
            for sid, source in list(self.sources.items()):
                interval = getattr(source, "config", {}).get("interval", 60) if hasattr(source, "config") else 60
                if time.time() - self._polled.get(sid, 0) >= interval:
                    self._polled[sid] = time.time()
                    if hasattr(source, "poll"):
                        source.poll()
        self.engine.evaluate_due()
        self.run_effects()
        if self.watchdog.due():
            self.watchdog.check()

    # Stored work

    def run_effects(self) -> None:
        relays: list[Doc] = []
        for effect in self.store.effects_due():
            if effect["kind"] == "relay":
                relays.append(effect)
                continue
            if self.settings["paused"] and effect["kind"] in ("launch", "deliver"):
                self.store.effect_defer(effect["id"], 5)
                continue
            handler = getattr(self, f"_do_{effect['kind']}", None)
            try:
                result = handler(effect["payload"]) if handler else True
            except Exception as error:  # noqa: BLE001
                delay = min(RETRY_MAX, RETRY_FIRST * 2 ** effect["attempts"])
                self.store.effect_failed(effect["id"], gh.scrub(str(error)), delay)
                if effect["kind"] == "deliver":
                    payload = effect["payload"]
                    self.engine.delivery_failed(payload["ticketId"], payload["assignmentId"], gh.scrub(str(error)))
                continue
            if result == "defer":
                self.store.effect_defer(effect["id"], 3)
            else:
                self.store.effect_done(effect["id"])
        if relays:
            self._relay(relays)

    def slots_used(self) -> int:
        return sum(1 for w in self.workers.values() if w.busy or not w.inbox.empty())

    def _full(self, ticket: Doc) -> bool:
        """No slot for a worker that is not already working; a person's message may use the one reserved slot."""
        worker = self.workers.get(ticket["id"])
        if worker is not None and not worker.stopped and (worker.busy or not worker.inbox.empty()):
            return False
        person = any(i.get("human") for i in ticket["inputs"] if i["id"] in ticket["undelivered"])
        return self.slots_used() >= self.settings["executor"]["maxWorkers"] + (1 if person else 0)

    def _do_launch(self, payload: Doc) -> Any:
        ticket = self.store.ticket(payload["ticketId"])
        if not ticket or not live(ticket):
            return True
        assignment = next((a for a in ticket["assignments"] if a["id"] == payload["assignmentId"]), None)
        if not assignment or assignment["state"] == "ended":
            return True
        if self._full(ticket):
            return "defer"
        worker = self.workers.get(ticket["id"])
        if worker is None or worker.stopped:
            cwd = self.prepare_directory(ticket)
            rules = [r for r in self.settings["policy"]["rules"] if r["id"] in (ticket["mandate"]["basis"].get("rules") or [])]
            handle = self.host.open_conversation(f"ticket {ticket['id'][:8]}: {ticket['title']}", cwd,
                                                 prompts.worker_prompt(self.settings, ticket, rules), self.worker_tools(ticket["id"]),
                                                 self.settings["executor"]["model"], role="worker")
            self.workers[ticket["id"]] = Worker(self, ticket["id"], handle, cwd)
            ticket = self.store.ticket(ticket["id"])
            ticket["execution"] = {**(ticket.get("execution") or {}), "directory": cwd, "conversation": handle.id}
            self.store.save_ticket(ticket)
        if assignment["state"] == "pending":
            # Waiting messages open the turn with it: delivered later, they would land after a ready report.
            ticket = self.store.ticket(ticket["id"])
            ids = list(ticket["undelivered"])
            text = self.start_text(ticket) + (f"\n\n{self.inputs_text(ticket, ids)}" if ids else "")
            self.engine.assignment_started(ticket["id"], assignment["id"])
            if ids:
                self.engine.inputs_delivered(ticket["id"], assignment["id"], ids)
            self.workers[ticket["id"]].enqueue(text, ids)
        elif payload.get("reopen") and ticket["undelivered"]:
            self.store.effect_add("deliver", ticket["id"], {"ticketId": ticket["id"], "assignmentId": assignment["id"]})
        return True

    def _do_deliver(self, payload: Doc) -> Any:
        ticket = self.store.ticket(payload["ticketId"])
        running = ticket and running_assignment(ticket)
        if not ticket or not live(ticket) or not running or not ticket["undelivered"]:
            return True
        worker = self.workers.get(ticket["id"])
        if worker is None or worker.stopped:
            self.store.effect_add("launch", ticket["id"], {"ticketId": ticket["id"], "assignmentId": running["id"], "reopen": True})
            return True
        if self._full(ticket):
            return "defer"
        ids = list(ticket["undelivered"])
        worker.enqueue(self.inputs_text(ticket, ids), ids)
        self.engine.inputs_delivered(ticket["id"], running["id"], ids)
        return True

    def _do_stop(self, payload: Doc) -> Any:
        worker = self.workers.get(payload["ticketId"])
        ticket = self.store.ticket(payload["ticketId"])
        if worker and (not ticket or not live(ticket) or not running_assignment(ticket)):
            worker.stop()
            self.workers.pop(payload["ticketId"], None)
        return True

    def _do_reply(self, payload: Doc) -> Any:
        message = self.store.outbox_get(payload["id"])
        if not message or message["status"] != "pending":
            return True
        source = self.sources.get(message["channel"]["source"])
        if source is None or not hasattr(source, "send"):
            self.store.outbox_set(message["id"], "dropped", reason="sourceGone")
            return True
        if self.settings["writes"] == "approve" and not message.get("approved"):
            # A person decides off the loop's thread, so polling and other work go on meanwhile.
            if message["id"] not in self._approving:
                self._approving.add(message["id"])
                threading.Thread(target=self._approve_and_send, args=(message, source), daemon=True,
                                 name=f"superagent-approve-{message['id'][:8]}").start()
            return "defer"
        self._send(message, source)
        return True

    def _approve_and_send(self, message: Doc, source: Any) -> None:
        try:
            where = source.link(message["channel"]["address"]) if hasattr(source, "link") else message["channel"]["thread"]
            if not self.host.approve(f"post on {where}: {message['text'][:300]}"):
                self.store.outbox_set(message["id"], "dropped", reason="denied")
                self.store.log_event("reply.denied", now_iso(), {"id": message["id"], "ticketId": message.get("ticketId", "")})
                self.store.effect_drop("reply", message["id"])
                return
            self.store.outbox_set(message["id"], "pending", approved=True)
            self._send(message, source)
            self.store.effect_drop("reply", message["id"])
        except Exception as error:  # noqa: BLE001 - left in the queue, retried without asking again
            self.store.set_kv(f"problem:reply:{message['id'][:8]}", {"at": now_iso(), "error": gh.scrub(str(error))[:300]})
        finally:
            self._approving.discard(message["id"])

    def _send(self, message: Doc, source: Any) -> None:
        result = source.send(message)
        self.store.outbox_set(message["id"], "sent" if result == "sent" else "dropped",
                              **({} if result == "sent" else {"reason": result}), sentAt=now_iso())
        self.store.log_event("reply.sent" if result == "sent" else "reply.dropped", now_iso(),
                             {"id": message["id"], "ticketId": message.get("ticketId", "")})
        self.store.kv_delete(f"problem:reply:{message['id'][:8]}")

    # The main Session

    def _relay(self, effects: list[Doc]) -> None:
        if self.main_busy:
            return
        items, live_effects, owed, asked = [], [], [], []
        for effect in effects:
            if len(items) >= MAX_BATCH:
                break                                      # left due: the next turn takes them
            payload = effect["payload"]
            if payload["kind"] == "candidate":
                trigger_id = payload["triggerIds"][0] if payload["triggerIds"] else ""
                candidate = self.engine.candidate(trigger_id) if trigger_id else None
                if not candidate or candidate["evalId"] != payload["evalId"]:
                    self.store.effect_done(effect["id"])
                    continue
                items.append(prompts.card(candidate, ticket_stage, self.settings["eval"]["mode"]))
                asked.append((trigger_id, candidate["strength"]))
            else:
                items.append(self._relay_text(payload))
                report = payload.get("report") or {}
                if payload["kind"] == "report" and (report.get("state") == "ready" or report.get("question")):
                    owed.append(payload["ticketId"])
            live_effects.append(effect)
        if not items:
            return
        for effect in live_effects:
            self.store.effect_done(effect["id"])
            if effect["payload"]["kind"] == "candidate":
                self.engine.candidate_delivered(effect["payload"]["evalId"])
        text = f"{len(items)} new item(s):\n\n" + "\n\n---\n\n".join(items)
        self.main_busy = True
        self.status_polls = 0
        threading.Thread(target=self._main_turn, args=(text, owed, asked), name="superagent-main", daemon=True).start()

    def _relay_text(self, payload: Doc) -> str:
        kind = payload["kind"]
        if kind == "follow_up":
            trigger = self.store.trigger(payload["triggerId"]) or {}
            owners = self.engine.owners(trigger) if trigger else []
            return (f"follow_up: reached ticket(s) {', '.join(o[:12] for o in owners)}; their workers decide it.\n"
                    + (prompts.trigger_line(trigger) if trigger else payload["triggerId"]))
        if kind == "report":
            ticket = self.store.ticket(payload["ticketId"]) or {}
            report = payload.get("report", {})
            lines = [f"report from {prompts.ticket_line(ticket, ticket_stage(ticket))}" if ticket else "report",
                     f"state: {report.get('state')}", f"summary: {prompts.clip(report.get('summary', ''), 1500, 'superagent_read ticket')}"]
            if (ticket or {}).get("attention"):
                lines.append(f"question for a person: {ticket['attention']['question']}")
            return "\n".join(lines)
        if kind == "notice":
            ticket = self.store.ticket(payload.get("ticketId", "")) or {}
            return f"notice: {payload.get('event')} {prompts.ticket_line(ticket, ticket_stage(ticket)) if ticket else ''} " \
                   f"outcome={payload.get('outcome', '')}"
        return str(payload)

    def _main_turn(self, text: str, owed: list[str] | None = None, asked: list[tuple[str, str]] | None = None) -> None:
        begun = time.time()
        try:
            if self.main is None:
                self.main = self.host.open_conversation("resident agent (main Session)", self.home,
                                                        prompts.orchestrator_prompt(self.settings), self.main_tools(),
                                                        self.settings["eval"]["model"], role="main")
            self.main_last = self.main.run(text) or ""
            # A plain-text answer reaches nobody: a ready result or a question must go out through superagent_reply.
            missing = [t for t in owed or [] if not self.store.replies_for(t, begun)]
            if missing:
                lines = []
                for ticket_id in missing:
                    ticket = self.store.ticket(ticket_id) or {}
                    question = (ticket.get("attention") or {}).get("question")
                    lines.append(f"- ticket {ticket_id[:12]}: " + (
                        f'superagent_reply purpose=attention ticket={ticket_id[:12]} question="{question}"' if question
                        else f"superagent_reply purpose=result ticket={ticket_id[:12]}"))
                self.main_last = self.main.run(
                    "Your answer above reached nobody: people only see what superagent_reply sends. Send it now:\n"
                    + "\n".join(lines)) or ""
            # A candidate is decided only by a tool call; one left undecided waits until its Eval expires.
            undecided = [(t, s) for t, s in asked or [] if self.engine.candidate(t)]
            if undecided:
                self.main_last = self.main.run(
                    "These candidates are still undecided; text does not decide them. Decide each now: explicit ones with "
                    "superagent_route (accept, attach, stop or ignore), inferred ones with superagent_assess or a route ignore.\n"
                    + "\n".join(f"- trigger {t[:12]} ({s})" for t, s in undecided)) or ""
        except Exception as error:  # noqa: BLE001
            self.store.set_kv("problem:main", {"at": now_iso(), "error": gh.scrub(str(error))[:500]})
        finally:
            self.main_busy = False

    # Workers

    def _prepare_ticket(self, ticket: Doc, outcome: Doc) -> Doc:
        name = slug(outcome.get("name") or outcome.get("title") or "", ticket["id"][:8])
        names = {t.get("execution", {}).get("name") for t in self.store.tickets(limit=1000)}
        if name in names:
            name = f"{name}-{ticket['id'][:6]}"
        ticket["execution"] = {"name": name, "assignment": outcome.get("note", "")}
        return ticket

    def work_source(self, ticket: Doc) -> Any:
        """The GitHub Source a ticket works in: the one its request came from, else the only one configured."""
        origin = self.store.trigger(ticket["origin"]["triggerId"]) or {}
        source = self.sources.get(origin.get("source", ""))
        if getattr(source, "kind", "") == "github":
            return source
        repos = [s for s in self.sources.values() if getattr(s, "kind", "") == "github"]
        return repos[0] if len(repos) == 1 else None

    def prepare_directory(self, ticket: Doc) -> str:
        name = (ticket.get("execution") or {}).get("name") or ticket["id"][:8]
        existing = (ticket.get("execution") or {}).get("directory")
        if existing and os.path.isdir(existing):
            return existing
        work = os.path.join(self.home, "superagent-work")
        source = self.work_source(ticket)
        if source is None:
            path = os.path.join(work, name)
            os.makedirs(path, exist_ok=True)
            return path
        token = self.token(source.id)
        web = "https://github.com" if source.config["api"] == "https://api.github.com" else source.config["api"].rsplit("/api/", 1)[0]
        clone = gh.ensure_clone(source.repo, os.path.join(self.home, "superagent-repos"), token, web)
        return gh.ticket_worktree(clone, work, name, source.config["defaultBranch"] or "main")

    def start_text(self, ticket: Doc) -> str:
        """What a new Assignment opens with: the assignment and its source message on the first one."""
        origin = self.store.trigger(ticket["origin"]["triggerId"])
        if len(ticket["assignments"]) > 1:
            return f"A new assignment of this ticket started ({ticket['assignments'][-2].get('endedReason') or 'resumed'}). " \
                   "Read the inputs that follow; check the directory and the threads before repeating anything."
        note = (ticket.get("execution") or {}).get("assignment")
        parts = [f"Assignment: {ticket['title']}\nScope: {ticket['mandate']['scope']}" + (f"\nRouting note: {note}" if note else "")]
        # Every message the decision covered, oldest first: a request and a later "stop" accepted together must
        # both reach the worker (measured: only the stop was shown and the worker did the work anyway).
        sources = sorted((t for t in (self.store.trigger(i) for i in ticket["origin"].get("triggerIds") or []) if t),
                         key=lambda t: t["at"]) or ([origin] if origin else [])
        if sources:
            parts.append(("Source message:\n" if len(sources) == 1 else "Source messages, oldest first:\n") + "\n\n".join(
                prompts.trigger_line(t, 3000 // len(sources), f"ticket_read trigger {t['id'][:12]}") for t in sources[-10:]))
        return "\n\n".join(parts)

    def inputs_text(self, ticket: Doc, ids: list[str]) -> str:
        """The Inputs as the worker reads them: the assignment first, then each message, cut long ones."""
        parts = []
        origin = self.store.trigger(ticket["origin"]["triggerId"])
        for input_id in ids:
            item = next((i for i in ticket["inputs"] if i["id"] == input_id), None)
            if not item:
                continue
            if item["kind"] == "trigger":
                trigger = self.store.trigger(item["triggerId"])
                if trigger and trigger["id"] != (origin or {}).get("id"):
                    tag = " (context only)" if item.get("context") else ""
                    note = f"\nRouting note: {item['note']}" if item.get("note") else ""
                    parts.append(f"New message{tag}:{note}\n" + prompts.trigger_line(trigger, 2000, f"ticket_read trigger {trigger['id'][:12]}"))
            elif item["kind"] == "policy-changed":
                parts.append(f"The Policy changed in a way that bears on this ticket: {item.get('effect')}")
            elif item["kind"] == "resumed":
                parts.append("A person's message resumed this ended ticket under its original mandate.")
            elif item["kind"] in ("text", "note"):
                parts.append(("Note from the main Session (context, not a request): " if item["kind"] == "note" else "") + item["text"])
        return "\n\n".join(parts) or "(no new input)"

    def worker_turn_ended(self, ticket_id: str, last: str) -> None:
        ticket = self.store.ticket(ticket_id)
        running = ticket and running_assignment(ticket)
        if not ticket or not live(ticket) or not running:
            return
        self.engine.turn_ended(ticket_id, running["id"])
        report = running.get("report")
        if not report or report["at"] < (running.get("deliveredAt") or ""):
            # A turn that ended without a report: the ticket waits for its next Trigger.
            try:
                self.engine.report(ticket_id, {"state": "waiting", "summary": prompts.clip(last, 1500, "the conversation")})
            except UserError:
                pass

    def worker_crashed(self, ticket_id: str, error: str) -> None:
        try:
            self.engine.report(ticket_id, {"state": "failed", "summary": f"the worker stopped with an error: {gh.scrub(error)[:500]}"})
            self.store.effect_add("relay", f"report:{ticket_id}:{time.time()}", {"kind": "report", "ticketId": ticket_id,
                                                                                 "report": {"state": "failed", "summary": error[:500]}})
        except UserError:
            pass

    # Tools

    def check_length(self, request: Doc) -> None:
        """A chat reply is at most 100 words (a hard ceiling, not a target); GitHub takes structured text."""
        trigger = self.store.trigger(request["triggerId"]) if request.get("triggerId") else None
        if trigger is None and request.get("ticketId"):
            ticket = self.store.ticket(request["ticketId"]) or {}
            trigger = self.store.trigger((ticket.get("origin") or {}).get("triggerId", ""))
        source = self.sources.get((trigger or {}).get("source", ""))
        if getattr(source, "kind", "") == "telegram" and tg.words(request["text"]) > tg.IM_WORD_LIMIT:
            raise ValueError(f"too long for a chat: {tg.words(request['text'])} words; at most {tg.IM_WORD_LIMIT}. "
                             "Say the outcome and what the person must do, nothing else.")
        if len(request["text"]) > 6000:
            raise ValueError("too long: at most 6000 characters")

    def _guard(self, work: Callable[[], str]) -> ToolResult:
        try:
            return ToolResult(work())
        except (UserError, ValueError, SettingsError, gh.GitHubError, RuntimeError) as error:
            return ToolResult(gh.scrub(str(error)), is_error=True)

    def _find_trigger(self, short: str) -> Doc:
        trigger = self.store.trigger(short)
        if trigger:
            return trigger
        matches = [t for t in self.store.recent_triggers("", 2000) if t["id"].startswith(short)]
        if len(matches) != 1:
            raise UserError("unknownTrigger")
        return matches[0]

    def _find_ticket(self, short: str) -> Doc:
        ticket = self.store.ticket(short)
        if ticket:
            return ticket
        matches = [t for t in self.store.tickets(limit=2000) if t["id"].startswith(short)]
        if len(matches) != 1:
            raise UserError("unknownTicket")
        return matches[0]

    def read(self, kind: str, ident: str, own: str | None = None, cursor: str = "") -> str:
        """Inline & Spill with a chain of recall: every object lists the ids of the objects it links to."""
        if kind == "trigger":
            trigger = self._find_trigger(ident)
            if own and not self.engine.reaches(self.store.ticket(own), trigger):
                raise UserError("notReached")
            record = self.store.disposition(trigger["id"]) or {}
            return (prompts.trigger_line(trigger, 20000) + f"\nthreads: {', '.join(thread_key(r) for r in trigger['threads'])}"
                    + f"\nreached tickets: {', '.join(i[:12] for i in record.get('fanout', [])) or 'none'}"
                    + (f"\ndecision: {record['decisionId'][:12]}" if record.get("decisionId") else ""))
        if kind == "thread":
            thread = self.store.thread(ident)
            if not thread:
                raise UserError("unknownTrigger")
            if own and own not in thread["subscriptions"]:
                raise UserError("notFollowing")
            triggers = self.store.triggers_on(ident, 20, cursor)
            lines = [f"thread {ident}; tickets: {', '.join(i[:12] for i in thread['subscriptions']) or 'none'}; "
                     f"related: {', '.join(thread.get('correlated', [])[:10]) or 'none'}"]
            lines += [prompts.trigger_line(t, 400) for t in triggers]
            if len(triggers) == 20:
                last = triggers[-1]
                lines.append(f"older: superagent_read thread {ident} cursor {last['at']}|{last['id']}")
            return "\n\n".join(lines)
        if kind == "ticket":
            ticket = self._find_ticket(ident)
            if own and ticket["id"] != own:
                raise UserError("unknownTicket")
            report = (ticket["assignments"][-1].get("report") if ticket["assignments"] else None) or {}
            threads = [t["key"] for t in self.store.threads(limit=500, subscribed=True) if ticket["id"] in t["subscriptions"]]
            return "\n".join([prompts.ticket_line(ticket, ticket_stage(ticket)), f"scope: {ticket['mandate']['scope']}",
                              f"origin trigger: {ticket['origin']['triggerId'][:12]}", f"threads: {', '.join(threads)}",
                              f"latest report: {report.get('state', 'none')} {report.get('summary', '')}",
                              f"question: {(ticket.get('attention') or {}).get('question', 'none')}",
                              f"decision: {ticket['decisionId'][:12]}"])
        if kind == "tickets":
            rows = self.store.tickets(limit=40)
            return "\n".join(prompts.ticket_line(t, ticket_stage(t)) for t in rows) or "no tickets"
        if kind == "decision":
            decision = self.store.decision(ident) or next((d for d in self.store.decisions(500) if d["id"].startswith(ident)), None)
            if not decision:
                raise UserError("unknownProblem")
            return f"{decision['kind']} on {decision['threadKey']}: {reason_text(decision['reason'])}"
        raise UserError("unknownTrigger")

    def main_tools(self) -> list[Tool]:
        sa = self

        def route(args: Doc, _ctx: ToolContext) -> ToolResult:
            out = []
            for item in (args.get("decisions") or [])[:20]:
                try:
                    trigger = sa._find_trigger(str(item.get("trigger", "")))
                    kind = item.get("kind")
                    if kind == "accept":
                        decision = sa.engine.decide(trigger["id"], {
                            "kind": "accept", "title": str(item.get("title", ""))[:120], "scope": str(item.get("scope", ""))[:500],
                            "reason": str(item.get("reason", "explicit request")), "name": str(item.get("name", "")),
                            "note": str(item.get("note", ""))[:300],
                            **({"acknowledgement": item["acknowledgement"]} if item.get("acknowledgement") else {})})
                        out.append(f"{trigger['id'][:12]}: accepted as ticket {decision.get('ticketId', '')[:12]}")
                    elif kind == "attach":
                        ticket = sa._find_ticket(str(item.get("ticket", "")))
                        sa.engine.attach(ticket["id"], trigger["id"], str(item.get("note", ""))[:300])
                        out.append(f"{trigger['id'][:12]}: attached to ticket {ticket['id'][:12]}")
                    elif kind == "stop":
                        ticket = sa._find_ticket(str(item.get("ticket", "")))
                        decision = sa.engine.decide(trigger["id"], {"kind": "stop", "ticketId": ticket["id"],
                                                                    "reason": str(item.get("reason", "asked to stop"))})
                        out.append(f"{trigger['id'][:12]}: {decision['kind']} ({reason_text(decision['reason'])})")
                    elif kind == "ignore":
                        decision = sa.engine.decide(trigger["id"], {"kind": "ignore", "reason": str(item.get("reason", ""))})
                        out.append(f"{trigger['id'][:12]}: ignored")
                    else:
                        out.append(f"{trigger['id'][:12]}: kind must be accept, attach, stop or ignore")
                except (UserError, ValueError) as error:
                    out.append(f"{item.get('trigger')}: refused: {error}")
            return ToolResult("\n".join(out) or "no decisions given")

        def assess(args: Doc, _ctx: ToolContext) -> ToolResult:
            def work() -> str:
                trigger = sa._find_trigger(str(args.get("trigger", "")))
                candidate = sa.engine.candidate(trigger["id"])
                strength = candidate["strength"] if candidate else "inferred"
                outcome = assessment_outcome(args.get("assessment") or {}, sa.settings, strength)
                if candidate:
                    outcome["evalId"] = candidate["evalId"]
                if outcome["kind"] == "accept":
                    outcome["name"] = str((args.get("assessment") or {}).get("name", ""))
                decision = sa.engine.decide(trigger["id"], outcome)
                extra = " (observe mode: recorded as would accept)" if (decision.get("audit") or {}).get("would_accept") else ""
                return f"{decision['kind']}{extra}: {reason_text(decision['reason'])}"
            return sa._guard(work)

        def reply(args: Doc, _ctx: ToolContext) -> ToolResult:
            def work() -> str:
                request = {"purpose": args.get("purpose"), "text": str(args.get("text", ""))}
                if request["purpose"] not in ("answer", "result", "attention"):
                    raise ValueError("purpose: answer, result or attention")
                if args.get("ticket"):
                    request["ticketId"] = sa._find_ticket(str(args["ticket"]))["id"]
                if args.get("trigger"):
                    request["triggerId"] = sa._find_trigger(str(args["trigger"]))["id"]
                if args.get("question"):
                    request["question"] = str(args["question"])
                sa.check_length(request)
                result = sa.engine.reply(request)
                return f"queued {result['id'][:12]} ({result['status']}{', already queued before' if result['reused'] else ''})"
            return sa._guard(work)

        def ticket(args: Doc, _ctx: ToolContext) -> ToolResult:
            def work() -> str:
                target = sa._find_ticket(str(args.get("ticket", "")))
                action = args.get("action")
                if action == "status":
                    if sa.engine.request_status(target["id"]):
                        return "asked; the answer reaches you as a new item"
                    # Measured on granite: told only "not waiting", the main Session asked again 17 times in one turn.
                    sa.status_polls += 1
                    if sa.status_polls > 2:
                        raise ValueError("stop polling: reports reach you as new items by themselves; end this turn")
                    return (f"not asked: the ticket is {ticket_stage(target)}, not waiting on a question. Its report "
                            "reaches you as a new item by itself; do not poll, end this turn")
                if action == "retry":
                    sa.engine.retry_ticket(target["id"])
                    return "retried"
                if action == "note":
                    sa.engine.note(target["id"], str(args.get("text", ""))[:1000])
                    return "noted"
                raise ValueError("action: status, retry or note")
            return sa._guard(work)

        def requeue(args: Doc, _ctx: ToolContext) -> ToolResult:
            return sa._guard(lambda: "queued" if sa.engine.reevaluate(sa._find_trigger(str(args.get("trigger", "")))["id"])
                             else "an Eval already holds it")

        def read(args: Doc, _ctx: ToolContext) -> ToolResult:
            return sa._guard(lambda: sa.read(str(args.get("kind", "")), str(args.get("id", "")), cursor=str(args.get("cursor", ""))))

        item = {"type": "object", "properties": {
            "trigger": {"type": "string"}, "kind": {"type": "string", "enum": ["accept", "attach", "stop", "ignore"]},
            "title": {"type": "string"}, "name": {"type": "string"}, "scope": {"type": "string"},
            "acknowledgement": {"type": "string"}, "note": {"type": "string"}, "ticket": {"type": "string"},
            "reason": {"type": "string"}}, "required": ["trigger", "kind"]}
        return [
            Tool("superagent_read", "Read a trigger, thread, ticket, decision, or the list of tickets (kind tickets). "
                 "Each result names the ids it links to; follow them to recall more.",
                 {"type": "object", "properties": {"kind": {"type": "string", "enum": ["trigger", "thread", "ticket", "tickets", "decision"]},
                                                   "id": {"type": "string"}, "cursor": {"type": "string"}}, "required": ["kind"]}, read),
            Tool("superagent_route", "Decide explicit candidates (1-20 items): accept, attach, stop or ignore. "
                 "Example: {\"decisions\":[{\"trigger\":\"a1b2c3\",\"kind\":\"accept\",\"title\":\"Fix README typo\","
                 "\"name\":\"readme-typo\",\"scope\":\"fix the typo in the install section\",\"acknowledgement\":\"On it: fixing the typo.\"}]}",
                 {"type": "object", "properties": {"decisions": {"type": "array", "items": item}}, "required": ["decisions"]}, route,
                 mutating=True),
            Tool("superagent_assess", "Record the assessment of a proactive (inferred) candidate against every enabled rule. "
                 "assessment: {decision: accept|ignore|unavailable, rule_assessments: [{rule_id, matched, reason}], "
                 "direction: {implementation_requested, target_basis, requires_design_decision, evidence}, title, name, scope, "
                 "reason, intent_evidence, acknowledgement}.",
                 {"type": "object", "properties": {"trigger": {"type": "string"}, "assessment": {"type": "object"}},
                  "required": ["trigger", "assessment"]}, assess, mutating=True),
            Tool("superagent_reply", "Send a short message where it belongs: result (a ready or ended ticket), attention (the ticket's "
                 "exact open question), answer (a person's question the worker answered).",
                 {"type": "object", "properties": {"purpose": {"type": "string", "enum": ["result", "attention", "answer"]},
                                                   "ticket": {"type": "string"}, "trigger": {"type": "string"},
                                                   "question": {"type": "string"}, "text": {"type": "string"}},
                  "required": ["purpose", "text"]}, reply, mutating=True),
            Tool("superagent_ticket", "Ask an idle ticket for its status, retry a blocked or failed one, or give it a note (context only).",
                 {"type": "object", "properties": {"ticket": {"type": "string"}, "action": {"type": "string", "enum": ["status", "retry", "note"]},
                                                   "text": {"type": "string"}}, "required": ["ticket", "action"]}, ticket, mutating=True),
            Tool("superagent_requeue", "Send a stored trigger to a fresh assessment.",
                 {"type": "object", "properties": {"trigger": {"type": "string"}}, "required": ["trigger"]}, requeue, mutating=True),
        ]

    def worker_tools(self, ticket_id: str) -> list[Tool]:
        sa = self

        def own() -> Doc:
            return sa.engine.require(ticket_id)

        def read(args: Doc, _ctx: ToolContext) -> ToolResult:
            return sa._guard(lambda: sa.read(str(args.get("kind", "")), str(args.get("id", "")), own=ticket_id,
                                             cursor=str(args.get("cursor", ""))))

        def ack(args: Doc, _ctx: ToolContext) -> ToolResult:
            def work() -> str:
                trigger = sa._find_trigger(str(args.get("trigger", "")))
                result = sa.engine.reply({"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": trigger["id"],
                                          "text": str(args.get("text", ""))[:400]})
                return f"acknowledgement {result['status']}{' (already queued)' if result['reused'] else ''}"
            return sa._guard(work)

        def report(args: Doc, _ctx: ToolContext) -> ToolResult:
            def work() -> str:
                state = args.get("state")
                if state == "ready":
                    sa.ready_check(ticket_id)
                body: Doc = {"state": state, "summary": str(args.get("summary", ""))[:4000]}
                if "question" in args:
                    body["question"] = args["question"] if args["question"] else None
                for name, field in (("asked_by", "askedBy"), ("answers", "answers")):
                    if args.get(name):
                        body[field] = sa._find_trigger(str(args[name]))["id"]
                ticket = sa.engine.report(ticket_id, body)
                if state in ("ready", "blocked", "failed") or body.get("question") or body.get("answers"):
                    sa.store.effect_add("relay", f"report:{ticket_id}:{time.time()}", {"kind": "report", "ticketId": ticket_id,
                                                                                       "report": body})
                return f"recorded; ticket is {ticket_stage(ticket)['stage']}"
            return sa._guard(work)

        def handback(args: Doc, _ctx: ToolContext) -> ToolResult:
            return sa._guard(lambda: "handed back to the main Session" if sa.engine.handback(
                ticket_id, sa._find_trigger(str(args.get("trigger", "")))["id"], str(args.get("reason", ""))[:300])
                else "recorded; another ticket still decides it")

        def unrelated(args: Doc, _ctx: ToolContext) -> ToolResult:
            def work() -> str:
                sa.engine.mark_unrelated(ticket_id, str(args.get("thread", "")), str(args.get("reason", ""))[:300])
                return "unsubscribed"
            return sa._guard(work)

        def follow(args: Doc, _ctx: ToolContext) -> ToolResult:
            def work() -> str:
                source = sa.work_source(sa.engine.require(ticket_id))
                if source is None:
                    raise ValueError("no GitHub Source for this ticket")
                number = int(args.get("number", 0))
                return "following" if sa.engine.follow_for_context(ticket_id, source.thread_ref(number)) else "already following"
            return sa._guard(work)

        def pull_request(args: Doc, ctx: ToolContext) -> ToolResult:
            return sa._guard(lambda: sa.open_pull_request(ticket_id, str(args.get("title", ""))[:200],
                                                          str(args.get("body", ""))[:20000], ctx))

        return [
            Tool("ticket_read", "Read this ticket, its threads, or a message that reached it (kind ticket / thread / trigger).",
                 {"type": "object", "properties": {"kind": {"type": "string", "enum": ["ticket", "thread", "trigger"]},
                                                   "id": {"type": "string"}, "cursor": {"type": "string"}}, "required": ["kind"]}, read),
            Tool("ticket_ack", "Acknowledge a person's request before working on it: one short sentence naming the change.",
                 {"type": "object", "properties": {"trigger": {"type": "string"}, "text": {"type": "string"}},
                  "required": ["trigger", "text"]}, ack, mutating=True),
            Tool("ticket_report", "Report the ticket's state: progress, waiting, ready, blocked, failed or dropped; optional question "
                 "(one concrete question for a person; null clears it), asked_by and answers (trigger ids).",
                 {"type": "object", "properties": {"state": {"type": "string", "enum": list(("progress", "waiting", "ready", "blocked",
                                                                                           "failed", "dropped"))},
                                                   "summary": {"type": "string"}, "question": {"type": "string"},
                                                   "asked_by": {"type": "string"}, "answers": {"type": "string"}},
                  "required": ["state", "summary"]}, report, mutating=True),
            Tool("ticket_handback", "Hand back a message that asks for more than this ticket, with a one-sentence reason.",
                 {"type": "object", "properties": {"trigger": {"type": "string"}, "reason": {"type": "string"}},
                  "required": ["trigger", "reason"]}, handback, mutating=True),
            Tool("ticket_thread_unrelated", "Drop a thread attached to this ticket that turned out unrelated.",
                 {"type": "object", "properties": {"thread": {"type": "string"}, "reason": {"type": "string"}},
                  "required": ["thread", "reason"]}, unrelated, mutating=True),
            Tool("ticket_thread_follow", "Follow a related GitHub issue or pull request for context (its messages reach you, grant nothing).",
                 {"type": "object", "properties": {"number": {"type": "integer"}}, "required": ["number"]}, follow, mutating=True),
            Tool("ticket_pull_request", "Open (or update) this ticket's pull request from the commits in this directory. The host runs the "
                 "team's check, pushes the branch and opens the pull request; you never push.",
                 {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                  "required": ["title", "body"]}, pull_request, mutating=True),
        ]

    def ready_check(self, ticket_id: str) -> None:
        """Code is delivered through a pull request: a model's word that it is ready is not enough."""
        ticket = self.engine.require(ticket_id)
        source = self.work_source(ticket)
        path = (ticket.get("execution") or {}).get("directory", "")
        if source is None or not path or not os.path.isdir(os.path.join(path, ".git")) and not os.path.isfile(os.path.join(path, ".git")):
            return
        if gh.git(["status", "--porcelain"], path).strip():
            raise ValueError("not ready: the directory has uncommitted changes. Commit them and call ticket_pull_request, "
                             "or undo them and report dropped if nothing needs to change.")
        base = f"origin/{source.config['defaultBranch'] or 'main'}"
        ahead = gh.git(["log", "--oneline", f"{base}..HEAD"], path).strip()
        if ahead and not any(f.get("own") for f in ticket.get("follows", [])):
            raise ValueError("not ready: the commits have no pull request yet. Call ticket_pull_request first.")

    def open_pull_request(self, ticket_id: str, title: str, body: str, ctx: ToolContext) -> str:
        ticket = self.engine.require(ticket_id)
        source = self.work_source(ticket)
        if source is None:
            raise ValueError("no GitHub Source for this ticket: it did not come from one and several are configured")
        path = (ticket.get("execution") or {}).get("directory", "")
        name = (ticket.get("execution") or {}).get("name", "")
        if not path or not os.path.isdir(path):
            raise ValueError("the ticket has no working directory")
        if gh.git(["status", "--porcelain"], path).strip():
            raise ValueError("uncommitted changes: commit them first")
        base = f"origin/{source.config['defaultBranch'] or 'main'}"
        if not gh.git(["log", "--oneline", f"{base}..HEAD"], path).strip():
            raise ValueError(f"no commits ahead of {base}")
        check = self.settings["executor"]["check"]
        if check:
            from ..isolation import run_check
            result = run_check(path, check, ctx.approve, None, label=f"ticket {ticket_id[:8]}")
            if result["exit"] != 0:
                raise ValueError(f"the check `{check}` failed (exit {result['exit']}); fix it first:\n{result['output'][-1500:]}")
        if self.settings["writes"] == "approve" and not self.host.approve(
                f"push branch xharness/{name} and open a pull request on {source.repo}: {title}"):
            raise ValueError("the person declined the pull request")
        token = self.token(source.id)
        gh.push_branch(path, name, token)
        pull = source.open_pull_request(f"xharness/{name}", title, f"{body}\n\n<!-- xharness ticket {ticket_id} -->")
        self.engine.bind_thread(ticket_id, source.thread_ref(pull["number"]), own=True)
        self.store.log_event("pull.opened", now_iso(), {"ticketId": ticket_id, "number": pull["number"]})
        return f"pull request #{pull['number']} {pull.get('html_url', '')}; its review and CI now reach you. Report ready when it is."

    # Views for the page and the report

    def overview(self) -> Doc:
        open_tickets = self.store.tickets_open()
        stages = [(t, ticket_stage(t)) for t in open_tickets]
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400))
        problems = [{"kind": k.split(":", 1)[1], **v} for k, v in self.store.kv_prefix("problem:").items()]
        for sid, source in self.sources.items():
            health = getattr(source, "health", {})
            if health.get("state") == "error":
                problems.append({"kind": f"source {sid}", "at": health.get("at"), "error": health.get("detail")})
        for effect in self.store.effects():
            if effect["attempts"] >= 3:
                problems.append({"kind": f"stored work {effect['kind']}", "error": effect["error"]})
        for decision in self.store.decisions(50, "unavailable"):
            if not decision.get("dismissed"):
                problems.append({"kind": "eval", "decisionId": decision["id"], "error": reason_text(decision["reason"])})
        problems += open_problems(self.watchdog)
        pending_writes = [m for m in self.store.outbox("pending", 50)]
        return {
            "person": [self.ticket_view(t, s) for t, s in stages if s["stage"] == "person"],
            "working": [self.ticket_view(t, s) for t, s in stages if s["stage"] != "person"],
            "problems": problems, "pendingWrites": len(pending_writes),
            "counts": self.store.event_counts(since + ".000Z"), "workers": self.slots_used(),
            "maxWorkers": self.settings["executor"]["maxWorkers"], "mode": self.settings["eval"]["mode"],
            "paused": self.settings["paused"], "main": {"busy": self.main_busy},
            "sources": {sid: getattr(s, "health", {}) for sid, s in self.sources.items()},
        }

    def ticket_view(self, ticket: Doc, stage: Doc | None = None) -> Doc:
        stage = stage or ticket_stage(ticket)
        report = (ticket["assignments"][-1].get("report") if ticket["assignments"] else None) or {}
        origin = self.store.trigger(ticket["origin"]["triggerId"]) or {}
        return {"id": ticket["id"], "title": ticket["title"], "stage": stage, "strength": ticket["mandate"]["strength"],
                "scope": ticket["mandate"]["scope"], "report": report, "attention": ticket.get("attention"),
                "ended": ticket.get("ended"), "createdAt": ticket["createdAt"], "updatedAt": ticket["updatedAt"],
                "origin": {"url": origin.get("url", ""), "actor": (origin.get("actor") or {}).get("id", ""),
                           "text": (origin.get("text") or "")[:300]},
                "conversation": (ticket.get("execution") or {}).get("conversation", ""), "delivery": ticket.get("delivery")}

    def usage(self, since: str) -> Doc:
        counts = self.store.event_counts(since)
        tickets = [t for t in self.store.tickets(limit=5000) if t["createdAt"] >= since]
        ended = [t for t in tickets if t.get("ended")]
        durations = sorted(
            (_parse(t["ended"]["at"]) - _parse(t["createdAt"])) / 60 for t in ended if t["ended"]["outcome"] != "stopped")
        decisions = [d for d in self.store.decisions(5000) if d["at"] >= since]
        return {"triggers": counts.get("trigger.received", 0), "decisions": len(decisions),
                "accepted": sum(1 for d in decisions if d["kind"] == "accept"),
                "wouldAccept": sum(1 for d in decisions if (d.get("audit") or {}).get("would_accept")),
                "tickets": len(tickets), "ended": len(ended),
                "byOutcome": {o: sum(1 for t in ended if t["ended"]["outcome"] == o) for o in ("completed", "merged", "closed", "dropped", "stopped")},
                "medianMinutes": round(statistics.median(durations), 1) if durations else None,
                "repliesSent": counts.get("reply.sent", 0), "repliesDropped": counts.get("reply.dropped", 0),
                "pullRequests": counts.get("pull.opened", 0)}


def _parse(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class _Broken:
    """A Source whose configuration failed: it receives nothing and shows why."""

    def __init__(self, source_id: str, error: str) -> None:
        self.id = source_id
        self.kind = "broken"
        self.health = {"state": "error", "detail": gh.scrub(error)[:300], "at": now_iso()}

    def scope(self, _meta: Doc) -> Doc:
        return {"listened": False, "reason": "sourceBroken"}
