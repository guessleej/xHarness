"""A multipart/form-data reader, standard library only.

`cgi.FieldStorage` was removed in Python 3.13 and xHarness ships no runtime
dependencies, so file uploads need their own parser. This one does exactly
what a file upload needs and nothing else: split on the boundary, read each
part's headers, hand back (field, filename, bytes).

It is a trust boundary, so it is deliberately strict -- a missing or
malformed boundary, a part without a disposition, or a body larger than the
caller allows is an error rather than a best-effort guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BOUNDARY = re.compile(r'boundary="?([^";,]+)"?', re.IGNORECASE)
DISPOSITION = re.compile(r'name="([^"]*)"(?:.*?filename="([^"]*)")?', re.IGNORECASE | re.DOTALL)
MAX_PARTS = 64
MAX_HEADER_BYTES = 8192


class MultipartError(ValueError):
    """The body was not a well-formed multipart/form-data payload."""


@dataclass
class Part:
    field: str
    filename: str | None
    data: bytes

    @property
    def is_file(self) -> bool:
        return self.filename is not None


def boundary_of(content_type: str) -> bytes:
    match = BOUNDARY.search(content_type or "")
    if not match:
        raise MultipartError("no boundary in Content-Type")
    boundary = match.group(1).strip()
    if not boundary or len(boundary) > 200:
        raise MultipartError("invalid boundary")
    return boundary.encode("ascii", "replace")


def parse(body: bytes, content_type: str) -> list[Part]:
    """Split a multipart body into its parts. Order is preserved."""
    if not (content_type or "").lower().startswith("multipart/form-data"):
        raise MultipartError("not multipart/form-data")
    delimiter = b"--" + boundary_of(content_type)
    segments = body.split(delimiter)
    if len(segments) < 2:
        raise MultipartError("body contains no parts")
    parts: list[Part] = []
    for segment in segments[1:]:
        if segment[:2] == b"--":          # closing delimiter
            break
        if len(parts) >= MAX_PARTS:
            raise MultipartError(f"too many parts (limit {MAX_PARTS})")
        segment = segment.lstrip(b"\r\n")
        head, separator, data = segment.partition(b"\r\n\r\n")
        if not separator:
            raise MultipartError("part has no header block")
        if len(head) > MAX_HEADER_BYTES:
            raise MultipartError("part headers too large")
        field, filename = _disposition(head)
        if field is None:
            raise MultipartError("part has no content-disposition name")
        # Each part's data ends with the CRLF that precedes the next delimiter.
        if data.endswith(b"\r\n"):
            data = data[:-2]
        parts.append(Part(field=field, filename=filename, data=data))
    if not parts:
        raise MultipartError("body contains no parts")
    return parts


def _disposition(head: bytes) -> tuple[str | None, str | None]:
    for line in head.split(b"\r\n"):
        if not line.lower().startswith(b"content-disposition:"):
            continue
        text = line.decode("utf-8", "replace")
        match = DISPOSITION.search(text)
        if not match:
            return None, None
        return match.group(1), match.group(2)
    return None, None


def files(parts: list[Part]) -> list[Part]:
    return [part for part in parts if part.is_file and part.data]
