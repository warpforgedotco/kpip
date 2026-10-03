"""RECORD's text must be what csv.writer writes for its rows.

:func:`kpip.install.wheel_transaction.record_text_of` joins rows itself
when no field needs quoting; for any rows, quoting or not, its text must be
csv.writer's, byte for byte.
"""

from __future__ import annotations

import csv
import io

from hypothesis import example, given
from hypothesis import strategies as st

from kpip.install.wheel_transaction import record_text_of

fields = st.one_of(
    st.sampled_from(["", "pkg/__init__.py", "sha256=abc", "123", "a b"]),
    st.text(alphabet=st.sampled_from('ab/._ ,"\r\n\t'), max_size=8),
)
rows = st.lists(st.tuples(fields, fields, fields), max_size=6)


@given(rows)
@example([("pkg-1.0.dist-info/RECORD", "", "")])
@example([("pkg/a,b.py", "sha256=x", "1")])
@example([('pkg/"q".py', "sha256=x", "1")])
@example([("pkg/line\nbreak.py", "sha256=x", "1")])
def test_record_text_is_what_csv_writes(rows: list[tuple[str, str, str]]) -> None:
    expected = io.StringIO(newline="")
    csv.writer(expected).writerows(rows)
    assert record_text_of(rows) == expected.getvalue()
