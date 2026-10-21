"""`@path` in a request attaches that file's text to it.

"Fix the bug in @src/app.py" saves the model a search and a read, and makes
sure it looks at the file the person meant. Only files inside the working
directory are attached (no `@../../etc/passwd`), text only, within size
limits. What was attached travels as context with the request, so it is in
the session log like everything else the model sees.
"""

from __future__ import annotations

import os
import re
from typing import Any

MENTION = re.compile(r"(?<![\w@./])@([^\s@,，。;；:：)）\]]+)")
MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 60_000
MAX_FILES = 8
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".xharness", ".mypy_cache", ".pytest_cache", "dist", "build"}
MAX_LISTED = 4000


def inside(root: str, path: str) -> bool:
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    return real == real_root or real.startswith(real_root + os.sep)


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def expand(text: str, cwd: str) -> tuple[str | None, list[str], list[str]]:
    """(context to send with the request, attached relative paths, image paths).
    An @-mentioned picture is passed on as an image, not as text."""
    attached: list[str] = []
    images: list[str] = []
    parts: list[str] = []
    notes: list[str] = []
    total = 0
    for match in MENTION.finditer(text or ""):
        rel = match.group(1).rstrip(".")
        if rel in attached or len(attached) >= MAX_FILES:
            continue
        path = os.path.join(cwd, rel)
        if not inside(cwd, path) or not os.path.isfile(path):
            continue
        if rel.lower().endswith(IMAGE_EXTENSIONS):
            if len(images) < 4:
                images.append(os.path.realpath(path))
                attached.append(rel)
            continue
        try:
            with open(path, "rb") as handle:
                raw = handle.read(MAX_FILE_CHARS * 4 + 1)
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            notes.append(f"{rel} is a binary file and was not attached")
            continue
        body = raw.decode("utf-8", errors="replace")
        if len(body) > MAX_FILE_CHARS:
            body = body[:MAX_FILE_CHARS] + "\n[file truncated; read it for the rest]"
        if total + len(body) > MAX_TOTAL_CHARS:
            notes.append(f"{rel} was not attached (attachments are limited to {MAX_TOTAL_CHARS:,} characters)")
            continue
        total += len(body)
        attached.append(rel)
        parts.append(f"### {rel}\n```\n{body}\n```")
    if not parts and not notes:
        return None, attached, images
    context = "Files the person attached with @ (current contents):\n\n" + "\n\n".join(parts)
    if notes:
        context += "\n\n" + "\n".join(notes)
    return context, attached, images


def list_files(cwd: str, query: str = "", limit: int = 30) -> list[dict[str, Any]]:
    """Files under the working directory for the @ picker, best matches first."""
    needle = (query or "").lower()
    found: list[tuple[int, str, int]] = []
    seen = 0
    for current, dirs, names in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            seen += 1
            if seen > MAX_LISTED:
                break
            rel = os.path.relpath(os.path.join(current, name), cwd)
            lowered = rel.lower()
            if needle and needle not in lowered:
                continue
            rank = 0 if os.path.basename(lowered).startswith(needle) else 1 if needle in os.path.basename(lowered) else 2
            try:
                size = os.path.getsize(os.path.join(current, name))
            except OSError:
                size = 0
            found.append((rank, rel, size))
        if seen > MAX_LISTED:
            break
    found.sort(key=lambda item: (item[0], len(item[1]), item[1]))
    return [{"path": rel, "bytes": size} for _rank, rel, size in found[:limit]]
