"""Model routing: which of the configured models answers this request.

The deciding is split in three, strictest first, because the layers fail in
different ways:

  1. Hard rules, decided by code and never by a model. A model that answers
     "is this sensitive?" wrongly once has leaked once, so these do not ask one.
       - a model the person's tier does not offer is out;
       - a model whose origin the operator denied (say "cn") is out;
       - a public (bought-in) model never receives what came from this machine
         -- tool results, attached files, pictures, hook output -- unless the
         operator says `external_sees = "all"`; and never receives personal
         data, secrets, internal addresses or the operator's own keywords,
         whatever the setting;
       - a model that cannot call tools, or whose context is too small for the
         request, is out.
     If nothing is left, the request fails with the reason. It does not fall
     back to the model that was ruled out.
  2. Capability and cost, among what the rules left. The request is rated easy,
     medium or hard (words, length, how long the tool loop has run; an optional
     small *decider* model settles the ambiguous middle). The cheapest model
     strong enough wins; own hardware before a rented model on a tie.
  3. Failure. A model that cannot be reached is skipped for a while and the next
     one in the ranking takes over -- only before it has produced any text, so a
     reply is never stitched from two models.

Every decision is written to the session (`route`, `route-fallback`), without
any of the text that was matched, and the usage report counts them.

Switched off unless `[routing] enabled = true`. A person who picks a model by
hand (`/model name`) takes routing out of that conversation; `/model auto`
puts it back.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .context import Context, Plugin
from .llm import AssistantTurn, OpenAIAdapter, endpoint_host, hosting_of
from .providers import PROVIDER_KEYS
from .redact import PATTERNS as SECRET_PATTERNS, scrub

LEVELS = {"easy": 1, "medium": 2, "hard": 3}
LEVEL_NAMES = {1: "簡單", 2: "中等", 3: "困難"}
WHERE_RANK = {"local": 0, "private": 1, "public": 2}
WHERE_NAMES = {"local": "本機", "private": "私有雲", "public": "外部"}
PREFER = ("cost", "quality")
EXTERNAL_SEES = ("prompt", "all")
DECIDER_WHEN = ("ambiguous", "always")
DEFAULT_COOLDOWN = 120.0
DECIDER_TIMEOUT = 20
MAX_DECIDER_CHARS = 1500
HEAVY_TOOL_LOOP = 8          # tool results in one task before the request counts as harder
SCAN_CACHE = 2048

REASON_TEXT = {
    "tier": "這個帳號沒有開放這個模型",
    "origin": "來源國在禁用清單",
    "files": "內容含本機檔案、工具結果或圖片，不能送到外部模型",
    "sensitive": "內容含個資、機密或關鍵字，不能送到外部模型",
    "tools": "這個模型不支援工具呼叫",
    "context": "內容超過這個模型的上下文",
}

#: Personal data and secrets, found by pattern. A false positive keeps a request
#: off the public models; a miss would send it there, so the patterns lean wide.
SENSITIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "身分證字號": re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?!\d)"),
    "手機號碼": re.compile(r"(?<!\d)09\d{2}[- ]?\d{3}[- ]?\d{3}(?!\d)"),
    "電子郵件": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "內網位址": re.compile(r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.])"),
}
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

HARD_WORDS = ("設計", "架構", "重構", "證明", "推導", "最佳化", "優化", "除錯", "遷移", "資安", "安全審查", "威脅",
              "演算法", "並行", "平行", "整合", "規劃", "refactor", "architect", "debug", "optimi", "migrat",
              "prove", "algorithm", "concurren", "design")
EASY_WORDS = ("翻譯", "摘要", "改寫", "潤飾", "校對", "解釋", "什麼是", "你好", "謝謝", "格式化", "列出", "幾點",
              "日期", "translate", "summar", "rewrite", "proofread", "what is", "hello", "thanks", "list ")

NEUTRAL_SYSTEM = (
    "You are xHarness, a coding agent by xCloudinfo. Use the available tools to look at and change what the "
    "person asks about; prefer small verified steps. Use paths relative to the working directory. When the "
    "task is done, reply with a concise summary in the language the user used, in the same script: "
    "Traditional Chinese for a user writing Traditional Chinese."
)
DECIDER_SYSTEM = (
    "You rate how hard a request is for an AI assistant. Answer with one word: easy (a lookup, a short rewrite, "
    "a question with a known answer), medium (a normal task of a few steps), or hard (design, debugging, "
    "multi-file changes, proofs, long analysis). Reply with only that word."
)


class RoutingError(RuntimeError):
    """Nothing may answer this request; the message says why, in plain words."""


@dataclass
class Candidate:
    name: str
    adapter: OpenAIAdapter
    where: str
    strength: int
    cost: float
    origin: str
    tools: bool
    order: int

    @property
    def public(self) -> bool:
        return self.where == "public"


@dataclass
class RouteConfig:
    models: list[Candidate]
    default: str
    prefer: str
    external_sees: str
    deny_origins: tuple[str, ...]
    sensitive_words: tuple[str, ...]
    sensitive_patterns: tuple[re.Pattern[str], ...]
    hard_words: tuple[str, ...]
    easy_words: tuple[str, ...]
    decider: str
    decider_when: str
    cooldown: float
    decider_entry: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Any) -> "RouteConfig":
        raw = dict(getattr(config, "routing", None) or {})
        providers = getattr(config, "providers", None) or {}
        spec = raw.get("models") or {}
        if not isinstance(spec, dict) or not spec:
            raise ValueError("[routing] needs at least one [routing.models.<provider name>] section")
        models: list[Candidate] = []
        for order, (name, entry) in enumerate(spec.items()):
            entry = entry or {}
            if name not in providers:
                raise ValueError(f"[routing.models.{name}]: no [providers.{name}] with that name")
            provider = providers[name]
            host_kind = hosting_of(provider.get("base_url"))
            loopback = endpoint_host(provider.get("base_url")) in ("localhost", "::1") or \
                endpoint_host(provider.get("base_url")).startswith("127.")
            where = str(entry.get("where") or ("local" if loopback else "private" if host_kind == "self-hosted" else "public"))
            if where not in WHERE_RANK:
                raise ValueError(f"[routing.models.{name}] where must be local, private or public")
            try:
                strength, cost = int(entry.get("strength", 2)), float(entry.get("cost", 0))
            except (TypeError, ValueError) as error:
                raise ValueError(f"[routing.models.{name}] strength is 1-3 and cost a number") from error
            if not 1 <= strength <= 3 or cost < 0:
                raise ValueError(f"[routing.models.{name}] strength is 1-3 and cost is not negative")
            adapter = OpenAIAdapter(**{k: v for k, v in provider.items() if k in PROVIDER_KEYS})
            models.append(Candidate(name, adapter, where, strength, cost, str(entry.get("origin") or "").lower(),
                                    bool(entry.get("tools", True)), order))
        prefer = str(raw.get("prefer") or "cost")
        sees = str(raw.get("external_sees") or "prompt")
        when = str(raw.get("decider_when") or "ambiguous")
        if prefer not in PREFER:
            raise ValueError(f"[routing] prefer must be one of {', '.join(PREFER)}")
        if sees not in EXTERNAL_SEES:
            raise ValueError(f"[routing] external_sees must be one of {', '.join(EXTERNAL_SEES)}")
        if when not in DECIDER_WHEN:
            raise ValueError(f"[routing] decider_when must be one of {', '.join(DECIDER_WHEN)}")
        default = str(raw.get("default") or models[0].name)
        if default not in {m.name for m in models}:
            raise ValueError(f"[routing] default {default!r} is not one of the routing models")
        decider = str(raw.get("decider") or "")
        decider_entry: dict[str, Any] = {}
        if decider:
            if decider not in providers:
                raise ValueError(f"[routing] decider {decider!r} is not a configured provider")
            if hosting_of(providers[decider].get("base_url")) != "self-hosted":
                # The decider reads the request. It runs on our own hardware or not at all.
                raise ValueError(f"[routing] decider {decider!r} must be self-hosted: it reads every request")
            decider_entry = {k: v for k, v in providers[decider].items() if k in PROVIDER_KEYS}
        try:
            patterns = tuple(re.compile(str(p)) for p in (raw.get("sensitive_patterns") or ()))
        except re.error as error:
            raise ValueError(f"[routing] sensitive_patterns: {error}") from error
        return cls(
            models=models, default=default, prefer=prefer, external_sees=sees,
            deny_origins=tuple(str(o).lower() for o in (raw.get("deny_origins") if "deny_origins" in raw else ["cn"]) or ()),
            sensitive_words=tuple(str(w) for w in (raw.get("sensitive_words") or ()) if str(w)),
            sensitive_patterns=patterns,
            hard_words=HARD_WORDS + tuple(str(w).lower() for w in (raw.get("hard_words") or ())),
            easy_words=EASY_WORDS + tuple(str(w).lower() for w in (raw.get("easy_words") or ())),
            decider=decider, decider_when=when,
            cooldown=float(raw.get("health_cooldown_seconds", DEFAULT_COOLDOWN)),
            decider_entry=decider_entry,
        )


def _luhn(digits: str) -> bool:
    total, flip = 0, False
    for char in reversed(digits):
        number = int(char)
        if flip:
            number = number * 2 - 9 if number > 4 else number * 2
        total += number
        flip = not flip
    return total % 10 == 0


def _transient(error: Exception) -> bool:
    """A failure of the model's side (unreachable, overloaded, broken), not of the request."""
    text = str(error)
    if "cannot reach" in text or "timed out" in text.lower():
        return True
    found = re.search(r"failed: (\d{3})", text)
    return bool(found and (int(found.group(1)) >= 500 or int(found.group(1)) == 429))


@dataclass
class Plan:
    level: int
    source: str                       # heuristic | decider | plain
    ranked: list[Candidate]           # first is the choice, the rest are the fallbacks in order
    excluded: list[tuple[str, str]]   # (provider, reason code)
    fresh: bool
    tainted: bool
    sensitive: list[str]
    demoted: list[str]


class Router:
    """The "llm" service when routing is on: stream() picks a model for every call."""

    routed = True

    def __init__(self, config: RouteConfig, ctx: Context | None = None, allowed: tuple[str, ...] = ()) -> None:
        self.config = config
        self.ctx = ctx
        self.allowed = tuple(allowed)
        self.by_name = {m.name: m for m in config.models}
        order = sorted(config.models, key=lambda m: (m.name != config.default, m.order))
        self.order = {m.name: i for i, m in enumerate(order)}
        self.current = self.by_name[config.default]
        #: A model the person picked by hand: it answers, but the hard rules still apply to it.
        self.pinned: Candidate | None = None
        self.tainted = False              # something from this machine was in the conversation: sticky
        self.health: dict[str, float] = {}
        self.last: dict[str, Any] | None = None
        self._scan_cache: dict[str, frozenset[str]] = {}
        self._decided: dict[str, int] = {}
        self._lock = threading.Lock()
        self._decider: OpenAIAdapter | None = None
        self._notified: str | None = None

    # -- what the rest of xharness reads off an llm service --------------------------------
    @property
    def model(self) -> str:
        return self.current.adapter.model

    @property
    def base_url(self) -> str:
        return self.current.adapter.base_url

    @property
    def context_tokens(self) -> int | None:
        known = [m.adapter.context_tokens for m in self.config.models if m.adapter.context_tokens]
        return self.current.adapter.context_tokens or (min(known) if known else None)

    @property
    def vision(self) -> bool:
        # Pictures are described in words unless every model can see them: the model that ends
        # up answering is not known until the request is made.
        return all(m.adapter.vision for m in self.config.models)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in ("config", "ctx", "current"):
            raise AttributeError(name)
        return getattr(self.current.adapter, name)

    def pin(self, name: str | None) -> bool:
        """Answer with this model from now on (None: route automatically again)."""
        if name is None:
            self.pinned = None
            return True
        cand = self.by_name.get(name)
        if cand is None:
            return False
        self.pinned = self.current = cand
        return True

    # -- scanning ----------------------------------------------------------------------------
    def _scan(self, text: str) -> frozenset[str]:
        key = hashlib.sha1(text.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()
        cached = self._scan_cache.get(key)
        if cached is not None:
            return cached
        found: set[str] = set()
        for label, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(text):
                found.add(label)
        if any(p.search(text) for p in SECRET_PATTERNS):
            found.add("金鑰或密碼")
        for match in CARD.finditer(text):
            digits = re.sub(r"\D", "", match.group(0))
            if 13 <= len(digits) <= 19 and _luhn(digits):
                found.add("信用卡號")
                break
        if any(p.search(text) for p in self.config.sensitive_patterns):
            found.add("自訂規則")
        lowered = text.lower()
        if any(word.lower() in lowered for word in self.config.sensitive_words):
            found.add("自訂關鍵字")
        result = frozenset(found)
        if len(self._scan_cache) >= SCAN_CACHE:
            self._scan_cache.clear()
        self._scan_cache[key] = result
        return result

    @staticmethod
    def _text(message: dict[str, Any]) -> str:
        content = message.get("content")
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False) if content else ""

    def _inspect(self, messages: list[dict[str, Any]]) -> tuple[bool, list[str]]:
        """(came from this machine, kinds of sensitive data found) over the non-system messages."""
        from .agent import SUMMARY_PREFIX

        tainted, kinds = self.tainted, set()
        for message in messages:
            if message.get("role") == "system":
                continue
            text = self._text(message)
            if message.get("role") == "tool" or message.get("internal") or text.startswith(SUMMARY_PREFIX):
                tainted = True      # a summary is written from the history: it carries what the history had
            kinds |= self._scan(text)
            for call in message.get("tool_calls") or []:
                kinds |= self._scan(str(call.get("arguments") or ""))
        return tainted, sorted(kinds)

    # -- difficulty --------------------------------------------------------------------------
    @staticmethod
    def _request(messages: list[dict[str, Any]]) -> tuple[str, int]:
        """The latest thing the person asked (without attached material) and how many tool
        results have come in since."""
        results = 0
        for message in reversed(messages):
            if message.get("role") == "tool":
                results += 1
            elif message.get("role") == "user":
                text = Router._text(message)
                return text.split("\n\n[", 1)[0] if message.get("internal") else text, results
        return "", results

    def _rate(self, request: str, results: int, tools: bool, size: int) -> int:
        if not tools:                    # a title, a summary: not an agent task
            return 1 if size < 6000 else 2
        lowered = request.lower()
        hard = any(word in lowered for word in self.config.hard_words)
        easy = any(word in lowered for word in self.config.easy_words)
        if hard or len(request) > 800 or request.count("```") >= 4:
            level = 3
        elif easy and len(request) < 120:
            level = 1
        else:
            level = 2
        return min(3, level + 1) if results >= HEAVY_TOOL_LOOP else level

    def _ask_decider(self, request: str) -> int | None:
        """The small model's rating of an ambiguous request; None when it cannot say."""
        if not self.config.decider_entry:
            return None
        key = hashlib.sha1(request.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()
        if key in self._decided:
            return self._decided[key]
        if self._decider is None:
            self._decider = OpenAIAdapter(**{**self.config.decider_entry, "timeout_seconds": DECIDER_TIMEOUT,
                                             "max_tokens": 24, "temperature": 0})
        messages = [{"role": "system", "content": DECIDER_SYSTEM},
                    {"role": "user", "content": scrub(request)[:MAX_DECIDER_CHARS]}]
        decider = self._decider
        try:
            if self.ctx is not None:     # through the same brake and meter as every other model call
                turn = self.ctx.invoke("llm/stream", {"messages": messages, "tools": []},
                                       lambda current: decider.stream(current["messages"], current["tools"]))
            else:
                turn = decider.stream(messages, [])
        except Exception:  # noqa: BLE001 - the decider is a hint; the heuristic stands without it
            return None
        # One word and nothing else: a reply that merely mentions "hard" is not a rating.
        found = re.fullmatch(r"\W*(easy|medium|hard)\W*", (turn.content or "").strip().lower())
        if not found:
            return None
        self._decided[key] = LEVELS[found.group(1)]
        return self._decided[key]

    # -- the plan ----------------------------------------------------------------------------
    def plan(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, decide: bool = True) -> Plan:
        fresh = bool(messages) and messages[-1].get("role") == "user"
        tainted, kinds = self._inspect(messages)
        with self._lock:
            self.tainted = tainted
        request, results = self._request(messages)
        size = sum(len(self._text(m)) for m in messages)
        level, source = self._rate(request, results, bool(tools), size), "heuristic" if tools else "plain"
        if (decide and tools and fresh and self.config.decider_entry and request
                and (level == 2 or self.config.decider_when == "always")):
            said = self._ask_decider(request)
            if said:
                level, source = said, "decider"
        tokens = size // 2 + len(json.dumps(tools, ensure_ascii=False)) // 3 if tools else size // 2
        needs_tools = bool(tools)
        excluded: list[tuple[str, str]] = []
        eligible: list[Candidate] = []
        for cand in ([self.pinned] if self.pinned else self.config.models):
            why = ""
            if self.allowed and cand.name not in self.allowed:
                why = "tier"
            elif cand.origin and cand.origin in self.config.deny_origins:
                why = "origin"
            elif cand.public and kinds:
                why = "sensitive"
            elif cand.public and tainted and self.config.external_sees == "prompt":
                why = "files"
            elif needs_tools and not cand.tools:
                why = "tools"
            elif cand.adapter.context_tokens and tokens * 1.2 > cand.adapter.context_tokens:
                why = "context"
            if why:
                excluded.append((cand.name, why))
            else:
                eligible.append(cand)
        now = time.time()
        down = [c for c in eligible if self.health.get(c.name, 0) > now]
        up = [c for c in eligible if c not in down]
        ranked = self._rank(up, level) + self._rank(down, level)
        if (not fresh and self.current in up and self.current.strength >= level):
            ranked = [self.current] + [c for c in ranked if c is not self.current]
        return Plan(level, source, ranked, excluded, fresh, tainted, kinds, [c.name for c in down])

    def _rank(self, candidates: list[Candidate], level: int) -> list[Candidate]:
        strong = [c for c in candidates if c.strength >= level]
        weak = [c for c in candidates if c.strength < level]
        if self.config.prefer == "quality":
            key = lambda c: (-c.strength, c.cost, WHERE_RANK[c.where], self.order[c.name])  # noqa: E731
        else:
            key = lambda c: (c.cost, WHERE_RANK[c.where], c.strength, self.order[c.name])  # noqa: E731
        return sorted(strong, key=key) + sorted(weak, key=lambda c: (-c.strength, *key(c)))

    # -- what each model is sent ---------------------------------------------------------------
    def _prepared(self, cand: Candidate, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                  date_line: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not cand.public or self.config.external_sees == "all":
            return messages, tools
        # A bought-in model gets a neutral system prompt (the real one carries AGENTS.md, the working
        # directory and the memory index) and no tool that names an internal MCP server.
        system = {"role": "system", "content": f"{NEUTRAL_SYSTEM}\n{date_line}"}
        rest = [m for m in messages if m.get("role") != "system"]
        return [system] + rest, [t for t in tools if not str(t.get("name", "")).startswith("mcp__")]

    def _log(self, event: dict[str, Any]) -> None:
        session = self.ctx.optional("session") if self.ctx is not None else None
        if session is not None:
            session.append(event)

    def _explain(self, plan: Plan, cand: Candidate) -> str:
        parts = []
        if plan.sensitive:
            parts.append("內容含" + "、".join(plan.sensitive) + "，外部模型排除")
        elif plan.tainted and self.config.external_sees == "prompt" and any(c.public for c in self.config.models):
            parts.append("內容來自本機，外部模型排除")
        parts.append(f"難度{LEVEL_NAMES[plan.level]}（{'小模型判斷' if plan.source == 'decider' else '規則判斷'}）")
        if cand.strength < plan.level:
            parts.append("沒有夠強的模型，用最強的")
        parts.append(f"選 {cand.name}（{WHERE_NAMES[cand.where]}）")
        return "；".join(parts)

    def explain(self, text: str, *, tools: bool = True, decide: bool = False) -> dict[str, Any]:
        """The decision for one request, without calling a model for the answer. For operators."""
        messages = [{"role": "user", "content": text}]
        stub = [{"name": "read"}] if tools else []
        plan = self.plan(messages, stub, decide=decide)
        return {
            "level": LEVEL_NAMES[plan.level], "source": plan.source, "sensitive": plan.sensitive,
            "choice": plan.ranked[0].name if plan.ranked else None,
            "ranking": [c.name for c in plan.ranked],
            "excluded": [{"provider": n, "why": REASON_TEXT[w]} for n, w in plan.excluded],
        }

    # -- the call --------------------------------------------------------------------------------
    def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
               on_delta: Any = None) -> AssistantTurn:
        from .agent import current_date_line

        plan = self.plan(messages, tools)
        if not plan.ranked:
            self._log({"type": "route", "provider": None, "level": plan.level, "fresh": plan.fresh,
                       "excluded": [{"provider": n, "why": w} for n, w in plan.excluded],
                       "sensitive": plan.sensitive, "reason": "沒有任何模型可以處理這個請求"})
            raise RoutingError(self._nothing(plan))
        last_error: Exception | None = None
        tried: list[str] = []
        for index, cand in enumerate(plan.ranked):
            self._notify(plan, cand, tried)
            sent, offered = self._prepared(cand, messages, tools, current_date_line())
            wrote = [False]

            def forward(text: str) -> None:
                wrote[0] = True
                if on_delta:
                    on_delta(text)
            cand.adapter.retry_delays = () if index < len(plan.ranked) - 1 else None
            try:
                turn = cand.adapter.stream(sent, offered, forward if on_delta else None)
            except RuntimeError as error:
                if not _transient(error):
                    raise
                self.health[cand.name] = time.time() + self.config.cooldown
                if wrote[0]:
                    raise          # part of a reply is already out; a second model cannot finish it
                last_error = error
                tried.append(cand.name)
                self._log({"type": "route-fallback", "provider": cand.name, "error": scrub(str(error))[:160]})
                continue
            finally:
                cand.adapter.retry_delays = None
            self.health.pop(cand.name, None)
            if plan.source != "plain":      # a title or a summary is not a decision about the person's request
                self._record(plan, cand, tried)
            return turn
        raise RuntimeError(f"every eligible model failed; last error: {scrub(str(last_error))[:200]}")

    def _notify(self, plan: Plan, cand: Candidate, tried: list[str]) -> None:
        """Tell the screen which model is about to be asked (not yet that it answered)."""
        if self.ctx is None or plan.source == "plain" or not (plan.fresh or tried or self._notified != cand.name):
            return
        self._notified = cand.name
        reason = (f"{'、'.join(tried)} 連不上，改用 {cand.name}" if tried else self._explain(plan, cand))
        self.ctx.emit("router/decision", {
            "provider": cand.name, "model": cand.adapter.model, "where": cand.where, "level": plan.level,
            "reason": reason, "excluded": [{"provider": n, "why": w} for n, w in plan.excluded], "fallback": bool(tried)})

    def _record(self, plan: Plan, cand: Candidate, tried: list[str]) -> None:
        """Write the decision down once the model that answered is known."""
        changed = self.last is None or self.last.get("provider") != cand.name
        self.current = cand
        if not (plan.fresh or changed or tried):
            return
        event = {"type": "route", "provider": cand.name, "model": cand.adapter.model, "where": cand.where,
                 "level": plan.level, "source": plan.source, "fresh": plan.fresh,
                 "excluded": [{"provider": n, "why": w} for n, w in plan.excluded],
                 "sensitive": plan.sensitive, "tainted": plan.tainted, "tried": tried,
                 "reason": self._explain(plan, cand) + (f"（{'、'.join(tried)} 連不上，改用備援）" if tried else "")}
        self.last = event
        self._log(event)

    def _nothing(self, plan: Plan) -> str:
        why = []
        for code in dict.fromkeys(w for _n, w in plan.excluded):
            names = "、".join(n for n, w in plan.excluded if w == code)
            why.append(f"{names}：{REASON_TEXT[code]}")
        extra = f"（內容含：{'、'.join(plan.sensitive)}）" if plan.sensitive else ""
        hint = f" 你指定了 {self.pinned.name}；用 /model auto 回到自動路由。" if self.pinned else ""
        return "沒有模型可以處理這個請求，沒有改送到被排除的模型。" + extra + " ".join(why) + hint

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {"current": self.current.name, "pinned": self.pinned.name if self.pinned else None,
                "tainted": self.tainted, "last": self.last,
                "down": {n: int(u - now) for n, u in self.health.items() if u > now},
                "models": [{"name": m.name, "model": m.adapter.model, "where": m.where, "strength": m.strength,
                            "cost": m.cost, "origin": m.origin,
                            "allowed": not self.allowed or m.name in self.allowed} for m in self.config.models]}


def router_plugin(config: Any, allowed: tuple[str, ...] = ()) -> Plugin:
    route_config = RouteConfig.from_config(config)

    def apply(ctx: Context, _config: Any) -> None:
        router = Router(route_config, ctx, allowed)
        ctx.provide("router", router)
        ctx.provide("llm", router)

    return Plugin("router", apply)
