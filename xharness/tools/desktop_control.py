"""Desktop tools: `desktop_look` and `desktop_act` -- see the screen and use the
mouse and keyboard, on the machine xharness runs on (macOS and Windows).

`desktop_look` lists what is on screen as numbered elements (role, label,
value, position), read through the Accessibility API, so a model that cannot
see can still say "click 7". With `screenshot: true` and a `[vision]`
describing model, a picture of the screen is described in words as well.
`desktop_act` clicks an element (or a point), pastes text, presses keys,
scrolls, or brings an app to the front, then looks again and returns the new
screen, so the model sees what its action did.

Every call asks the person, even when the approval mode is "auto": this is
the person's own computer, a misplaced click can send a message or delete a
file, and an approval card names the element ("click AXButton「傳送」 in
LINE"). Nobody to ask means no. `[tools] desktop_approval = false` puts the
tools back under the normal approval policy.

One screen, one mouse: a lock lets one action run at a time, and only the
main agent of a conversation gets these tools (children and discussion seats
do not), so the approvals cannot be skipped through a child whose approvals
are forwarded.

Refused outright, approval or not: typing into a password field (the person
types passwords), the key combinations that empty the Trash (macOS) or delete
past the Recycle Bin (Windows), and, on Windows, an app that runs as
administrator (Windows drops input sent to it without saying so).

Each platform has its own backend script, run once per request in its own
process: desktop_mac.py (Accessibility API, Quartz events) and desktop_win.py
(UI Automation, SendInput). Keys travel by name ("cmd", "s"); on Windows "cmd"
means ctrl, so a model can use the same words on both.

Enabled by `[tools] desktop_control = true`.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - runs this package's own backend with a fixed argv
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..context import Context, Plugin
from . import Tool, ToolContext, ToolResult, register_tools

HERE = os.path.dirname(os.path.abspath(__file__))
#: sys.platform -> the backend module next to this file
BACKENDS = {"darwin": "desktop_mac", "win32": "desktop_win"}
BACKEND_TIMEOUT = 30
SETTLE_SECONDS = 0.7
STALE_SECONDS = 300
MAX_ELEMENTS = 150
MAX_TEXT = 2000
MAX_CLICKS = 3
MAX_SCROLL = 30
#: Containers that carry no label of their own are walked but not listed.
QUIET_ROLES = {"AXGroup", "AXScrollArea", "AXSplitGroup", "AXLayoutArea", "AXUnknown", "AXSplitter", "AXLayoutItem",
               "Pane", "Group", "Custom", "Separator", "Thumb"}

MODIFIERS = {"cmd": 0x100000, "command": 0x100000, "shift": 0x20000, "alt": 0x80000, "option": 0x80000,
             "opt": 0x80000, "ctrl": 0x40000, "control": 0x40000, "win": 0x100000, "windows": 0x100000}
KEYCODES = {
    **dict(zip("asdfhgzxcvbqweryt", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17])),
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
    "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41,
    "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "forward_delete": 117, "home": 115, "end": 119, "page_up": 116, "page_down": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    **{f"f{n}": code for n, code in zip(range(1, 13), [122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111])},
}
#: Irreversible, so never sent: empty the Trash (with or without its confirmation), and on Windows delete a file
#: past the Recycle Bin.
REFUSED_COMBOS = {
    "darwin": {
        frozenset({"cmd", "shift", "delete"}): "empties the Trash",
        frozenset({"cmd", "alt", "shift", "delete"}): "empties the Trash without asking",
    },
    "win32": {
        frozenset({"shift", "delete"}): "deletes for good, skipping the Recycle Bin",
        frozenset({"shift", "forward_delete"}): "deletes for good, skipping the Recycle Bin",
    },
}
PERMISSION_HELP = {
    "no-accessibility": ("macOS has not allowed this app to control the computer. The person needs to open "
                         "System Settings > Privacy & Security > Accessibility and turn on the app that runs xharness "
                         "(Terminal, or Python for the desktop window), then try again."),
    "no-screen-recording": ("macOS has not allowed screenshots. The person needs to turn on the app that runs xharness "
                            "under System Settings > Privacy & Security > Screen Recording, then restart it. "
                            "desktop_look without a screenshot still works."),
    "no-desktop": ("There is no desktop to work on: the screen is locked or signed out, or xharness was started outside "
                   "the person's sign-in (as a service, or over SSH). Start it from the person's own desktop session."),
    "elevated": ("The app in front runs as administrator, and Windows drops input that a normal program sends to it. "
                 "Ask the person to do this step themselves, or to start xharness as administrator."),
}


def parse_combo(text: str) -> tuple[int, int, frozenset[str]]:
    """"cmd+shift+s" -> (macOS keycode, macOS modifier flags, canonical names). Raises ValueError."""
    parts = [p.strip().lower() for p in str(text).replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError("keys is required, e.g. \"cmd+s\" or \"return\"")
    *mods, key = parts
    key = {"del": "delete", "pageup": "page_up", "pagedown": "page_down"}.get(key, key)
    if key not in KEYCODES:
        raise ValueError(f"unknown key: {key}; use a letter, digit, return, tab, space, delete, escape, an arrow "
                         "(left/right/up/down), home, end, page_up, page_down or f1-f12")
    flags, names = 0, set()
    for mod in mods:
        if mod not in MODIFIERS:
            raise ValueError(f"unknown modifier: {mod}; use cmd (ctrl on Windows), shift, alt, ctrl or win")
        flags |= MODIFIERS[mod]
        names.add({"command": "cmd", "option": "alt", "opt": "alt", "control": "ctrl", "windows": "win"}.get(mod, mod))
    return KEYCODES[key], flags, frozenset(names | {canonical_key(key)})


def canonical_key(key: str) -> str:
    return {"backspace": "delete", "enter": "return", "esc": "escape"}.get(key, key)


def key_step(text: str) -> dict[str, Any]:
    """One key action for any backend: macOS reads code and flags, the others read the names."""
    code, flags, names = parse_combo(text)
    *mods, key = [p.strip().lower() for p in str(text).replace(" ", "").split("+") if p.strip()]
    mods = [{"command": "cmd", "option": "alt", "opt": "alt", "control": "ctrl", "windows": "win"}.get(m, m) for m in mods]
    key = canonical_key({"del": "delete", "pageup": "page_up", "pagedown": "page_down"}.get(key, key))
    return {"action": "key", "code": code, "flags": flags, "mods": mods, "key": key}


def backend_argv(module: str) -> list[str]:
    """How to run a backend script: from source with this Python, or through the packaged app's own executable."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--desktop-backend", module]
    return [sys.executable, "-I", os.path.join(HERE, f"{module}.py")]


class ScriptBackend:
    """Runs this platform's backend script once per request in its own process."""

    def __init__(self, module: str | None = None) -> None:
        self.module = module or BACKENDS[sys.platform]
        self.image_suffix = ".png" if self.module == "desktop_win" else ".jpg"

    def call(self, request: dict[str, Any], timeout: float = BACKEND_TIMEOUT) -> dict[str, Any]:
        try:
            done = subprocess.run(  # nosec B603 - our own script, fixed argv, the request is one JSON argument
                [*backend_argv(self.module), json.dumps(request)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"the screen did not answer within {timeout:g} seconds (an app may be busy)") from error
        if not (done.stdout or "").strip():
            # A crash inside the screen APIs ends the process without a word on stdout.
            raise RuntimeError(f"the screen reader stopped (exit {done.returncode:#x}); try again: an app may have been "
                               "opening or closing" + (f": {done.stderr.strip()[-200:]}" if done.stderr.strip() else ""))
        try:
            result = json.loads(done.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"desktop backend failed: {(done.stderr or done.stdout).strip()[:300]}") from error
        if result.get("error"):
            raise RuntimeError(PERMISSION_HELP.get(result["error"], result["error"]))
        return result


@dataclass
class Snapshot:
    app: str
    pid: int
    taken: float
    screen: list[int]
    elements: list[dict[str, Any]] = field(default_factory=list)
    window: str = ""


def element_line(index: int, element: dict[str, Any]) -> str:
    x, y, w, h = element["rect"]
    kind = element["role"] + (f"/{element['subrole']}" if element.get("subrole") else "")
    parts = [f"[{index}] {kind}"]
    if element.get("label"):
        parts.append(f"「{element['label'][:80]}」")
    if element.get("value"):
        parts.append(f"value=「{element['value'][:80]}」")
    parts.append(f"at ({x + w // 2},{y + h // 2})")
    if w > 200 or h > 100:
        parts.append(f"size {w}x{h}")
    if element.get("focused"):
        parts.append("focused")
    if not element.get("enabled", True):
        parts.append("disabled")
    return " ".join(parts)


def listed(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in elements if e.get("role") not in QUIET_ROLES or e.get("label") or e.get("value") or e.get("focused")]


def describe_element(snapshot: Snapshot | None, index: int) -> str:
    if snapshot is None or not 1 <= index <= len(snapshot.elements):
        return f"element {index}"
    element = snapshot.elements[index - 1]
    text = element.get("label") or element.get("value") or ""
    return f"{element['role']}「{text[:40]}」" if text else element["role"] + (f"/{element['subrole']}" if element.get("subrole") else "")


class Desktop:
    def __init__(self, backend: Any = None, describer: tuple[str, Callable[[str, str], str]] | None = None,
                 settle: float = SETTLE_SECONDS) -> None:
        self.backend = backend or ScriptBackend()
        self.describer = describer
        self.settle = settle
        # ponytail: one lock for the whole screen -- there is one mouse; nothing finer is possible.
        self.lock = threading.Lock()
        self.snapshots: dict[str, Snapshot] = {}

    def look(self, agent: str, app: str = "", apps: bool = True) -> tuple[Snapshot, str]:
        raw = self.backend.call({"op": "look", "app": app, "limit": MAX_ELEMENTS * 2})
        snapshot = Snapshot(raw["app"], int(raw["pid"]), time.monotonic(), list(raw["screen"]), listed(raw["elements"])[:MAX_ELEMENTS],
                            raw.get("window") or "")
        self.snapshots[agent] = snapshot
        lines = [f"Screen {raw['screen'][0]}x{raw['screen'][1]} points. "
                 + (f"App: {raw['app']}" + ("" if raw.get("front") else " (not in front; desktop_act open_app brings it forward)"))
                 + (f", window「{raw['window']}」" if raw.get("window") else ", no window open")]
        others = [w for w in raw.get("windows") or [] if w and w != raw.get("window")]
        if others:
            lines.append("Its other windows: " + "、".join(others[:10]))
        if apps:
            lines.append("Running apps: " + ", ".join(raw.get("apps") or []))
        if snapshot.elements:
            lines.append(f"Elements ({len(snapshot.elements)}{', more not listed' if raw.get('truncated') else ''}; "
                         "use the number as desktop_act element, positions are screen points):")
            lines += [element_line(i, e) for i, e in enumerate(snapshot.elements, 1)]
        else:
            lines.append("No elements could be read in this window.")
        return snapshot, "\n".join(lines)

    def screenshot_text(self, snapshot: Snapshot) -> str:
        if self.describer is None:
            return "No screenshot: no image-describing model is set ([vision] provider)."
        name, describe = self.describer
        handle, path = tempfile.mkstemp(prefix="xh-screen-", suffix=getattr(self.backend, "image_suffix", ".jpg"))
        os.close(handle)
        try:
            self.backend.call({"op": "screenshot", "path": path, "width": snapshot.screen[0]})
            text = describe(path, (f"This is the person's screen, {snapshot.screen[0]}x{snapshot.screen[1]}; a position "
                                   "in this picture is a screen position. List the visible text and controls with their "
                                   "approximate x,y, then say what the screen is showing."))
        finally:
            os.unlink(path)     # the person's screen is not kept on disk
        return f"Screenshot, as described by {name}:\n{text.strip()}"


def typed_check(snapshot: Snapshot, text: str, replace: bool) -> str:
    """Say plainly whether the focused field now holds the text, so the model need not work it out from the
    element list (when the text landed wrong, granite kept retyping rather than say so)."""
    field = next((e for e in snapshot.elements if e.get("focused") and e.get("value")), None)
    if field is None:
        return ""
    flat = lambda t: " ".join(t.replace("\r", "\n").split())  # noqa: E731
    value, want = flat(field["value"]), flat(text)
    if (value == want) if replace else (want in value):
        return "Checked: the field now holds exactly that text. Do not type it again; go on with the next step.\n"
    if len(field["value"]) >= 120 and want[:60] in value:
        return "Checked: the field starts with that text. Do not type it again; go on with the next step.\n"
    return f"Checked: the field now reads 「{field['value'][:120]}」, which is not that text; look before trying again.\n"


def same_window(a: str | None, b: str | None) -> bool:
    """Same window title, or one side does not know it; an unsaved-changes mark ("*") does not count."""
    return not a or not b or a.lstrip("*") == b.lstrip("*")


def where_in(front: dict[str, Any]) -> str:
    """The app, and the window when the backend knows it: one Notepad window can hold several documents."""
    return f"{front.get('app')}" + (f"「{front['window'][:60]}」" if front.get("window") else "")


def _still_there(desktop: Desktop, target: dict[str, Any]) -> bool:
    """The element the person approved is still where it was: same role, text and place."""
    raw = desktop.backend.call({"op": "look", "app": "", "limit": MAX_ELEMENTS * 2})
    want = target["element"]
    same = lambda e: all(e.get(k) == want.get(k) for k in ("role", "subrole", "label", "rect"))  # noqa: E731
    return int(raw.get("pid", -1)) == target["pid"] and any(same(e) for e in listed(raw.get("elements") or []))


def _make_tools(desktop: Desktop, always_ask: bool, platform: str | None = None) -> list[Tool]:
    platform = platform or sys.platform
    refused = REFUSED_COMBOS.get(platform, {})

    def run_look(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        app = str(args.get("app") or "").strip()
        shot = bool(args.get("screenshot"))
        summary = f"desktop_look: read the screen ({app or 'the app in front'})" + (", with a screenshot" if shot else "")
        if always_ask and not ctx.approve(summary):     # reading is not a change: asked only when every call is
            return ToolResult("denied by approval policy", is_error=True)
        with desktop.lock:
            try:
                snapshot, text = desktop.look(ctx.agent, app)
                if shot:
                    text += "\n\n" + desktop.screenshot_text(snapshot)
            except (RuntimeError, OSError) as error:
                return ToolResult(f"desktop_look failed: {error}", is_error=True)
        return ToolResult(text)

    def point(args: dict[str, Any], ctx: ToolContext, front: dict[str, Any]) -> tuple[int, int, str]:
        snapshot = desktop.snapshots.get(ctx.agent)
        if args.get("element") is not None:
            index = int(args["element"])
            if snapshot is None:
                raise ValueError("call desktop_look first: element numbers come from it")
            if time.monotonic() - snapshot.taken > STALE_SECONDS or front.get("pid") != snapshot.pid \
                    or not same_window(front.get("window"), snapshot.window):
                raise ValueError(f"the screen changed since desktop_look ({front.get('app')} is in front now); look again")
            if not 1 <= index <= len(snapshot.elements):
                raise ValueError(f"no element {index}; the last look listed 1-{len(snapshot.elements)}")
            x, y, w, h = snapshot.elements[index - 1]["rect"]
            front["element"] = snapshot.elements[index - 1]     # checked again after the approval
            return x + w // 2, y + h // 2, f"{describe_element(snapshot, index)} in {where_in(front)}"
        if args.get("x") is None or args.get("y") is None:
            raise ValueError("give element (a number from desktop_look) or x and y")
        x, y = int(args["x"]), int(args["y"])
        if snapshot and not (0 <= x < snapshot.screen[0] * 3 and -snapshot.screen[1] * 2 <= y < snapshot.screen[1] * 3):
            raise ValueError(f"({x},{y}) is far outside the screen ({snapshot.screen[0]}x{snapshot.screen[1]})")
        return x, y, f"({x},{y}) in {where_in(front)}"

    def plan(args: dict[str, Any], ctx: ToolContext) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
        """(approval summary, backend requests, the app they are for) for one action.
        Raises ValueError for a malformed action, PermissionError for a refused one."""
        action = str(args.get("action") or "")
        if action == "open_app":
            app = str(args.get("app") or "").strip()
            if not app:
                raise ValueError("app is required for open_app")
            return f"desktop open_app: bring {app} to the front", [{"action": "open_app", "app": app}], None
        front = desktop.backend.call({"op": "focus"})
        if action in ("click", "double_click", "right_click"):
            x, y, where = point(args, ctx, front)
            count = 2 if action == "double_click" else max(1, min(int(args.get("count") or 1), MAX_CLICKS))
            return (f"desktop {action}: {where} at ({x},{y})",
                    [{"action": "click", "x": x, "y": y, "button": "right" if action == "right_click" else "left", "count": count}], front)
        if action == "type":
            text = str(args.get("text") or "")
            if not text:
                raise ValueError("text is required for type")
            if len(text) > MAX_TEXT:
                raise ValueError(f"text is limited to {MAX_TEXT} characters per call")
            steps, where = [], f"the focused field in {where_in(front)}"
            if args.get("element") is not None or args.get("x") is not None:
                x, y, where = point(args, ctx, front)
                steps.append({"action": "click", "x": x, "y": y, "button": "left", "count": 1})
            if args.get("replace"):
                steps.append(key_step("cmd+a"))
            steps.append({"action": "paste", "text": text})
            if args.get("submit"):
                steps.append(key_step("return"))
            shown = text if len(text) <= 120 else text[:120] + "…"
            return (f"desktop type into {where}{', replacing what is there' if args.get('replace') else ''}: 「{shown}」"
                    + (" then press return" if args.get("submit") else "")), steps, front
        if action == "key":
            _code, _flags, names = parse_combo(str(args.get("keys") or ""))
            if names in refused:
                raise PermissionError(f"refused: {args.get('keys')} {refused[names]}, which cannot be undone; "
                                      "ask the person to do it themselves")
            # A key that types a character is typing, and a password field refuses it like a paste.
            typing = not names & {"cmd", "ctrl", "win"} and any(len(n) == 1 or n == "space" for n in names)
            return (f"desktop key: {args.get('keys')} in {where_in(front)}",
                    [{**key_step(str(args.get("keys"))), "typing": typing}], front)
        if action == "scroll":
            direction = str(args.get("direction") or "down")
            if direction not in ("up", "down"):
                raise ValueError("direction is up or down")
            amount = max(1, min(int(args.get("amount") or 5), MAX_SCROLL))
            snapshot = desktop.snapshots.get(ctx.agent)
            if args.get("element") is not None or args.get("x") is not None:
                x, y, where = point(args, ctx, front)
            elif snapshot:
                x, y, where = snapshot.screen[0] // 2, snapshot.screen[1] // 2, f"the middle of the screen in {where_in(front)}"
            else:
                raise ValueError("call desktop_look first, or give x and y")
            return (f"desktop scroll {direction} {amount}: {where}",
                    [{"action": "scroll", "x": x, "y": y, "lines": amount if direction == "up" else -amount}], front)
        raise ValueError("action is one of: click, double_click, right_click, type, key, scroll, open_app")

    def run_act(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with desktop.lock:
            try:
                summary, steps, target = plan(args, ctx)
            except PermissionError as error:
                return ToolResult(str(error), is_error=True)
            except (ValueError, TypeError) as error:
                return ToolResult(str(error), is_error=True)
            except (RuntimeError, OSError) as error:
                return ToolResult(f"desktop_act failed: {error}", is_error=True)
            if not ctx.approve(summary):
                return ToolResult("denied by approval policy", is_error=True)
            try:
                if target is not None:
                    # Approving in a browser or the desktop window brings that to the front, and the
                    # keys would land there: put the app the action is for back in front, and check.
                    desktop.backend.call({"action": "activate", "pid": target["pid"], **({"window": target["window"]} if target.get("window") else {})})
                    time.sleep(desktop.settle / 2)
                    now = desktop.backend.call({"op": "focus"})
                    if now.get("elevated"):
                        return ToolResult(f"nothing was sent: {PERMISSION_HELP['elevated']}", is_error=True)
                    if now.get("pid") != target["pid"] or not same_window(now.get("window"), target.get("window")):
                        return ToolResult(f"nothing was sent: {target['app']} could not be brought back to the front "
                                          f"({now.get('app')} is in front); look again", is_error=True)
                    if target.get("element") is not None and not _still_there(desktop, target):
                        return ToolResult("nothing was sent: the element moved or changed while waiting for the "
                                          "approval; look again", is_error=True)
                for step in steps:
                    typing = step.pop("typing", False) or step["action"] == "paste"
                    if typing and desktop.backend.call({"op": "focus"}).get("secure"):
                        return ToolResult("refused: the focused field is a password field. Ask the person to type "
                                          "the password themselves.", is_error=True)
                    desktop.backend.call(step)
                time.sleep(desktop.settle)
                snapshot, after = desktop.look(ctx.agent, "", apps=False)
            except (RuntimeError, OSError) as error:
                return ToolResult(f"desktop_act failed: {error}", is_error=True)
        check = typed_check(snapshot, str(args.get("text") or ""), bool(args.get("replace"))) if args.get("action") == "type" else ""
        return ToolResult(f"Done: {summary.removeprefix('desktop ')}\n{check}\nThe screen now:\n{after}")

    return [
        Tool(
            name="desktop_look",
            description=(
                "See the person's screen: the app in front (or the app named), its window, and its buttons, fields, "
                "menus and text as numbered elements with positions. screenshot=true adds a description of a "
                "screenshot. Use before desktop_act; element numbers are only valid until the screen changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "App name, e.g. \"Finder\"; default the app in front"},
                    "screenshot": {"type": "boolean", "description": "Also describe a screenshot (slower)"},
                },
            },
            execute=run_look,
            always_ask=always_ask,
        ),
        Tool(
            name="desktop_act",
            description=(
                "Use the person's mouse and keyboard, then return the screen as it is afterwards. "
                "action: click / double_click / right_click (element number from desktop_look, or x and y), "
                "type (pastes text at the cursor of the focused field, adding to what is there; replace=true selects all of "
                "the field first so the text replaces it; with element it clicks the element first; submit=true presses return), "
                "key (keys like \"cmd+s\", \"return\", \"tab\", \"cmd+shift+4\"; cmd means ctrl on Windows, win is the "
                "Windows key), scroll (direction up/down, amount), "
                "open_app (app name). Each action is approved by the person. Never type passwords."
            ),
            mutating=True,
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["click", "double_click", "right_click", "type", "key", "scroll", "open_app"]},
                    "element": {"type": "integer", "description": "Element number from the last desktop_look"},
                    "x": {"type": "integer", "description": "Screen x in points, when there is no element"},
                    "y": {"type": "integer", "description": "Screen y in points"},
                    "text": {"type": "string", "description": "type: the text"},
                    "replace": {"type": "boolean", "description": "type: replace the field's whole content"},
                    "submit": {"type": "boolean", "description": "type: press return afterwards"},
                    "keys": {"type": "string", "description": "key: e.g. cmd+s"},
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "amount": {"type": "integer", "description": "scroll: lines, default 5"},
                    "count": {"type": "integer", "description": "click: 1-3"},
                    "app": {"type": "string", "description": "open_app: the app"},
                },
                "required": ["action"],
            },
            execute=run_act,
            always_ask=always_ask,
        ),
    ]


def desktop_control_plugin(always_ask: bool = True, describer: tuple[str, Callable[[str, str], str]] | None = None,
                           desktop: Desktop | None = None) -> Plugin:
    def _apply(ctx: Context, _config: Any) -> None:
        if desktop is None and sys.platform not in BACKENDS:
            print("xharness: [tools] desktop_control works on macOS and Windows; the desktop tools are off", file=sys.stderr)
            return
        register_tools(ctx, _make_tools(desktop or Desktop(describer=describer), always_ask))

    return Plugin("tool-desktop", _apply)
