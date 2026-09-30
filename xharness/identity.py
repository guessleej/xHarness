"""Identity: who is using this harness, and what they are allowed to spend.

Without an [auth] section nothing changes: the Web UI keeps its single
bearer token (or loopback-only binding) and every session lands in the
shared session directory, which is what a single-operator install wants.

With [auth] the server stops being a shared appliance:
- each person signs in and gets their own session token
- their conversations and session logs live under their own directory
- the fleet view, the session list and usage reports show them only their
  own work; an admin sees everyone
- every sign-in, refusal and lockout lands in an append-only audit log

Passwords are never stored in the config file. Local accounts keep a
PBKDF2-HMAC-SHA256 hash in a 0600 file under $XHARNESS_HOME; the LDAP
backend keeps no password material at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .ldap import LdapError, LdapServer, authenticate as ldap_authenticate
from .redact import scrub
from .session import harness_home

USERS_FILE = "users.json"
AUDIT_FILE = "access-audit.jsonl"
PBKDF2_ITERATIONS = 600_000  # OWASP 2023 guidance for PBKDF2-HMAC-SHA256
SALT_BYTES = 16
TOKEN_BYTES = 32
DEFAULT_SESSION_HOURS = 12
LOCKOUT_THRESHOLD = 5
LOCKOUT_SECONDS = 300
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SAFE_ROLE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
#: Always available, so an install with no [auth.roles] behaves as before.
BUILTIN_ROLES = {
    "admin": {"label": "管理者", "admin": True},
    "user": {"label": "一般使用者", "admin": False},
}
#: What a restricted role (a student, a guest) should not be handed. Tools are
#: removed from the registry rather than refused at call time, so the model is
#: never even told they exist.
SUGGESTED_RESTRICTED_TOOLS = ("bash", "write", "edit", "security_scan", "subagent", "subagent_batch")


def users_file() -> str:
    return os.path.join(harness_home(), USERS_FILE)


def audit_file() -> str:
    return os.path.join(harness_home(), AUDIT_FILE)


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


@dataclass
class User:
    name: str
    display: str = ""
    role: str = "user"
    quota_tokens_per_day: int | None = None
    quota_tokens_per_month: int | None = None
    #: Human-readable name of the tier, e.g. 教師, 學生.
    role_label: str = ""
    #: True when this tier may see the admin surfaces and everyone's usage.
    administrator: bool = False
    #: Tools this tier never receives.
    denied_tools: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return self.administrator

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display": self.display or self.name,
            "role": self.role,
            "role_label": self.role_label or self.role,
            "admin": self.administrator,
            "denied_tools": list(self.denied_tools),
            "quota_tokens_per_day": self.quota_tokens_per_day,
            "quota_tokens_per_month": self.quota_tokens_per_month,
        }


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


@dataclass
class UserStore:
    """Accounts, sign-in and session tokens. One instance per server."""

    settings: dict[str, Any] = field(default_factory=dict)
    path: str | None = None
    _tokens: dict[str, tuple[str, float]] = field(default_factory=dict, init=False)
    _attempts: dict[str, _Attempts] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # --- configuration ------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.settings)

    @property
    def backend(self) -> str:
        return str(self.settings.get("backend") or "local").lower()

    @property
    def iterations(self) -> int:
        """PBKDF2 rounds. Configurable because a small Jetson-class node pays for
        every sign-in, and because the test suite would otherwise spend its whole
        run stretching passwords. Lowering it below the default weakens every
        stored hash, so it is documented as a trade-off rather than a knob to turn."""
        return max(1, int(self.settings.get("pbkdf2_iterations") or PBKDF2_ITERATIONS))

    @property
    def session_seconds(self) -> float:
        return float(self.settings.get("session_hours", DEFAULT_SESSION_HOURS)) * 3600

    @property
    def allow_any(self) -> bool:
        """LDAP only: may anyone the directory accepts use the harness?"""
        return bool(self.settings.get("allow_any", self.backend == "ldap"))

    def _file(self) -> str:
        return self.path or users_file()

    def _configured(self, name: str) -> dict[str, Any]:
        entry = (self.settings.get("users") or {}).get(name)
        return entry if isinstance(entry, dict) else {}

    def roles(self) -> dict[str, dict[str, Any]]:
        """Tiers defined in [auth.roles], over the two built-in ones."""
        merged = {name: dict(spec) for name, spec in BUILTIN_ROLES.items()}
        for name, spec in (self.settings.get("roles") or {}).items():
            if not SAFE_ROLE.match(str(name)) or not isinstance(spec, dict):
                continue
            merged.setdefault(name, {}).update(spec)
        return merged

    def role_spec(self, role: str) -> dict[str, Any]:
        return self.roles().get(role, BUILTIN_ROLES["user"])

    def _profile(self, name: str, stored: dict[str, Any] | None = None) -> User:
        """Config wins over the account file, so an admin can change a tier without a re-register."""
        stored = stored or {}
        configured = self._configured(name)
        roles = self.roles()
        role = str(configured.get("role") or stored.get("role") or self.settings.get("default_role") or "user")
        if role not in roles:
            role = "user"
        spec = roles[role]

        def quota(key: str) -> int | None:
            # Most specific first: the person, then their stored record, then
            # their tier, then the site default.
            for source in (configured, stored, spec, self.settings):
                if key in source and source[key] is not None:
                    return int(source[key]) or None
            default = self.settings.get(f"default_{key}")
            return int(default) if default else None

        return User(
            name=name,
            display=str(configured.get("display") or stored.get("display") or name),
            role=role,
            role_label=str(spec.get("label") or role),
            administrator=bool(spec.get("admin", role == "admin")),
            denied_tools=tuple(str(item) for item in (spec.get("deny_tools") or ())),
            quota_tokens_per_day=quota("quota_tokens_per_day"),
            quota_tokens_per_month=quota("quota_tokens_per_month"),
        )

    # --- account file ---------------------------------------------------
    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self._file(), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        path = self._file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.tmp"
        # Create with 0600 from the start: the hash file must never exist
        # world-readable, not even for the moment between write and chmod.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)

    def add(self, name: str, password: str, role: str = "user", display: str = "") -> User:
        if not SAFE_NAME.match(name):
            raise ValueError("username may contain only letters, digits, dot, underscore and hyphen")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        if role not in self.roles():
            raise ValueError(f"role must be one of: {', '.join(sorted(self.roles()))}")
        data = self._read()
        data[name] = {
            "hash": hash_password(password, iterations=self.iterations),
            "role": role,
            "display": display or name,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        self._write(data)
        self.audit("account-create", name, True, f"role={role}")
        return self._profile(name, data[name])

    def remove(self, name: str) -> bool:
        data = self._read()
        if name not in data:
            return False
        # Disable rather than erase: the audit trail and past usage must stay
        # attributable after someone leaves or graduates.
        data[name]["disabled"] = True
        data[name]["disabled_at"] = datetime.now(timezone.utc).isoformat()
        self._write(data)
        self.revoke_user(name)
        self.audit("account-disable", name, True, "")
        return True

    def set_password(self, name: str, password: str) -> bool:
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        data = self._read()
        if name not in data:
            return False
        data[name]["hash"] = hash_password(password, iterations=self.iterations)
        self._write(data)
        self.revoke_user(name)  # old sessions must not survive a password change
        self.audit("password-change", name, True, "")
        return True

    def accounts(self) -> list[dict[str, Any]]:
        stored = self._read()
        names = set(stored) | set(self.settings.get("users") or {})
        items = []
        for name in sorted(names):
            entry = stored.get(name, {})
            profile = self._profile(name, entry).public()
            profile["disabled"] = bool(entry.get("disabled"))
            profile["source"] = "file" if name in stored else "config"
            items.append(profile)
        return items

    def profile(self, name: str) -> User | None:
        """Rebuild a user from their name, for callers holding only an identifier."""
        if not SAFE_NAME.match(name or ""):
            return None
        stored = self._read().get(name, {})
        if stored.get("disabled"):
            return None
        if not stored and not self._configured(name) and not self.allow_any:
            return None
        return self._profile(name, stored)

    # --- sign-in ---------------------------------------------------------
    def _locked(self, name: str) -> float:
        record = self._attempts.get(name)
        if record and record.locked_until > time.time():
            return record.locked_until - time.time()
        return 0.0

    def _record_failure(self, name: str) -> None:
        record = self._attempts.setdefault(name, _Attempts())
        record.failures += 1
        if record.failures >= LOCKOUT_THRESHOLD:
            record.locked_until = time.time() + LOCKOUT_SECONDS
            record.failures = 0

    def authenticate(self, name: str, password: str) -> tuple[User | None, str]:
        """Return (user, detail). A None user with a detail is a refusal, not a crash."""
        name = (name or "").strip()
        if not SAFE_NAME.match(name):
            self.audit("sign-in", name[:64], False, "invalid username")
            return None, "invalid username"
        with self._lock:
            remaining = self._locked(name)
        if remaining:
            self.audit("sign-in", name, False, "locked out")
            return None, f"too many failed attempts; try again in {int(remaining) + 1}s"

        stored = self._read().get(name, {})
        if stored.get("disabled"):
            self.audit("sign-in", name, False, "account disabled")
            return None, "account disabled"

        if self.backend == "ldap":
            ok, detail = self._ldap_check(name, password)
        else:
            ok = bool(stored.get("hash")) and verify_password(password, str(stored["hash"]))
            detail = "ok" if ok else "invalid credentials"

        if not ok:
            with self._lock:
                self._record_failure(name)
            self.audit("sign-in", name, False, detail)
            return None, detail
        with self._lock:
            self._attempts.pop(name, None)
        self.audit("sign-in", name, True, self.backend)
        return self._profile(name, stored), "ok"

    def _ldap_check(self, name: str, password: str) -> tuple[bool, str]:
        settings = self.settings.get("ldap") or {}
        url = str(settings.get("url") or "")
        user_dn = str(settings.get("user_dn") or "")
        if not url or not user_dn:
            return False, "LDAP backend is selected but [auth.ldap] url/user_dn are not set"
        if not self.allow_any and not self._configured(name):
            return False, "this account is not listed in [auth.users]"
        server = LdapServer(
            url=url,
            user_dn=user_dn,
            timeout=float(settings.get("timeout_seconds", 10)),
            verify=bool(settings.get("verify_certificate", True)),
        )
        try:
            return ldap_authenticate(server, name, password)
        except LdapError as error:
            print(f"[xharness] LDAP: {error}", file=sys.stderr)
            return False, "the directory could not be reached"

    # --- session tokens ---------------------------------------------------
    def issue(self, user: User) -> str:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with self._lock:
            self._expire()
            self._tokens[token] = (user.name, time.time() + self.session_seconds)
        return token

    def resolve(self, token: str) -> User | None:
        if not token:
            return None
        with self._lock:
            self._expire()
            entry = self._tokens.get(token)
        if entry is None:
            return None
        stored = self._read().get(entry[0], {})
        if stored.get("disabled"):
            self.revoke_user(entry[0])
            return None
        return self._profile(entry[0], stored)

    def revoke(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def revoke_user(self, name: str) -> None:
        with self._lock:
            for token in [key for key, (owner, _) in self._tokens.items() if owner == name]:
                self._tokens.pop(token, None)

    def _expire(self) -> None:
        now = time.time()
        for token in [key for key, (_, expiry) in self._tokens.items() if expiry <= now]:
            self._tokens.pop(token, None)

    def sessions(self) -> int:
        with self._lock:
            self._expire()
            return len(self._tokens)

    # --- audit -------------------------------------------------------------
    def audit(self, action: str, name: str, ok: bool, detail: str = "") -> None:
        """Append-only access log. Never records passwords or tokens."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "user": name,
            "ok": ok,
            "detail": scrub(detail)[:200],
        }
        try:
            path = audit_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as error:
            print(f"[xharness] access audit append failed: {error}", file=sys.stderr)

    def audit_tail(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with open(audit_file(), encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return []
        records = []
        for line in lines[-limit:]:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(records))
