"""A wheel's release, read without its tags, must be what the full parse reads.

``wheel_release_from_filename`` builds a page's catalog; ``parse_wheel_file_once``
everything else. For any name, both refuse it, or both give the same
canonical name and version.
"""

from __future__ import annotations

from hypothesis import example, given
from hypothesis import strategies as st

from kpip.core.wheel import parse_wheel_file_once, wheel_release_from_filename

fields = st.sampled_from(
    [
        "Foo_Bar",
        "foo",
        "foo.bar",
        "foo__bar",
        "foo-bar",
        "Ünï",
        "1.0",
        "1.0rc1",
        "1.0+local",
        "v2",
        "not.a.version",
        "1",
        "1a",
        "py3",
        "none",
        "any",
        "cp311.cp312",
        "manylinux_2_17_x86_64",
        "",
        "_",
        ".",
    ]
)
names = st.builds(
    lambda parts, suffix: "-".join(parts) + suffix,
    st.lists(fields, min_size=1, max_size=7),
    st.sampled_from([".whl", ".WHL", ".tar.gz", ""]),
)


def full(name: str) -> tuple[str, str] | None:
    wheel = parse_wheel_file_once(name)
    return None if wheel is None else (wheel.name, wheel.version.public)


def release(name: str) -> tuple[str, str] | None:
    found = wheel_release_from_filename(name)
    return None if found is None else (found[0], found[1].public)


@given(names)
@example("Foo_Bar-1.0-py3-none-any.whl")
@example("foo-1.0-1-py3-none-any.whl")
@example("foo-1.0-x1-py3-none-any.whl")
@example("foo__bar-1.0-py3-none-any.whl")
@example("dir/foo-1.0-py3-none-any.whl")
@example("foo-bad.version-py3-none-any.whl")
def test_a_wheels_release_is_what_its_full_parse_reads(name: str) -> None:
    assert release(name) == full(name)
