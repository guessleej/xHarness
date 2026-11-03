"""Windows backend for the desktop tools (tools/desktop_control.py).

Run as its own process, one request per run: `python -I desktop_win.py '<json>'`
prints one JSON object, the same protocol as desktop_mac.py. A process of its
own because the screen is read through raw COM calls (ctypes), where one wrong
pointer ends the process -- the web server must not go down with it -- and the
caller kills a run that takes too long, which covers an app that stops
answering.

The screen is read through UI Automation, not from pixels: every button, field
and menu comes with its control type, its name and its position, so a model
that cannot see can still say what to click. Only what is on screen inside the
window is walked, and at most 200 children of any one element, which keeps a
folder with thousands of files from taking minutes.

Input goes through SendInput. Text is sent as Unicode characters
(KEYEVENTF_UNICODE): they do not go through the input method, so Zhuyin being
on does not turn "abc" into "ㄇ", and the clipboard is never touched. Keys
arrive by name; "cmd" means ctrl here.

Positions are physical pixels: the process declares itself per-monitor DPI
aware, so UI Automation rectangles, the mouse and screenshots agree on a
scaled (125%, 150%) screen.

Must run inside the person's own desktop session; a service or an SSH login
has no desktop to read. Standard library only.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as W
import json
import os
import struct
import sys
import time
import zlib
from typing import Any

HRESULT = ctypes.c_long
PTR = ctypes.c_void_p

CLSID_CUIAutomation = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
IID_IUIAutomation = "{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}"
IID_IUIAutomationValuePattern = "{a94cd8b1-0844-4cd6-9d2d-640537ab39e9}"
UIA_ValuePatternId = 10002

# Vtable slots (IUnknown takes 0-2).
AUTOMATION = {"ElementFromHandle": 6, "GetFocusedElement": 8, "ControlViewWalker": 14}
ELEMENT = {"GetCurrentPatternAs": 14, "ProcessId": 20, "ControlType": 21, "Name": 23, "HasKeyboardFocus": 26,
           "IsEnabled": 28, "HelpText": 31, "IsPassword": 35, "IsOffscreen": 38, "BoundingRectangle": 43}
WALKER = {"FirstChild": 4, "NextSibling": 6}
VALUE = {"CurrentValue": 4}

CONTROL_TYPES = dict(enumerate([
    "Button", "Calendar", "CheckBox", "ComboBox", "Edit", "Hyperlink", "Image", "ListItem", "List", "Menu", "MenuBar",
    "MenuItem", "ProgressBar", "RadioButton", "ScrollBar", "Slider", "Spinner", "StatusBar", "Tab", "TabItem", "Text",
    "ToolBar", "ToolTip", "Tree", "TreeItem", "Custom", "Group", "Thumb", "DataGrid", "DataItem", "Document",
    "SplitButton", "Window", "Pane", "Header", "HeaderItem", "Table", "TitleBar", "Separator", "SemanticZoom", "AppBar",
], start=50000))
#: Control types whose ValuePattern is worth reading (a document's value can be the whole file: it is cut).
VALUED = {"Edit", "ComboBox", "Document", "Spinner", "Slider", "DataItem"}
MAX_CHILDREN = 200

VK = {
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)}, **{str(d): 0x30 + d for d in range(10)},
    "return": 0x0D, "tab": 0x09, "space": 0x20, "delete": 0x08, "escape": 0x1B, "forward_delete": 0x2E,
    "home": 0x24, "end": 0x23, "page_up": 0x21, "page_down": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
    "=": 0xBB, "-": 0xBD, ",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE, "[": 0xDB, "]": 0xDD, "\\": 0xDC, "`": 0xC0,
}
MOD_VK = {"cmd": 0x11, "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
#: Keys that need KEYEVENTF_EXTENDEDKEY, or they arrive as the number pad's.
EXTENDED = {0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B}

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYUP, UNICODE, EXTENDEDKEY = 0x0002, 0x0004, 0x0001
MOUSE = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010)}
WHEEL = 0x0800


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD), ("time", W.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", W.DWORD), ("u", _INPUTUNION)]


class GUID(ctypes.Structure):
    _fields_ = [("a", W.DWORD), ("b", W.WORD), ("c", W.WORD), ("d", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    guid = GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None


def _setup() -> None:
    user32.GetForegroundWindow.restype = W.HWND
    user32.GetAncestor.restype = W.HWND
    user32.GetAncestor.argtypes = [W.HWND, W.UINT]
    user32.GetWindow.restype = W.HWND
    user32.GetWindow.argtypes = [W.HWND, W.UINT]
    user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [W.HWND]
    user32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    user32.IsWindowVisible.argtypes = [W.HWND]
    user32.IsIconic.argtypes = [W.HWND]
    user32.GetWindowLongW.argtypes = [W.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [W.HWND]
    user32.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
    user32.SendInput.argtypes = [W.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.OpenInputDesktop.restype = W.HANDLE
    user32.CloseDesktop.argtypes = [W.HANDLE]
    kernel32.OpenProcess.restype = W.HANDLE
    kernel32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel32.CloseHandle.argtypes = [W.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
    try:
        user32.SetProcessDpiAwarenessContext(PTR(-4))       # per-monitor v2: physical pixels everywhere
    except (AttributeError, OSError):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)


# COM through vtables

def _call(obj: int, slot: int, *args: Any, argtypes: tuple = ()) -> int:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(PTR))).contents
    function = ctypes.WINFUNCTYPE(HRESULT, PTR, *argtypes)(vtable[slot])
    return function(obj, *args)


def _release(obj: int | None) -> None:
    if obj:
        _call(obj, 2)


class UIA:
    def __init__(self) -> None:
        ctypes.oledll.ole32.CoInitializeEx(None, 2)
        out = PTR()
        clsid, iid = _guid(CLSID_CUIAutomation), _guid(IID_IUIAutomation)
        ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(out))
        self.automation = out.value
        walker = PTR()
        _call(self.automation, AUTOMATION["ControlViewWalker"], ctypes.byref(walker), argtypes=(ctypes.POINTER(PTR),))
        self.walker = walker.value

    def from_window(self, hwnd: int) -> int | None:
        out = PTR()
        _call(self.automation, AUTOMATION["ElementFromHandle"], W.HWND(hwnd), ctypes.byref(out),
              argtypes=(W.HWND, ctypes.POINTER(PTR)))
        return out.value

    def focused(self) -> int | None:
        out = PTR()
        if _call(self.automation, AUTOMATION["GetFocusedElement"], ctypes.byref(out), argtypes=(ctypes.POINTER(PTR),)) < 0:
            return None
        return out.value

    def _move(self, element: int, slot: int) -> int | None:
        out = PTR()
        if _call(self.walker, slot, PTR(element), ctypes.byref(out), argtypes=(PTR, ctypes.POINTER(PTR))) < 0:
            return None
        return out.value

    def children(self, element: int) -> list[int]:
        kids, child = [], self._move(element, WALKER["FirstChild"])
        while child and len(kids) < MAX_CHILDREN:
            kids.append(child)
            child = self._move(child, WALKER["NextSibling"])
        _release(child)
        return kids

    @staticmethod
    def _int(element: int, slot: int) -> int:
        out = ctypes.c_int()
        return out.value if _call(element, ELEMENT[slot], ctypes.byref(out), argtypes=(ctypes.POINTER(ctypes.c_int),)) >= 0 else 0

    @staticmethod
    def _text(element: int, slot: int) -> str:
        out = PTR()
        if _call(element, ELEMENT[slot], ctypes.byref(out), argtypes=(ctypes.POINTER(PTR),)) < 0 or not out.value:
            return ""
        try:
            return ctypes.wstring_at(out.value)
        finally:
            ctypes.windll.oleaut32.SysFreeString(out)

    def rect(self, element: int) -> tuple[int, int, int, int] | None:
        rect = W.RECT()
        if _call(element, ELEMENT["BoundingRectangle"], ctypes.byref(rect), argtypes=(ctypes.POINTER(W.RECT),)) < 0:
            return None
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def value(self, element: int) -> str:
        pattern, iid = PTR(), _guid(IID_IUIAutomationValuePattern)
        if _call(element, ELEMENT["GetCurrentPatternAs"], UIA_ValuePatternId, ctypes.byref(iid), ctypes.byref(pattern),
                 argtypes=(ctypes.c_int, ctypes.POINTER(GUID), ctypes.POINTER(PTR))) < 0 or not pattern.value:
            return ""
        try:
            out = PTR()
            if _call(pattern.value, VALUE["CurrentValue"], ctypes.byref(out), argtypes=(ctypes.POINTER(PTR),)) < 0 or not out.value:
                return ""
            text = ctypes.wstring_at(out.value)
            ctypes.windll.oleaut32.SysFreeString(out)
            return text
        finally:
            _release(pattern.value)

    def describe(self, element: int, rect: tuple[int, int, int, int]) -> dict[str, Any]:
        role = CONTROL_TYPES.get(self._int(element, "ControlType"), "Unknown")
        secret = bool(self._int(element, "IsPassword"))
        value = "(hidden)" if secret else (self.value(element)[:120] if role in VALUED else "")
        return {
            "role": role, "subrole": "Password" if secret else "",
            "label": self._text(element, "Name") or self._text(element, "HelpText"),
            "value": value, "rect": list(rect),
            "enabled": bool(self._int(element, "IsEnabled")), "focused": bool(self._int(element, "HasKeyboardFocus")),
        }


def _walk(uia: UIA, window: int, limit: int, budget: float) -> tuple[list[dict[str, Any]], bool]:
    """Breadth-first over what is on screen inside `window`, children before grandchildren."""
    start = time.monotonic()
    wx, wy, ww, wh = uia.rect(window) or (0, 0, 10 ** 5, 10 ** 5)
    queue, out, truncated, held = [window], [], False, []
    while queue:
        if len(out) >= limit or time.monotonic() - start > budget:
            truncated = True
            break
        for element in uia.children(queue.pop(0)):
            held.append(element)
            rect = uia.rect(element)
            if not rect or uia._int(element, "IsOffscreen"):
                continue
            x, y, w, h = rect
            if w < 2 or h < 2 or x + w < wx or y + h < wy or x > wx + ww or y > wy + wh:
                continue    # scrolled out of view, or outside the window: neither it nor its children show
            queue.append(element)
            out.append(uia.describe(element, rect))
    for element in held:
        _release(element)
    return out, truncated


# Windows and processes

def _desktop_ok() -> bool:
    """False when the screen is locked or this process has no interactive desktop (a service, an SSH login)."""
    desk = user32.OpenInputDesktop(0, False, 0x0100)     # DESKTOP_SWITCHDESKTOP
    if not desk:
        return False
    user32.CloseDesktop(desk)
    return bool(user32.GetForegroundWindow())


def _pid(hwnd: int) -> int:
    pid = W.DWORD()
    user32.GetWindowThreadProcessId(W.HWND(hwnd), ctypes.byref(pid))
    return pid.value


def _title(hwnd: int) -> str:
    size = user32.GetWindowTextLengthW(W.HWND(hwnd)) + 1
    buffer = ctypes.create_unicode_buffer(size)
    user32.GetWindowTextW(W.HWND(hwnd), buffer, size)
    return buffer.value


def _exe(pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = W.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        return buffer.value if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(handle)


def _cloaked(hwnd: int) -> bool:
    """Store apps keep hidden (cloaked) windows around; they are not on screen."""
    value = ctypes.c_int()
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(W.HWND(hwnd), 14, ctypes.byref(value), ctypes.sizeof(value))
    except OSError:
        return False
    return bool(value.value)


def _app_name(hwnd: int, pid: int) -> str:
    name = os.path.splitext(os.path.basename(_exe(pid)))[0]
    # Store apps all run in ApplicationFrameHost; their window title is the app's name.
    return _title(hwnd) if name.lower() == "applicationframehost" else (name or _title(hwnd))


def _windows() -> list[dict[str, Any]]:
    """Top-level windows a person would call open, front to back."""
    found: list[dict[str, Any]] = []
    proc = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

    def visit(hwnd: int, _param: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4) or _cloaked(hwnd):      # GW_OWNER
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80 or not _title(hwnd):                          # WS_EX_TOOLWINDOW
            return True
        pid = _pid(hwnd)
        found.append({"hwnd": hwnd, "pid": pid, "title": _title(hwnd), "name": _app_name(hwnd, pid)})
        return True

    user32.EnumWindows(proc(visit), 0)
    return found


def _front() -> dict[str, Any] | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    hwnd = user32.GetAncestor(hwnd, 3) or hwnd                  # GA_ROOTOWNER
    pid = _pid(hwnd)
    return {"hwnd": hwnd, "pid": pid, "title": _title(hwnd), "name": _app_name(hwnd, pid)}


def _elevated(pid: int) -> bool:
    """The process runs as administrator; input from a process that does not is dropped (UIPI)."""
    def token_elevated(process: int) -> bool | None:
        token, value, size = W.HANDLE(), W.DWORD(), W.DWORD()
        if not ctypes.windll.advapi32.OpenProcessToken(W.HANDLE(process), 0x0008, ctypes.byref(token)):   # TOKEN_QUERY
            return None
        try:
            ok = ctypes.windll.advapi32.GetTokenInformation(token, 20, ctypes.byref(value), 4, ctypes.byref(size))
            return bool(value.value) if ok else None
        finally:
            kernel32.CloseHandle(token)

    if token_elevated(kernel32.GetCurrentProcess()):
        return False                                             # an elevated sender reaches everything
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        state = token_elevated(handle)
    finally:
        kernel32.CloseHandle(handle)
    return state is None or state                                # a token this process may not read is an elevated one


def _screen() -> list[int]:
    return [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)]


# Requests

def look(request: dict[str, Any]) -> dict[str, Any]:
    if not _desktop_ok():
        return {"error": "no-desktop"}
    windows, front = _windows(), _front()
    wanted = str(request.get("app") or "").casefold()
    if wanted:
        target = next((w for w in windows if w["name"].casefold() == wanted), None) or \
            next((w for w in windows if wanted in w["title"].casefold()), None)
        if target is None:
            return {"error": f"no open window of an app named {request.get('app')}"}
    else:
        target = front
    if target is None:
        return {"error": "no app is in front"}
    uia = UIA()
    window = uia.from_window(target["hwnd"])
    elements, truncated = _walk(uia, window, int(request.get("limit") or 150), float(request.get("budget") or 6)) if window else ([], False)
    rect = uia.rect(window) if window else None
    _release(window)
    names = []
    for w in windows:
        if w["name"] not in names:
            names.append(w["name"])
    return {
        "screen": _screen(), "app": target["name"], "pid": target["pid"],
        "front": bool(front) and front["pid"] == target["pid"], "apps": names,
        "window": target["title"], "window_rect": list(rect) if rect else None,
        "windows": [w["title"] for w in windows if w["pid"] == target["pid"]],
        "elements": elements, "truncated": truncated,
    }


def focus(_request: dict[str, Any]) -> dict[str, Any]:
    """The app in front, whether the focused element is a password field, and whether the app runs elevated."""
    if not _desktop_ok():
        return {"error": "no-desktop"}
    front = _front()
    if front is None:
        return {"error": "no-desktop"}
    uia = UIA()
    element = uia.focused()
    secure = bool(element) and bool(UIA._int(element, "IsPassword"))
    _release(element)
    return {"app": front["name"], "pid": front["pid"], "window": front["title"], "secure": secure,
            "elevated": _elevated(front["pid"])}


def _send(*inputs: INPUT) -> None:
    array = (INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)) != len(inputs):
        raise RuntimeError(f"SendInput was blocked (error {ctypes.get_last_error()}); the screen may be locked")


def _key(vk: int = 0, up: bool = False, char: int = 0) -> INPUT:
    flags = (KEYUP if up else 0) | (UNICODE if char else 0) | (EXTENDEDKEY if vk in EXTENDED else 0)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, char, flags, 0, 0)))


def _mouse(flags: int, data: int = 0) -> INPUT:
    return INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, ctypes.c_uint32(data).value, flags, 0, 0)))


def _move_to(x: int, y: int) -> None:
    if not user32.SetCursorPos(int(x), int(y)):
        raise RuntimeError("the mouse could not be moved; the screen may be locked")
    time.sleep(0.05)


def press(mods: list[str], key: str) -> None:
    code = VK.get(key.upper() if len(key) == 1 and key.isalpha() else key)
    if code is None:
        raise ValueError(f"unknown key: {key}")
    held = [MOD_VK[m] for m in mods if m in MOD_VK]
    _send(*[_key(m) for m in held], _key(code), _key(code, up=True), *[_key(m, up=True) for m in reversed(held)])


def type_text(text: str) -> None:
    """Unicode characters straight to the focused field: no input method, no clipboard."""
    for line_no, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        if line_no:
            _send(_key(VK["return"]), _key(VK["return"], up=True))
        units = struct.unpack(f"<{len(line.encode('utf-16-le')) // 2}H", line.encode("utf-16-le"))
        for start in range(0, len(units), 64):
            _send(*[k for unit in units[start:start + 64] for k in (_key(char=unit), _key(char=unit, up=True))])


def activate(pid: int, title: str = "") -> None:
    mine = [w for w in _windows() if w["pid"] == pid]
    window = next((w for w in mine if title and w["title"].lstrip("*") == title.lstrip("*")), None) or (mine[0] if mine else None)
    if window is None:
        raise RuntimeError("that app has no open window")
    hwnd = window["hwnd"]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                              # SW_RESTORE
    # Windows lets only the program with the last input change the front window; one ALT tap makes it ours.
    _send(_key(MOD_VK["alt"]), _key(MOD_VK["alt"], up=True))
    user32.SetForegroundWindow(hwnd)


def open_app(name: str) -> None:
    running = next((w for w in _windows() if w["name"].casefold() == name.casefold()), None)
    if running:
        activate(running["pid"])
        return
    # ShellExecute resolves App Paths (notepad, msedge, winword...) the way the Run box does; no shell is involved.
    result = ctypes.windll.shell32.ShellExecuteW(None, "open", name, None, None, 1)
    if result <= 32:
        raise RuntimeError(f"Windows could not open {name} (code {result}); use the program's name, e.g. notepad or msedge")
    # A cold start takes a moment: wait for its window to come to the front, so the next look sees it.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        front = _front()
        if front and (front["name"].casefold() == name.casefold() or name.casefold() in front["title"].casefold()):
            return
        time.sleep(0.2)


def act(request: dict[str, Any]) -> dict[str, Any]:
    if not _desktop_ok():
        return {"error": "no-desktop"}
    action = request["action"]
    if action == "open_app":
        open_app(str(request["app"]))
    elif action == "activate":
        activate(int(request["pid"]), str(request.get("window") or ""))
    elif action == "click":
        _move_to(request["x"], request["y"])
        down, up = MOUSE["right" if request.get("button") == "right" else "left"]
        for _ in range(max(1, int(request.get("count") or 1))):
            _send(_mouse(down), _mouse(up))
    elif action == "key":
        press(list(request.get("mods") or []), str(request["key"]))
    elif action == "paste":
        type_text(str(request["text"]))
    elif action == "scroll":
        _move_to(request["x"], request["y"])
        _send(_mouse(WHEEL, int(request["lines"]) * 120))
    else:
        raise ValueError(f"unknown action {action}")
    return {}


def _png(path: str, width: int, height: int, bgra: bytes) -> None:
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    line = width * 3
    rows = b"".join(b"\x00" + rgb[y * line:(y + 1) * line] for y in range(height))      # filter 0 on every row

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def screenshot(request: dict[str, Any]) -> dict[str, Any]:
    """The primary screen at its own pixel size, so a position in the picture is a position to click."""
    if not _desktop_ok():
        return {"error": "no-desktop"}
    width, height = _screen()
    gdi = ctypes.windll.gdi32
    for name, res, args in (("CreateCompatibleDC", W.HDC, [W.HDC]), ("CreateCompatibleBitmap", W.HBITMAP, [W.HDC, ctypes.c_int, ctypes.c_int]),
                            ("SelectObject", W.HGDIOBJ, [W.HDC, W.HGDIOBJ]), ("DeleteObject", W.BOOL, [W.HGDIOBJ]),
                            ("DeleteDC", W.BOOL, [W.HDC]),
                            ("BitBlt", W.BOOL, [W.HDC] + [ctypes.c_int] * 4 + [W.HDC, ctypes.c_int, ctypes.c_int, W.DWORD]),
                            ("GetDIBits", ctypes.c_int, [W.HDC, W.HBITMAP, W.UINT, W.UINT, PTR, PTR, W.UINT])):
        getattr(gdi, name).restype, getattr(gdi, name).argtypes = res, args
    user32.GetDC.restype, user32.GetDC.argtypes = W.HDC, [W.HWND]
    user32.ReleaseDC.argtypes = [W.HWND, W.HDC]
    screen = user32.GetDC(None)
    memory = gdi.CreateCompatibleDC(screen)
    bitmap = gdi.CreateCompatibleBitmap(screen, width, height)
    try:
        gdi.SelectObject(memory, bitmap)
        if not gdi.BitBlt(memory, 0, 0, width, height, screen, 0, 0, 0x00CC0020 | 0x40000000):    # SRCCOPY | CAPTUREBLT
            raise RuntimeError("the screen could not be copied")
        header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)     # top-down 32-bit
        info = ctypes.create_string_buffer(header + b"\x00" * 16)
        pixels = ctypes.create_string_buffer(width * height * 4)
        if not gdi.GetDIBits(memory, bitmap, 0, height, ctypes.cast(pixels, PTR), ctypes.cast(info, PTR), 0):
            raise RuntimeError("the screen could not be read")
        _png(str(request["path"]), width, height, pixels.raw)
    finally:
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory)
        user32.ReleaseDC(None, screen)
    return {"path": request["path"]}


def selftest(_request: dict[str, Any]) -> dict[str, Any]:
    """Checks the COM slots against the desktop itself: the root of the front window must read as a window."""
    if not _desktop_ok():
        return {"error": "no-desktop"}
    front = _front()
    uia = UIA()
    window = uia.from_window(front["hwnd"])
    role = CONTROL_TYPES.get(UIA._int(window, "ControlType"), "?")
    rect = uia.rect(window)
    _release(window)
    return {"app": front["name"], "role": role, "rect": list(rect) if rect else None, "screen": _screen()}


def main() -> None:
    request = json.loads(sys.argv[1])
    _setup()
    handler = {"look": look, "focus": focus, "screenshot": screenshot, "selftest": selftest}.get(request.get("op"), act)
    try:
        result = handler(request)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        result = {"error": str(error)[:400]}
    sys.stdout.write(json.dumps(result))      # ASCII only: the pipe's encoding differs by system locale


if __name__ == "__main__":
    main()
