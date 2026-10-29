"""macOS backend for the desktop tools (tools/desktop_control.py).

Run as its own process, one request per run: `python -I desktop_mac.py '<json>'`
prints one JSON object. Why a process of its own: the screen is read through
ctypes calls into ApplicationServices, and an Objective-C exception in there
(an app that quits while it is being read, say) aborts the whole process --
the web server must not go down with it. The caller also kills a run that
takes too long, which covers an app that stops answering.

The screen is read through the Accessibility API, not from pixels: every
button, field and menu comes with its role, its label and its position, so a
model that cannot see can still say what to click. Only what is visible is
walked (AXVisibleChildren where an element has it, and nothing outside the
window), which is what keeps a long chat history from taking minutes.

Input goes through Quartz events from a small JXA script. Text is pasted, not
typed key by key: typed keys go through the input method, and with Zhuyin on
"abc" arrives as "ㄇ". The clipboard is saved before and put back after.

Needs the Accessibility permission for whatever app runs xharness (Terminal,
or the desktop window's Python), and Screen Recording for screenshots.
Standard library only.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import subprocess  # nosec B404 - fixed system tools: osascript, screencapture, sips, open
import sys
import time
from typing import Any

V = ctypes.c_void_p
UTF8 = 0x08000100
ERR_NO_VALUE = -25212

ACTIONS_JS = r"""
ObjC.import('AppKit'); ObjC.import('CoreGraphics');
function post(e) { $.CGEventPost($.kCGHIDEventTap, e); }
function key(code, flags) {
  for (const down of [true, false]) {
    const e = $.CGEventCreateKeyboardEvent($(), code, down);
    $.CGEventSetFlags(e, flags);
    post(e);
  }
}
function run(argv) {
  const a = JSON.parse(argv[0]);
  if (a.action === 'apps') {
    const se = Application('System Events'), f = $.NSScreen.mainScreen.frame;
    const procs = se.processes.whose({backgroundOnly: false});
    const names = procs.name(), pids = procs.unixId(), front = procs.frontmost();
    return JSON.stringify({screen: [f.size.width, f.size.height],
      apps: names.map((n, i) => ({name: n, pid: pids[i], front: front[i]}))});
  }
  if (a.action === 'click') {
    const pt = $.CGPointMake(a.x, a.y);
    const [down, up, button] = a.button === 'right'
      ? [$.kCGEventRightMouseDown, $.kCGEventRightMouseUp, $.kCGMouseButtonRight]
      : [$.kCGEventLeftMouseDown, $.kCGEventLeftMouseUp, $.kCGMouseButtonLeft];
    post($.CGEventCreateMouseEvent($(), $.kCGEventMouseMoved, pt, 0));
    delay(0.05);
    for (let n = 1; n <= a.count; n++) {
      for (const t of [down, up]) {
        const e = $.CGEventCreateMouseEvent($(), t, pt, button);
        $.CGEventSetIntegerValueField(e, $.kCGMouseEventClickState, n);
        post(e);
      }
    }
    return '{}';
  }
  if (a.action === 'key') { key(a.code, a.flags); return '{}'; }
  if (a.action === 'activate') {
    const p = Application('System Events').processes.whose({unixId: a.pid});
    if (p.length) p[0].frontmost = true;
    return '{}';
  }
  if (a.action === 'scroll') {
    post($.CGEventCreateMouseEvent($(), $.kCGEventMouseMoved, $.CGPointMake(a.x, a.y), 0));
    delay(0.05);
    post($.CGEventCreateScrollWheelEvent($(), $.kCGScrollEventUnitLine, 1, a.lines));
    return '{}';
  }
  if (a.action === 'paste') {
    const pb = $.NSPasteboard.generalPasteboard, saved = [], items = pb.pasteboardItems;
    for (let i = 0; i < items.count; i++) {
      const item = items.objectAtIndex(i), types = item.types, entry = [];
      for (let j = 0; j < types.count; j++) {
        const t = types.objectAtIndex(j), d = item.dataForType(t);
        if (d) entry.push([t, d]);
      }
      saved.push(entry);
    }
    pb.clearContents;
    pb.setStringForType($(a.text), $.NSPasteboardTypeString);
    key(9, $.kCGEventFlagMaskCommand);
    delay(0.4);
    pb.clearContents;
    const back = saved.map(entry => {
      const item = $.NSPasteboardItem.alloc.init;
      entry.forEach(([t, d]) => item.setDataForType(d, t));
      return item;
    });
    if (back.length) pb.writeObjects($(back));
    return '{}';
  }
  throw new Error('unknown action ' + a.action);
}
"""


class _AX:
    """The few CoreFoundation / Accessibility calls the walk needs."""

    def __init__(self) -> None:
        self.cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        self.ax = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
        for lib, name, res, args in [
            (self.cf, "CFStringCreateWithCString", V, [V, ctypes.c_char_p, ctypes.c_uint32]),
            (self.cf, "CFStringGetCString", ctypes.c_bool, [V, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]),
            (self.cf, "CFStringGetLength", ctypes.c_long, [V]),
            (self.cf, "CFGetTypeID", ctypes.c_ulong, [V]),
            (self.cf, "CFStringGetTypeID", ctypes.c_ulong, []),
            (self.cf, "CFBooleanGetTypeID", ctypes.c_ulong, []),
            (self.cf, "CFNumberGetTypeID", ctypes.c_ulong, []),
            (self.cf, "CFArrayGetCount", ctypes.c_long, [V]),
            (self.cf, "CFArrayGetValueAtIndex", V, [V, ctypes.c_long]),
            (self.cf, "CFBooleanGetValue", ctypes.c_bool, [V]),
            (self.cf, "CFNumberGetValue", ctypes.c_bool, [V, ctypes.c_int, V]),
            (self.cf, "CFRetain", V, [V]),
            (self.cf, "CFRelease", None, [V]),
            (self.ax, "AXIsProcessTrusted", ctypes.c_bool, []),
            (self.ax, "AXUIElementCreateApplication", V, [ctypes.c_int]),
            (self.ax, "AXUIElementCreateSystemWide", V, []),
            (self.ax, "AXUIElementCopyAttributeValue", ctypes.c_int, [V, V, ctypes.POINTER(V)]),
            (self.ax, "AXUIElementCopyAttributeValues", ctypes.c_int, [V, V, ctypes.c_long, ctypes.c_long, ctypes.POINTER(V)]),
            (self.ax, "AXUIElementSetMessagingTimeout", ctypes.c_int, [V, ctypes.c_float]),
            (self.ax, "AXValueGetValue", ctypes.c_bool, [V, ctypes.c_int, V]),
            (self.ax, "CGPreflightScreenCaptureAccess", ctypes.c_bool, []),
        ]:
            function = getattr(lib, name)
            function.restype, function.argtypes = res, args
        self._names: dict[str, int] = {}

    def name(self, text: str) -> int:
        if text not in self._names:
            self._names[text] = self.cf.CFStringCreateWithCString(None, text.encode(), UTF8)
        return self._names[text]

    def attr(self, element: int, name: str) -> int | None:
        out = V()
        if self.ax.AXUIElementCopyAttributeValue(element, self.name(name), ctypes.byref(out)) != 0:
            return None
        return out.value

    def value(self, element: int, name: str) -> Any:
        """A string, number or bool attribute as Python; anything else (or missing) as None."""
        ref = self.attr(element, name)
        if not ref:
            return None
        try:
            kind = self.cf.CFGetTypeID(ref)
            if kind == self.cf.CFStringGetTypeID():
                size = self.cf.CFStringGetLength(ref) * 4 + 1
                buffer = ctypes.create_string_buffer(size)
                return buffer.value.decode("utf-8", "replace") if self.cf.CFStringGetCString(ref, buffer, size, UTF8) else ""
            if kind == self.cf.CFBooleanGetTypeID():
                return bool(self.cf.CFBooleanGetValue(ref))
            if kind == self.cf.CFNumberGetTypeID():
                number = ctypes.c_double()
                self.cf.CFNumberGetValue(ref, 13, ctypes.byref(number))  # kCFNumberDoubleType
                return number.value
            return None
        finally:
            self.cf.CFRelease(ref)

    def array(self, element: int, name: str, limit: int) -> list[int] | None:
        out = V()
        if self.ax.AXUIElementCopyAttributeValues(element, self.name(name), 0, limit, ctypes.byref(out)) != 0 or not out.value:
            return None
        try:
            count = self.cf.CFArrayGetCount(out.value)
            return [self.cf.CFRetain(self.cf.CFArrayGetValueAtIndex(out.value, i)) for i in range(count)]
        finally:
            self.cf.CFRelease(out.value)

    def rect(self, element: int) -> tuple[float, float, float, float] | None:
        class Pair(ctypes.Structure):
            _fields_ = [("a", ctypes.c_double), ("b", ctypes.c_double)]

        position, size = self.attr(element, "AXPosition"), self.attr(element, "AXSize")
        point, extent = Pair(), Pair()
        try:
            ok = (position and size and self.ax.AXValueGetValue(position, 1, ctypes.byref(point))
                  and self.ax.AXValueGetValue(size, 2, ctypes.byref(extent)))
        finally:
            for ref in (position, size):
                if ref:
                    self.cf.CFRelease(ref)
        return (point.a, point.b, extent.a, extent.b) if ok else None


def _run(argv: list[str], timeout: float = 20) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)  # nosec B603 - fixed argv, no shell
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip()[:400] or f"{argv[0]} failed")
    return done.stdout


def _js(request: dict[str, Any]) -> dict[str, Any]:
    return json.loads(_run(["/usr/bin/osascript", "-l", "JavaScript", "-e", ACTIONS_JS, json.dumps(request)]) or "{}")


def _walk(ax: _AX, window: int, limit: int, budget: float) -> tuple[list[dict[str, Any]], bool]:
    """Breadth-first over what is visible inside `window`, children before grandchildren."""
    start = time.monotonic()
    bounds = ax.rect(window) or (0, 0, 1e5, 1e5)
    wx, wy, ww, wh = bounds
    queue, out, truncated = [window], [], False
    while queue:
        if len(out) >= limit or time.monotonic() - start > budget:
            truncated = True
            break
        parent = queue.pop(0)
        kids = None
        for attribute in ("AXVisibleChildren", "AXVisibleRows"):
            kids = ax.array(parent, attribute, 80)
            if kids:
                break
        kids = kids or ax.array(parent, "AXChildren", 200) or []
        for element in kids:
            rect = ax.rect(element)
            if not rect:
                continue
            x, y, w, h = rect
            if w < 2 or h < 2 or x + w < wx or y + h < wy or x > wx + ww or y > wy + wh:
                continue    # scrolled out of view, or outside the window: neither it nor its children show
            queue.append(element)
            subrole = ax.value(element, "AXSubrole") or ""
            value = ax.value(element, "AXValue")
            out.append({
                "role": ax.value(element, "AXRole") or "",
                "subrole": subrole,
                "label": ax.value(element, "AXTitle") or ax.value(element, "AXDescription") or ax.value(element, "AXHelp") or "",
                "value": "(hidden)" if subrole == "AXSecureTextField" else ("" if value is None else str(value)[:120]),
                "rect": [round(v) for v in rect],
                "enabled": ax.value(element, "AXEnabled") is not False,
                "focused": ax.value(element, "AXFocused") is True,
            })
    return out, truncated


def look(request: dict[str, Any]) -> dict[str, Any]:
    ax = _AX()
    if not ax.ax.AXIsProcessTrusted():
        return {"error": "no-accessibility"}
    listing = _js({"action": "apps"})
    apps = listing["apps"]
    wanted = str(request.get("app") or "").casefold()
    target = next((a for a in apps if wanted and a["name"].casefold() == wanted), None) if wanted else \
        next((a for a in apps if a["front"]), None)
    if target is None:
        return {"error": f"no running app named {request.get('app')}" if wanted else "no app is in front"}
    app = ax.ax.AXUIElementCreateApplication(int(target["pid"]))
    ax.ax.AXUIElementSetMessagingTimeout(app, 1.5)
    windows = ax.array(app, "AXWindows", 30) or []
    window = ax.attr(app, "AXFocusedWindow") or ax.attr(app, "AXMainWindow") or (windows[0] if windows else None)
    elements, truncated = _walk(ax, window, int(request.get("limit") or 150), float(request.get("budget") or 6)) if window else ([], False)
    return {
        "screen": [round(v) for v in listing["screen"]],
        "app": target["name"], "pid": target["pid"], "front": bool(target["front"]),
        "apps": [a["name"] for a in apps],
        "window": (ax.value(window, "AXTitle") or "") if window else None,
        "window_rect": [round(v) for v in (ax.rect(window) or (0, 0, 0, 0))] if window else None,
        "windows": [ax.value(w, "AXTitle") or "" for w in windows],
        "elements": elements, "truncated": truncated,
    }


def focus(_request: dict[str, Any]) -> dict[str, Any]:
    """The app in front and whether the focused element is a password field."""
    ax = _AX()
    if not ax.ax.AXIsProcessTrusted():
        return {"error": "no-accessibility"}
    front = next((a for a in _js({"action": "apps"})["apps"] if a["front"]), None)
    element = ax.attr(ax.ax.AXUIElementCreateSystemWide(), "AXFocusedUIElement")
    secure = bool(element) and ax.value(element, "AXSubrole") == "AXSecureTextField"
    return {"app": front and front["name"], "pid": front and front["pid"], "secure": secure}


def screenshot(request: dict[str, Any]) -> dict[str, Any]:
    if not _AX().ax.CGPreflightScreenCaptureAccess():
        return {"error": "no-screen-recording"}
    path = str(request["path"])
    _run(["/usr/sbin/screencapture", "-x", "-m", "-t", "jpg", path])
    # Scale to screen points, so a position in the picture is a position to click.
    _run(["/usr/bin/sips", "--resampleWidth", str(int(request["width"])), path])
    return {"path": path}


def act(request: dict[str, Any]) -> dict[str, Any]:
    if not _AX().ax.AXIsProcessTrusted():
        return {"error": "no-accessibility"}
    if request["action"] == "open_app":
        _run(["/usr/bin/open", "-a", str(request["app"])])
        return {}
    return _js(request)


def main() -> None:
    request = json.loads(sys.argv[1])
    handler = {"look": look, "focus": focus, "screenshot": screenshot}.get(request.get("op"), act)
    try:
        result = handler(request)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        result = {"error": str(error)[:400]}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
