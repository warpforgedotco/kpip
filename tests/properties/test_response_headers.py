"""kpip's header parser must build the message email.parser builds.

:func:`kpip.network.headers.parse_header_lines` replaces http.client's, and
urllib3 reads the result's headers, payload and defects. For any header
block, the two messages must agree on all of them, whether kpip parses the
block itself or hands it to the original, and whether or not the lines end
with the blank line that closes a response's headers.
"""

from __future__ import annotations

import io

from hypothesis import example, given
from hypothesis import strategies as st

from kpip.network import headers

names = st.one_of(
    st.sampled_from([b"Content-Type", b"ETag", b"Cache-Control", b"From", b"X-Odd"]),
    st.binary(min_size=0, max_size=6),
)
values = st.one_of(
    st.sampled_from(
        [
            b"application/json",
            b" multipart/mixed; boundary=x",
            b"message/rfc822",
            b"\t gzip ",
        ]
    ),
    st.binary(max_size=12),
)
endings = st.sampled_from([b"\r\n", b"\n", b"\r", b""])
lines = st.one_of(
    st.builds(lambda n, v, e: n + b":" + v + e, names, values, endings),
    st.builds(lambda v, e: v + e, values, endings),
)
blocks = st.builds(
    lambda body, end: b"".join(body) + end,
    st.lists(lines, max_size=6),
    st.sampled_from([b"\r\n", b"\n", b"", b"rest\r\n"]),
)


def state(block: bytes, terminated: bool, parse) -> tuple:
    fp = io.BytesIO(block)
    lines = []
    while (line := fp.readline()) not in (b"\r\n", b"\n", b""):
        lines.append(line)
    if terminated:
        lines.append(line)
    message = parse(lines)
    payload = message._payload
    return (
        type(message),
        message._headers,
        [(part._headers, part._payload) for part in payload]
        if isinstance(payload, list)
        else payload,
        message._unixfrom,
        [type(defect) for defect in message.defects],
        message.is_multipart(),
    )


@given(blocks, st.booleans())
@example(b'Content-Type: text/html\r\nETag:  "x" \r\n\r\nbody', True)
@example(b"From nobody\r\nA: b\r\n\r\n", True)
@example(b"A: b\r\n continued\r\n\r\n", True)
@example(b"Content-Type: multipart/mixed; boundary=x\r\n\r\n", True)
@example(b"Proxy-Agent: p\r\n", False)
def test_parse_header_lines_builds_the_message_email_parser_builds(
    block: bytes, terminated: bool
) -> None:
    assert state(block, terminated, headers.parse_header_lines) == state(
        block, terminated, headers._original
    )
