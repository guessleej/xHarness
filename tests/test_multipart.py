import pytest

from xharness.multipart import MultipartError, boundary_of, files, parse

CT = "multipart/form-data; boundary=----XH123"


def body(*parts):
    out = b""
    for headers, data in parts:
        out += b"------XH123\r\n" + headers + b"\r\n\r\n" + data + b"\r\n"
    return out + b"------XH123--\r\n"


def test_single_file():
    raw = body((b'Content-Disposition: form-data; name="file"; filename="a.txt"', b"hello"))
    [part] = parse(raw, CT)
    assert part.field == "file" and part.filename == "a.txt" and part.data == b"hello"
    assert part.is_file


def test_binary_survives_intact():
    payload = bytes(range(256)) * 4
    raw = body((b'Content-Disposition: form-data; name="f"; filename="b.bin"', payload))
    assert parse(raw, CT)[0].data == payload


def test_data_containing_crlf_is_not_truncated():
    payload = b"line one\r\nline two\r\n\r\nline three"
    raw = body((b'Content-Disposition: form-data; name="f"; filename="c.txt"', payload))
    assert parse(raw, CT)[0].data == payload


def test_several_parts_keep_order():
    raw = body(
        (b'Content-Disposition: form-data; name="note"', b"hi"),
        (b'Content-Disposition: form-data; name="file"; filename="x.csv"', b"a,b"),
    )
    parts = parse(raw, CT)
    assert [p.field for p in parts] == ["note", "file"]
    assert [p.filename for p in files(parts)] == ["x.csv"]


def test_plain_field_is_not_a_file():
    raw = body((b'Content-Disposition: form-data; name="note"', b"hi"))
    assert parse(raw, CT)[0].is_file is False


def test_utf8_filename():
    raw = body(('Content-Disposition: form-data; name="f"; filename="成績單.csv"'.encode(), b"x"))
    assert parse(raw, CT)[0].filename == "成績單.csv"


def test_missing_boundary_refused():
    with pytest.raises(MultipartError):
        parse(b"whatever", "multipart/form-data")


def test_wrong_content_type_refused():
    with pytest.raises(MultipartError):
        parse(b"x", "application/json")


def test_part_without_header_block_refused():
    with pytest.raises(MultipartError):
        parse(b"------XH123\r\nno-blank-line", CT)


def test_part_without_a_name_refused():
    raw = body((b"Content-Disposition: form-data", b"x"))
    with pytest.raises(MultipartError):
        parse(raw, CT)


def test_empty_body_refused():
    with pytest.raises(MultipartError):
        parse(b"", CT)


def test_boundary_quoted_and_unquoted():
    assert boundary_of('multipart/form-data; boundary="abc"') == b"abc"
    assert boundary_of("multipart/form-data; boundary=abc") == b"abc"


def test_too_many_parts_refused():
    raw = body(*[(b'Content-Disposition: form-data; name="f%d"' % i, b"x") for i in range(70)])
    with pytest.raises(MultipartError):
        parse(raw, CT)
