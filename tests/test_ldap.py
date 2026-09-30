import pytest

from xharness.ldap import LdapError, LdapServer, authenticate, bind_request, bind_result

SERVER = LdapServer(url="ldaps://ad.example.edu", user_dn="{user}@example.edu")


def _response(code: int, message: str = "") -> bytes:
    """Hand-built BindResponse, so the decoder is tested against real BER."""
    text = message.encode("utf-8")
    inner = bytes([0x0A, 0x01, code]) + b"\x04\x00" + bytes([0x04, len(text)]) + text
    payload = bytes([0x61, len(inner)]) + inner
    body = b"\x02\x01\x01" + payload
    return bytes([0x30, len(body)]) + body


def test_bind_request_is_well_formed():
    data = bind_request(1, "alice@example.edu", "secret")
    assert data[0] == 0x30  # LDAPMessage SEQUENCE
    assert b"\x60" in data  # [APPLICATION 0] BindRequest
    assert b"alice@example.edu" in data
    assert data.count(b"\x02\x01\x03") == 1  # version 3


def test_long_values_use_long_form_length():
    data = bind_request(1, "u@example.edu", "x" * 300)
    assert data[1] & 0x80  # long-form length marker


def test_result_success():
    assert bind_result(_response(0)) == (0, "")


def test_result_invalid_credentials():
    code, message = bind_result(_response(49, "80090308: LdapErr"))
    assert code == 49 and "LdapErr" in message


def test_malformed_response_raises():
    with pytest.raises(LdapError):
        bind_result(b"\x05\x00")
    with pytest.raises(LdapError):
        bind_result(b"")


def test_empty_password_never_reaches_the_network():
    # An empty simple bind is an anonymous bind and most servers answer OK.
    ok, detail = authenticate(SERVER, "alice", "")
    assert ok is False and detail == "empty password"


def test_dn_injection_refused():
    with pytest.raises(LdapError):
        SERVER.dn_for("alice,cn=admin,dc=example")
    with pytest.raises(LdapError):
        SERVER.dn_for("alice*)(uid=*")


def test_dn_template_filled():
    assert SERVER.dn_for("alice") == "alice@example.edu"
    other = LdapServer(url="ldap://d", user_dn="uid={user},ou=people,dc=example")
    assert other.dn_for("bob") == "uid=bob,ou=people,dc=example"


def test_scheme_and_default_ports():
    assert LdapServer("ldaps://host", "{user}").parts == ("host", 636, True)
    assert LdapServer("ldap://host", "{user}").parts == ("host", 389, False)
    assert LdapServer("host", "{user}").parts == ("host", 389, False)
    with pytest.raises(LdapError):
        LdapServer("https://host", "{user}").parts


def test_unreachable_server_raises_not_returns_false():
    # Down must not look like "wrong password": the caller reports differently.
    with pytest.raises(LdapError):
        authenticate(LdapServer("ldap://127.0.0.1:1", "{user}", timeout=0.2), "alice", "secret")
