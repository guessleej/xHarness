"""Skills: reusable instructions the model loads when a task calls for them.

A skill is a directory holding SKILL.md (front matter with `name` and
`description`, then the instructions) and, optionally, reference files and
scripts next to it. The same layout Claude Code and Codex use, so an existing
skill works here unchanged.

Only the index -- one line per skill -- goes into the system prompt. The
body is loaded on demand through the `skill` tool, so twenty skills cost a
few hundred tokens until one is actually needed, and what the model read is
in the transcript like any other tool result.

Where skills come from, first match wins on a name clash:
  1. <working directory>/.xharness/skills    the person's own
  2. <working directory>/.claude/skills      same, in Claude Code's layout
  3. <server start directory>/.xharness/skills   the site's, for everyone
  4. $XHARNESS_HOME/skills                    installed for this machine
  5. [skills] dirs = [...]                    anything else the operator lists
  6. ~/.claude/skills                         only with [skills] claude_home = true
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from .context import Context, Plugin
from .session import harness_home
from .tools import Tool, ToolContext, ToolResult, register_tools

SKILL_FILE = "SKILL.md"
MAX_INDEX_ENTRIES = 60
MAX_DESCRIPTION_CHARS = 240
MAX_BODY_CHARS = 30_000
MAX_LISTED_FILES = 60
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,80}$")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    directory: str
    source: str  # which kind of location it came from, for the UI

    @property
    def path(self) -> str:
        return os.path.join(self.directory, SKILL_FILE)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """`---` delimited `key: value` lines, then the body. No YAML library:
    skills only use flat string keys, and a folded description (`>`) is
    joined into one line."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    fields: dict[str, str] = {}
    key: str | None = None
    for line in head.splitlines():
        match = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if match:
            key, value = match.group(1).lower(), match.group(2).strip()
            fields[key] = "" if value in (">", "|", ">-", "|-") else value.strip("'\"")
        elif key and line.startswith((" ", "\t")):
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields, body.lstrip("\n")


class SkillStore:
    def __init__(self, settings: dict[str, Any] | None = None, site_dir: str | None = None) -> None:
        settings = settings or {}
        self.enabled = bool(settings.get("enabled", True))
        self.extra_dirs = [os.path.expanduser(str(d)) for d in settings.get("dirs", []) or []]
        self.claude_home = bool(settings.get("claude_home", False))
        self.site_dir = site_dir or os.getcwd()

    def roots(self, cwd: str | None) -> list[tuple[str, str]]:
        roots: list[tuple[str, str]] = []
        if cwd:
            roots += [(os.path.join(cwd, ".xharness", "skills"), "工作目錄"),
                      (os.path.join(cwd, ".claude", "skills"), "工作目錄")]
        roots.append((os.path.join(self.site_dir, ".xharness", "skills"), "站台"))
        roots.append((os.path.join(harness_home(), "skills"), "本機"))
        roots += [(d, "設定") for d in self.extra_dirs]
        if self.claude_home:
            roots.append((os.path.expanduser("~/.claude/skills"), "~/.claude"))
        return roots

    def discover(self, cwd: str | None = None) -> list[Skill]:
        if not self.enabled:
            return []
        found: dict[str, Skill] = {}
        seen_dirs: set[str] = set()
        for root, source in self.roots(cwd):
            real = os.path.realpath(root)
            if real in seen_dirs or not os.path.isdir(root):
                continue
            seen_dirs.add(real)
            for entry in sorted(os.listdir(root)):
                directory = os.path.join(root, entry)
                path = os.path.join(directory, SKILL_FILE)
                if not os.path.isfile(path):
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        fields, _body = parse_front_matter(handle.read(8192))
                except OSError:
                    continue
                name = fields.get("name") or entry
                if not NAME_RE.match(name) or name in found:
                    continue
                description = re.sub(r"\s+", " ", fields.get("description", "")).strip()
                found[name] = Skill(name, description[:MAX_DESCRIPTION_CHARS], directory, source)
        return sorted(found.values(), key=lambda s: s.name)

    def get(self, name: str, cwd: str | None = None) -> Skill | None:
        return next((s for s in self.discover(cwd) if s.name == name), None)

    def index(self, cwd: str | None = None) -> str | None:
        """The system-prompt section: names and one-line descriptions only."""
        skills = self.discover(cwd)
        if not skills:
            return None
        lines = [f"- {s.name}: {s.description or '(no description)'}" for s in skills[:MAX_INDEX_ENTRIES]]
        more = f"\n- ... and {len(skills) - MAX_INDEX_ENTRIES} more" if len(skills) > MAX_INDEX_ENTRIES else ""
        return ("## Skills\n\nThese are instructions written for specific kinds of task. When a task matches "
                "one, call the skill tool with its name before starting and follow what it says.\n\n"
                + "\n".join(lines) + more)


def load_skill(skill: Skill) -> str:
    """The body, plus the files beside it so the model can read references."""
    with open(skill.path, encoding="utf-8", errors="replace") as handle:
        _fields, body = parse_front_matter(handle.read())
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[skill truncated]"
    files: list[str] = []
    for current, dirs, names in os.walk(skill.directory):
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "__pycache__", "node_modules")))
        for name in sorted(names):
            if name != SKILL_FILE and not name.startswith("."):
                files.append(os.path.relpath(os.path.join(current, name), skill.directory))
        if len(files) >= MAX_LISTED_FILES:
            break
    listing = ""
    if files:
        listing = (f"\n\n---\nFiles in this skill (directory {skill.directory}); read them when the "
                   "instructions refer to them:\n" + "\n".join(f"- {f}" for f in files[:MAX_LISTED_FILES]))
    return f"# Skill: {skill.name}\n\n{body}{listing}"


def skills_plugin(settings: dict[str, Any] | None = None, site_dir: str | None = None) -> Plugin:
    def apply(ctx: Context, _config: Any) -> None:
        store = SkillStore(settings, site_dir)
        ctx.provide("skills", store)

        def run(args: dict[str, Any], tool_ctx: ToolContext) -> ToolResult:
            name = str(args.get("name") or "").strip()
            skill = store.get(name, tool_ctx.cwd)
            if skill is None:
                known = ", ".join(s.name for s in store.discover(tool_ctx.cwd)[:30]) or "(none)"
                return ToolResult(f"no skill named {name!r}; available: {known}", is_error=True)
            try:
                return ToolResult(load_skill(skill))
            except OSError as error:
                return ToolResult(f"cannot read skill {name}: {error}", is_error=True)

        register_tools(ctx, [Tool(
            name="skill",
            description=("Load a skill: instructions written for a specific kind of task, listed under "
                         "Skills in the system prompt. Call it before starting a matching task."),
            parameters={"type": "object", "properties": {"name": {"type": "string", "description": "The skill's name"}},
                        "required": ["name"]},
            execute=run,
        )])

    return Plugin("skills", apply)
