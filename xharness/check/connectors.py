"""Source connectors: how the platform gets records for a scheduled or manual run.

  rest    GET a URL, take the list at records_path ("data.items"), map fields.
          The token (if any) is AES-256-GCM encrypted at rest and only sent in the Authorization header.
          Only http/https; loopback, link-local (cloud metadata) and other reserved addresses are refused
          unless the host is listed in [check] connector_hosts.
  folder  read *.csv / *.json / *.jsonl dropped into a folder under [check] inbox_root; files that were
          read move to processed/<date>/, files that failed move to failed/ -- nothing is read twice.

Field mapping: {"agent_field": "source.path"}; an unmapped agent field is taken from the source field of
the same name. Every fetch returns a snapshot note (count, sha256, basis time) that goes into the run.
"""

from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from . import rulekit
from .store import TPE

KINDS = {"rest": "REST API（平臺去拉）", "folder": "檔案交換（對方放檔）"}
MAX_BYTES = 20_000_000
MAX_RECORDS = 50_000
FILE_TYPES = (".csv", ".json", ".jsonl")


class ConnectorError(ValueError):
    """A problem the operator can act on, worded for the page."""


def validate(config: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    kind = config.get("kind")
    if kind not in KINDS:
        raise ConnectorError("介接方式只能是 REST API 或檔案交換")
    if not str(config.get("name", "")).strip():
        raise ConnectorError("請填介接名稱")
    mapping = config.get("mapping") or {}
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise ConnectorError("欄位對應要是「我們的欄位：對方的欄位」")
    if kind == "rest":
        url = str(config.get("url", ""))
        if not re.match(r"^https?://", url):
            raise ConnectorError("網址只能是 http 或 https")
        check_host(url, settings)
    else:
        folder_path(config, settings)
    return config


def check_host(url: str, settings: dict[str, Any]) -> None:
    """Refuse reserved addresses (SSRF) unless the operator allowed the host by name."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in {str(h) for h in settings.get("connector_hosts", [])}:
        return
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        raise ConnectorError(f"找不到主機「{host}」") from None
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ConnectorError(f"「{host}」指向本機或保留位址（{address}），平臺不會去連；"
                                 "確定要連請由平臺管理者加進 [check] connector_hosts")


def folder_path(config: dict[str, Any], settings: dict[str, Any]) -> str:
    root = str(settings.get("inbox_root") or "")
    if not root:
        raise ConnectorError("平臺管理者還沒設定 [check] inbox_root，檔案交換不能用")
    sub = str(config.get("folder", "")).strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_\-/]{1,120}", sub) or ".." in sub.split("/"):
        raise ConnectorError("資料夾名稱只能用英數、底線、連字號與斜線")
    path = os.path.realpath(os.path.join(root, sub))
    if not path.startswith(os.path.realpath(root) + os.sep):
        raise ConnectorError("資料夾必須在 inbox_root 底下")
    return path


def _parse(name: str, data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8-sig")
    if name.endswith(".csv"):
        return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(io.StringIO(text))]
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    value = json.loads(text)
    return value if isinstance(value, list) else [value]


def map_records(records: list[Any], mapping: dict[str, str], records_path: str = "") -> list[dict[str, Any]]:
    out = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record = dict(raw)
        for ours, theirs in mapping.items():
            if theirs:
                record[ours] = rulekit.dig(raw, theirs)
        out.append(record)
    return out


def fetch(config: dict[str, Any], settings: dict[str, Any], secret: str = "", *, opener: Any = None,
          dry_run: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(records, snapshot note). dry_run reads without moving files (for "測試連線")."""
    basis = datetime.now(TPE).isoformat(timespec="seconds")
    files: list[str] = []
    bad: list[str] = []
    if config["kind"] == "rest":
        url = str(config["url"])
        check_host(url, settings)
        request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       **({"Authorization": f"Bearer {secret}"} if secret else {})})
        try:
            with (opener or urllib.request.urlopen)(request, timeout=30) as response:  # nosec B310 -- http/https only, host checked above
                body = response.read(MAX_BYTES + 1)
        except OSError as error:
            raise ConnectorError(f"連不上對方（{type(error).__name__}）") from None
        if len(body) > MAX_BYTES:
            raise ConnectorError("對方回的資料超過 20MB")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise ConnectorError("對方回的不是 JSON") from None
        listed = rulekit.dig(payload, str(config.get("records_path", ""))) if config.get("records_path") else payload
        if not isinstance(listed, list):
            raise ConnectorError(f"在「{config.get('records_path') or '最外層'}」找不到資料清單")
        raw = listed
        digest = hashlib.sha256(body).hexdigest()
    else:
        folder = folder_path(config, settings)
        os.makedirs(folder, exist_ok=True)
        raw, hasher, bad = [], hashlib.sha256(), []
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not name.lower().endswith(FILE_TYPES) or not os.path.isfile(path):
                continue
            with open(path, "rb") as handle:
                data = handle.read(MAX_BYTES + 1)
            try:
                if len(data) > MAX_BYTES:
                    raise ConnectorError("檔案超過 20MB")
                raw.extend(_parse(name.lower(), data))
                hasher.update(data)
                files.append(path)
            except (ValueError, ConnectorError, UnicodeDecodeError) as error:
                # One bad file is set aside on its own; the good files still run (and the run says so).
                bad.append(f"{name}（{type(error).__name__}）")
                if not dry_run:
                    failed = os.path.join(folder, "failed")
                    os.makedirs(failed, exist_ok=True)
                    shutil.move(path, os.path.join(failed, f"{int(time.time())}-{name}"))
        digest = hasher.hexdigest()
    if len(raw) > MAX_RECORDS:
        raise ConnectorError(f"一次超過 {MAX_RECORDS} 筆，請對方分批")
    records = map_records(raw, config.get("mapping") or {})
    note = {"connector": config.get("name"), "kind": config["kind"], "records": len(records), "sha256": digest,
            "basis": basis, "files": [os.path.basename(f) for f in files], "failed_files": bad}
    return records, note


def archive(config: dict[str, Any], settings: dict[str, Any], note: dict[str, Any]) -> None:
    """After a successful run only: move the files that were read to processed/<date>/, so a failed run is retried."""
    if config.get("kind") != "folder" or not note.get("files"):
        return
    folder = folder_path(config, settings)
    done = os.path.join(folder, "processed", datetime.now(TPE).strftime("%Y-%m-%d"))
    os.makedirs(done, exist_ok=True)
    for name in note["files"]:
        if os.path.isfile(os.path.join(folder, name)):
            shutil.move(os.path.join(folder, name), os.path.join(done, name))
