"""Response headers parsed without the email package.

http.client hands every response's header lines to email.parser, a general
RFC 5322 parser. Responses from an index have one header per line, so
parse_header_lines splits them itself into the message email.parser would
build, and leaves any block with continuation lines, a malformed line or a
multipart or message body to the original.
"""

from __future__ import annotations

import http.client

_original = http.client._parse_header_lines  # ty:ignore[unresolved-attribute]
_NAME_BYTES = bytes(range(0x21, 0x7F)).replace(b":", b"")
_ENDS = (b"\r\n", b"\n", b"")


def _header(line: bytes) -> tuple[str, str] | None:
    """The (name, value) email.parser reads from one header line, or None."""
    if line.endswith(b"\r\n"):
        line = line[:-2]
    elif line.endswith(b"\n"):
        line = line[:-1]
    else:
        return None
    name, colon, value = line.partition(b":")
    if not colon or not name or name.translate(None, _NAME_BYTES) or b"\r" in value:
        return None
    return name.decode("latin-1"), value.lstrip(b" \t").decode("latin-1")


def parse_header_lines(lines: list[bytes], _class=http.client.HTTPMessage):
    message = _class()
    headers = message._headers  # ty:ignore[unresolved-attribute]
    for line in lines[:-1] if lines and lines[-1] in _ENDS else lines:
        header = _header(line)
        if header is None:
            return _original(lines, _class)
        name, value = header
        if name.lower() == "content-type" and value.lower().lstrip().startswith(
            ("multipart", "message")
        ):
            return _original(lines, _class)
        headers.append(header)
    message.set_payload("")
    return message


def install() -> None:
    """Have http.client parse every response's headers with parse_header_lines."""
    http.client._parse_header_lines = parse_header_lines  # ty:ignore[unresolved-attribute]
