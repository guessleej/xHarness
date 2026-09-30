"""Per-user working directories and the files people upload into them.

Without [auth] an agent works in the directory the server was started from,
which is what a single operator wants: their own project, their own files.

With [auth] that directory would be shared by everyone signed in -- one
teacher's `write` would be another teacher's `read`. So each person gets a
working directory of their own, and the files they upload from the browser
land inside it. The agent's cwd is that directory, so `read`, `write`,
`glob` and `bash` all stay inside it without any tool needing to know that
accounts exist.

Uploaded names are never trusted: a stored file keeps a sanitised basename,
and the path it lands on is always verified to be inside the user's uploads
directory before a byte is written.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .session import SAFE_USER, harness_home

UPLOADS = "uploads"
DEFAULT_MAX_FILE_MB = 25
DEFAULT_MAX_TOTAL_MB = 500
DEFAULT_RETENTION_DAYS = 0  # 0 = keep until someone deletes it
MAX_NAME_LENGTH = 120
#: Deliberately a small allow-list: documents, data and images a school
#: actually hands to an assistant. Anything executable is absent on purpose.
DEFAULT_EXTENSIONS = (
    ".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".log",
    ".pdf", ".doc", ".docx", ".odt", ".rtf",
    ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic",
    ".zip", ".py", ".js", ".ts", ".html", ".css", ".sql", ".sh", ".toml", ".ini",
)
UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


def workspaces_root() -> str:
    return os.path.join(harness_home(), "workspaces")


def workspace_dir(user: str | None) -> str | None:
    """The directory this user's agents work in; None means "use the server's cwd"."""
    if not user:
        return None
    if not SAFE_USER.match(user):
        raise ValueError(f"unsafe user name for a workspace: {user!r}")
    return os.path.join(workspaces_root(), "u", user)


def uploads_dir(user: str | None) -> str | None:
    workspace = workspace_dir(user)
    return os.path.join(workspace, UPLOADS) if workspace else None


def ensure(user: str | None) -> str | None:
    """Create the workspace on first use and return it."""
    workspace = workspace_dir(user)
    if workspace:
        os.makedirs(os.path.join(workspace, UPLOADS), exist_ok=True)
    return workspace


def safe_name(name: str) -> str:
    """A storable basename: no directories, no control characters, never empty."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = name.replace("\\", "/").split("/")[-1]      # basename, both separators
    name = UNSAFE_CHARS.sub("_", name).strip(" .")
    if not name or set(name) <= {"."}:
        name = "upload"
    if len(name) > MAX_NAME_LENGTH:
        stem, dot, extension = name.rpartition(".")
        keep = MAX_NAME_LENGTH - len(extension) - 1
        name = (stem[:keep] + dot + extension) if dot and keep > 0 else name[:MAX_NAME_LENGTH]
    return name


def unique_path(directory: str, name: str) -> str:
    """Never silently overwrite someone's earlier upload."""
    candidate = os.path.join(directory, name)
    if not os.path.exists(candidate):
        return candidate
    stem, dot, extension = name.rpartition(".")
    stem, extension = (stem, dot + extension) if dot else (name, "")
    for index in range(1, 1000):
        candidate = os.path.join(directory, f"{stem}-{index}{extension}")
        if not os.path.exists(candidate):
            return candidate
    raise OSError("too many files with that name")


@dataclass
class FileRules:
    max_file_bytes: int = DEFAULT_MAX_FILE_MB * 1024 * 1024
    max_total_bytes: int = DEFAULT_MAX_TOTAL_MB * 1024 * 1024
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    retention_days: int = DEFAULT_RETENTION_DAYS

    @classmethod
    def from_config(cls, settings: dict[str, Any] | None) -> "FileRules":
        settings = settings or {}
        extensions = settings.get("allowed_extensions")
        if extensions:
            normalised = tuple(
                ("." + str(item).lower().lstrip(".")) for item in extensions
            )
        else:
            normalised = DEFAULT_EXTENSIONS
        return cls(
            max_file_bytes=int(settings.get("max_file_mb", DEFAULT_MAX_FILE_MB)) * 1024 * 1024,
            max_total_bytes=int(settings.get("max_total_mb", DEFAULT_MAX_TOTAL_MB)) * 1024 * 1024,
            extensions=normalised,
            retention_days=int(settings.get("retention_days", DEFAULT_RETENTION_DAYS)),
        )

    def extension_ok(self, name: str) -> bool:
        _, _, extension = name.rpartition(".")
        return ("." + extension.lower()) in self.extensions if extension else False


class UploadRefused(ValueError):
    """The upload was rejected by a rule; the message is shown to the user."""


def total_bytes(user: str | None) -> int:
    directory = uploads_dir(user)
    if not directory or not os.path.isdir(directory):
        return 0
    total = 0
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            total += os.path.getsize(path)
    return total


def store(user: str | None, filename: str, data: bytes, rules: FileRules) -> dict[str, Any]:
    """Write one uploaded file into the user's uploads directory."""
    directory = uploads_dir(user)
    if not directory:
        raise UploadRefused("這個伺服器沒有啟用帳號，無法接收上傳")
    name = safe_name(filename)
    if not rules.extension_ok(name):
        raise UploadRefused(f"不接受這種副檔名：{name}")
    if len(data) > rules.max_file_bytes:
        raise UploadRefused(
            f"檔案 {len(data) / 1048576:.1f} MB 超過單檔上限 {rules.max_file_bytes // 1048576} MB"
        )
    if not data:
        raise UploadRefused("檔案是空的")
    if total_bytes(user) + len(data) > rules.max_total_bytes:
        raise UploadRefused(
            f"超過你的儲存上限 {rules.max_total_bytes // 1048576} MB，請先刪除用不到的檔案"
        )
    os.makedirs(directory, exist_ok=True)
    path = unique_path(directory, name)
    # Belt and braces: the sanitised name cannot escape, but verify anyway.
    if os.path.commonpath([os.path.realpath(directory), os.path.realpath(os.path.dirname(path))]) != os.path.realpath(directory):
        raise UploadRefused("檔名不合法")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    return {
        "name": os.path.basename(path),
        "path": f"{UPLOADS}/{os.path.basename(path)}",   # relative to the agent's cwd
        "bytes": len(data),
        "uploaded": datetime.now(timezone.utc).isoformat(),
    }


def listing(user: str | None) -> list[dict[str, Any]]:
    directory = uploads_dir(user)
    if not directory or not os.path.isdir(directory):
        return []
    items = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        items.append({
            "name": name,
            "path": f"{UPLOADS}/{name}",
            "bytes": os.path.getsize(path),
            "uploaded": datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(),
        })
    return sorted(items, key=lambda item: item["uploaded"], reverse=True)


def remove(user: str | None, name: str) -> bool:
    directory = uploads_dir(user)
    if not directory:
        return False
    path = os.path.join(directory, safe_name(name))
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True


def sweep(user: str | None, rules: FileRules) -> int:
    """Delete uploads past the retention window; 0 days means keep them."""
    directory = uploads_dir(user)
    if not directory or not rules.retention_days or not os.path.isdir(directory):
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - rules.retention_days * 86400
    removed = 0
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed
