"""One masking function, used everywhere something is written down.

Audit records, session transcripts and error messages are all places a
credential leaks into by accident: a failing request carries the URL that
had a token in its query string, an exception repeats the header it could
not parse. Scrubbing in one place means there is one thing to review and
one thing to fix, instead of every call site deciding for itself.

Masking keeps enough of a value to recognise it in a support conversation
("the key ending 4f21") and not enough to use it.
"""

from __future__ import annotations

import re
from typing import Any

KEEP_HEAD = 4
KEEP_TAIL = 2
MIN_MASKABLE = 12

#: Keys whose value is a secret wherever it appears in a mapping.
SECRET_KEYS = re.compile(
    r"(password|passwd|secret|token|api[-_]?key|apikey|authorization|credential|private[-_]?key)",
    re.IGNORECASE,
)
PATTERNS = (
    # Authorization: Bearer <token>
    re.compile(r"(Bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
    # ?api_key=... / &token=... in a URL
    re.compile(r"([?&](?:api[-_]?key|key|token|access_token|password)=)([^&\s\"']{4,})", re.IGNORECASE),
    # .env style: OPENAI_API_KEY=..., DB_PASSWORD="..." (upper-case names only,
    # so prose such as "max_tokens=16384" is left alone)
    re.compile(r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)\s*=\s*[\"']?)([^\s\"']{6,})"),
    # provider-style keys: sk-..., sk-proj-..., sk-ant-api03-..., hf_..., ghp_...
    # The body includes - and _: modern keys carry them, and stopping at the
    # first dash used to mask "sk-proj" and print the actual secret after it.
    re.compile(r"\b((?:sk|pk|rk)-[A-Za-z0-9_-]{8,}|hf_[A-Za-z0-9_]{8,}|gh[pousr]_[A-Za-z0-9_]{8,})"),
    # user:password@host
    re.compile(r"(://[^:/\s]+:)([^@/\s]{3,})(?=@)"),
)


def mask(value: Any) -> str:
    """A recognisable stub of a secret: ab12……f4, or all stars when it is short."""
    text = str(value or "")
    if not text:
        return ""
    if len(text) < MIN_MASKABLE:
        return "*" * len(text)
    return f"{text[:KEEP_HEAD]}{'*' * 6}{text[-KEEP_TAIL:]}"


def scrub(text: Any) -> str:
    """Mask every credential shape in a free-text string (log lines, exceptions)."""
    result = str(text or "")
    for pattern in PATTERNS:
        if pattern.groups == 1:
            result = pattern.sub(lambda match: mask(match.group(1)), result)
        else:
            result = pattern.sub(lambda match: match.group(1) + mask(match.group(2)), result)
    return result


def scrub_mapping(data: Any, _depth: int = 0) -> Any:
    """Recursively mask values whose key names them a secret, and scrub the rest."""
    if _depth > 6:
        return data
    if isinstance(data, dict):
        return {
            key: (mask(value) if SECRET_KEYS.search(str(key)) and isinstance(value, (str, int))
                  else scrub_mapping(value, _depth + 1))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [scrub_mapping(item, _depth + 1) for item in data]
    if isinstance(data, str):
        return scrub(data)
    return data
