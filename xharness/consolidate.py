"""Memory consolidation: fold scattered memories into tidy topic pages.

Two passes build a plan: a deterministic one that finds near-duplicate
memories (difflib over description and body) and an optional LLM one that
reads a whole topic and proposes merges, deletions, and rewrites. A plan is
only a proposal until it is applied: the CLI is dry-run by default, the
agent tool goes through the approval policy, and every applied change is
audited as "consolidate".
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .memory import SLUG, Memory, MemoryStore

DUPLICATE_THRESHOLD = 0.8
MAX_TOPIC_CHARS = 60_000


@dataclass
class Merge:
    into: str
    from_names: list[str]
    description: str
    content: str
    kind: str = "note"
    reason: str = ""


@dataclass
class Plan:
    topic: str
    merges: list[Merge] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.merges and not self.deletes

    def describe(self) -> str:
        lines = [f"consolidation plan for topic {self.topic!r}:"]
        if self.empty:
            lines.append("  nothing to do")
        for merge in self.merges:
            lines.append(f"  merge {', '.join(merge.from_names)} -> {merge.into}: {merge.description}")
            if merge.reason:
                lines.append(f"      reason: {merge.reason}")
        for name in self.deletes:
            lines.append(f"  delete {name}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


def _similarity(a: Memory, b: Memory) -> float:
    left = f"{a.description}\n{a.body}".lower()
    right = f"{b.description}\n{b.body}".lower()
    return difflib.SequenceMatcher(None, left, right).ratio()


def find_duplicates(memories: list[Memory], threshold: float = DUPLICATE_THRESHOLD) -> list[tuple[Memory, Memory, float]]:
    """Pairs of memories whose text is nearly the same, highest similarity first."""
    pairs: list[tuple[Memory, Memory, float]] = []
    for index, left in enumerate(memories):
        for right in memories[index + 1 :]:
            score = _similarity(left, right)
            if score >= threshold:
                pairs.append((left, right, round(score, 3)))
    return sorted(pairs, key=lambda item: -item[2])


def deterministic_plan(topic: str, memories: list[Memory]) -> Plan:
    """Merge near-duplicates into the newer (on a tie: fuller) memory of each pair."""
    plan = Plan(topic=topic)
    consumed: set[str] = set()
    for left, right, score in find_duplicates(memories):
        if left.name in consumed or right.name in consumed:
            continue
        # Newer wins; on a timestamp tie keep the richer text so nothing is lost.
        if (left.updated, len(left.body)) >= (right.updated, len(right.body)):
            keep, drop = left, right
        else:
            keep, drop = right, left
        body = keep.body.rstrip()
        if drop.body.strip() and drop.body.strip() not in body:
            # Similar text is not the same fact: keep the older wording, but
            # labelled with its source and date so a contradiction stays visible.
            stamp = drop.updated[:10] or "unknown date"
            body += f"\n\n---\n併入自 {drop.name}（{stamp}，較舊）：\n{drop.body.strip()}"
        plan.merges.append(
            Merge(
                into=keep.name,
                from_names=[drop.name],
                description=keep.description,
                content=body,
                kind=keep.kind,
                reason=f"near-duplicate (similarity {score})",
            )
        )
        consumed.update({keep.name, drop.name})
    return plan


def _topic_dump(memories: list[Memory]) -> str:
    text = "\n\n".join(
        f"### {memory.name}\nkind: {memory.kind}\ndescription: {memory.description}\n{memory.body.strip()}"
        for memory in memories
    )
    return text[:MAX_TOPIC_CHARS]


LLM_INSTRUCTIONS = (
    "你是記憶管理員。下面是同一主題的所有記憶。找出重複、互相矛盾、或已過時的項目，提出整併計畫。"
    "只回覆一個 JSON 物件，格式：\n"
    '{"merges":[{"into":"目標名稱","from":["被併入的名稱"],"description":"一句描述","content":"合併後內容","reason":"為什麼"}],'
    '"deletes":[{"name":"要刪的名稱","reason":"為什麼"}],"notes":["其他觀察"]}\n'
    "規則：只能使用下面出現過的名稱；矛盾時保留較新的（updated 較晚）並在 reason 說明；沒有需要整併就回空陣列。"
)


def llm_plan(topic: str, memories: list[Memory], llm: Any) -> Plan:
    """Ask the model for a plan; anything it proposes about unknown names is dropped."""
    known = {memory.name: memory for memory in memories}
    prompt = f"{LLM_INSTRUCTIONS}\n\n主題：{topic}\n\n{_topic_dump(memories)}"
    turn = llm.stream([{"role": "user", "content": prompt}], [])
    plan = Plan(topic=topic)
    match = re.search(r"\{.*\}", turn.content, re.S)
    if not match:
        plan.notes.append("model returned no JSON plan")
        return plan
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        plan.notes.append("model returned invalid JSON")
        return plan
    for item in data.get("merges") or []:
        if not isinstance(item, dict):
            continue
        into = str(item.get("into") or "").strip().lower()
        sources = [str(name).strip().lower() for name in (item.get("from") or []) if str(name).strip().lower() in known]
        sources = [name for name in sources if name != into]
        if into not in known or not sources or not str(item.get("content") or "").strip():
            plan.notes.append(f"ignored merge proposal with unknown names or empty content: {item.get('into')!r}")
            continue
        plan.merges.append(
            Merge(
                into=into,
                from_names=sources,
                description=str(item.get("description") or known[into].description),
                content=str(item["content"]),
                kind=known[into].kind,
                reason=str(item.get("reason") or "model proposal"),
            )
        )
    for item in data.get("deletes") or []:
        name = str((item.get("name") if isinstance(item, dict) else item) or "").strip().lower()
        if name in known and name not in plan.deletes:
            plan.deletes.append(name)
        elif name:
            plan.notes.append(f"ignored delete of unknown memory {name!r}")
    plan.notes.extend(str(note) for note in (data.get("notes") or []) if str(note).strip())
    return plan


def build_plan(store: MemoryStore, topic: str, llm: Any | None = None) -> Plan:
    memories = store.topics().get(topic, [])
    if not memories:
        return Plan(topic=topic, notes=[f"no memories under topic {topic!r}"])
    plan = deterministic_plan(topic, memories)
    if llm is not None:
        proposed = llm_plan(topic, memories, llm)
        already = {name for merge in plan.merges for name in [merge.into, *merge.from_names]}
        for merge in proposed.merges:
            if merge.into in already or any(name in already for name in merge.from_names):
                plan.notes.append(f"skipped model merge into {merge.into!r}: overlaps a duplicate merge")
                continue
            plan.merges.append(merge)
            already.update({merge.into, *merge.from_names})
        for name in proposed.deletes:
            if name not in already and name not in plan.deletes:
                plan.deletes.append(name)
        plan.notes.extend(proposed.notes)
    return plan


def apply_plan(store: MemoryStore, plan: Plan, actor: dict[str, Any] | None = None) -> list[str]:
    """Execute a plan; returns human-readable lines of what changed."""
    done: list[str] = []
    for merge in plan.merges:
        if not SLUG.match(merge.into):
            continue
        target = store.read(merge.into)
        store.write(
            merge.into,
            merge.description,
            merge.content,
            kind=merge.kind or (target.kind if target else "note"),
            actor=actor,
            topic=plan.topic,
            action="consolidate",
        )
        for name in merge.from_names:
            if name != merge.into and store.delete(name, actor=actor, action="consolidate"):
                done.append(f"merged {name} into {merge.into}")
    for name in plan.deletes:
        if store.delete(name, actor=actor, action="consolidate"):
            done.append(f"deleted {name}")
    return done
