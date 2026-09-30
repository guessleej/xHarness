"""Minimal LDAP simple bind, standard library only.

Schools authenticate against Active Directory or OpenLDAP, and xHarness
ships no runtime dependencies, so this module speaks just enough LDAP to
answer one question: does this user's password check out? It encodes a
BindRequest in BER, reads the BindResponse result code, and closes the
connection. No search, no directory browsing, no attribute mapping --
roles and quotas come from the harness config, not from the directory.

Security notes:
- an empty password turns a simple bind into an ANONYMOUS bind, which most
  servers answer with success; empty passwords are rejected before connecting
- the DN template is filled with a username validated against a strict
  character set, so a crafted name cannot inject DN syntax
- ldaps:// verifies the certificate chain and hostname by default
"""

from __future__ import annotations

import re
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

BIND_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 65536
LDAP_VERSION = 3

# Usernames that may be substituted into a DN template. Deliberately strict:
# no commas, equals signs, backslashes, parentheses or spaces, which are the
# characters that give DN and filter syntax its meaning.
SAFE_USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

RESULT_SUCCESS = 0
RESULT_INVALID_CREDENTIALS = 49
RESULT_MESSAGES = {
    RESULT_INVALID_CREDENTIALS: "invalid credentials",
    1: "operations error",
    2: "protocol error",
    8: "stronger authentication required",
    53: "unwilling to perform (the account may be disabled or must change its password)",
}


class LdapError(RuntimeError):
    """The directory could not be reached or answered with a protocol error."""


# --- BER encoding ------------------------------------------------------


def _length(size: int) -> bytes:
    if size < 0x80:
        return bytes([size])
    body = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _length(len(body)) + body


def _integer(value: int) -> bytes:
    size = max(1, (value.bit_length() + 8) // 8)  # keep the sign bit clear
    return _tlv(0x02, value.to_bytes(size, "big"))


def _octet_string(value: str) -> bytes:
    return _tlv(0x04, value.encode("utf-8"))


def bind_request(message_id: int, dn: str, password: str) -> bytes:
    """LDAPMessage { messageID, [APPLICATION 0] BindRequest { version, name, simple } }."""
    body = (
        _integer(LDAP_VERSION)
        + _octet_string(dn)
        + _tlv(0x80, password.encode("utf-8"))  # [0] simple authentication
    )
    return _tlv(0x30, _integer(message_id) + _tlv(0x60, body))


# --- BER decoding ------------------------------------------------------


def _read_length(data: bytes, index: int) -> tuple[int, int]:
    if index >= len(data):
        raise LdapError("truncated response")
    first = data[index]
    index += 1
    if first < 0x80:
        return first, index
    count = first & 0x7F
    if count == 0 or index + count > len(data):
        raise LdapError("malformed length in response")
    return int.from_bytes(data[index : index + count], "big"), index + count


def bind_result(data: bytes) -> tuple[int, str]:
    """Pull (resultCode, diagnosticMessage) out of a BindResponse."""
    if not data or data[0] != 0x30:
        raise LdapError("response is not an LDAPMessage")
    _, index = _read_length(data, 1)
    if index >= len(data) or data[index] != 0x02:
        raise LdapError("response has no message id")
    id_length, index = _read_length(data, index + 1)
    index += id_length
    if index >= len(data) or data[index] != 0x61:  # [APPLICATION 1] BindResponse
        raise LdapError("response is not a BindResponse")
    _, index = _read_length(data, index + 1)
    if index >= len(data) or data[index] != 0x0A:
        raise LdapError("BindResponse has no result code")
    code_length, index = _read_length(data, index + 1)
    code = int.from_bytes(data[index : index + code_length], "big")
    index += code_length
    message = ""
    for _ in range(2):  # matchedDN, then diagnosticMessage
        if index >= len(data) or data[index] != 0x04:
            break
        text_length, index = _read_length(data, index + 1)
        message = data[index : index + text_length].decode("utf-8", "replace")
        index += text_length
    return code, message


# --- connection --------------------------------------------------------


@dataclass
class LdapServer:
    url: str
    user_dn: str
    timeout: float = BIND_TIMEOUT_SECONDS
    verify: bool = True

    @property
    def parts(self) -> tuple[str, int, bool]:
        split = urlsplit(self.url if "://" in self.url else f"ldap://{self.url}")
        secure = split.scheme == "ldaps"
        if split.scheme not in ("ldap", "ldaps"):
            raise LdapError(f"unsupported LDAP scheme: {split.scheme}")
        return split.hostname or "", split.port or (636 if secure else 389), secure

    def dn_for(self, username: str) -> str:
        if not SAFE_USERNAME.match(username):
            raise LdapError("username contains characters that are not allowed in a DN")
        return self.user_dn.replace("{user}", username)


def authenticate(server: LdapServer, username: str, password: str) -> tuple[bool, str]:
    """Return (ok, detail). A False result is an answer, not an error; LdapError means unreachable."""
    if not password:
        # An empty password makes this an anonymous bind, which succeeds on
        # most servers and would let anyone in as anyone.
        return False, "empty password"
    dn = server.dn_for(username)
    host, port, secure = server.parts
    if not host:
        raise LdapError(f"no host in LDAP url: {server.url}")
    try:
        connection: socket.socket = socket.create_connection((host, port), timeout=server.timeout)
    except OSError as error:
        raise LdapError(f"cannot reach {host}:{port}: {error}") from error
    try:
        if secure:
            context = ssl.create_default_context()
            if not server.verify:
                # Opt-in only, for a school's internal CA that is not installed
                # on this machine; documented as a downgrade in the config.
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            connection = context.wrap_socket(connection, server_hostname=host)
        connection.sendall(bind_request(1, dn, password))
        data = connection.recv(MAX_RESPONSE_BYTES)
    except (OSError, ssl.SSLError) as error:
        raise LdapError(f"LDAP bind to {host}:{port} failed: {error}") from error
    finally:
        try:
            connection.close()
        except OSError:  # nosec B110 - the bind result is already decided; a failed close changes nothing
            pass
    code, message = bind_result(data)
    if code == RESULT_SUCCESS:
        return True, "ok"
    return False, message or RESULT_MESSAGES.get(code, f"LDAP result code {code}")
