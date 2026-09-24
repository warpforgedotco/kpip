"""Reading the lock a new one starts from."""

from __future__ import annotations

from pathlib import Path

from kpip.cli.fast import parse_lock_arguments
from kpip.cli.lock_format import (
    lock_left_behind,
    lock_preferences,
    previous_lock_digest,
    read_previous_lock,
)

LOCK = b"""\
created-by = "kpip"
lock-version = "1.0"

[[packages]]
name = "Demo_Pkg"
version = "1.2"

[[packages]]
name = "other"
version = "3.0"

[[packages]]
name = "local"
directory = { path = "." }
"""


def test_preferences_are_each_versioned_package_by_canonical_name() -> None:
    assert lock_preferences(LOCK, []) == {"demo-pkg": "1.2", "other": "3.0"}


def test_an_upgraded_package_has_no_preference() -> None:
    assert lock_preferences(LOCK, ["demo.pkg"]) == {"other": "3.0"}


def test_no_previous_lock_or_an_unreadable_one_prefers_nothing() -> None:
    assert lock_preferences(None, []) == {}
    assert lock_preferences(b"not [ toml", []) == {}
    assert lock_preferences(b"\xff", []) == {}
    assert lock_preferences(b'packages = "none"', []) == {}


def test_the_previous_lock_is_the_output(tmp_path: Path) -> None:
    output = tmp_path / "pylock.toml"

    assert read_previous_lock(str(output), upgrade=False) is None

    output.write_bytes(LOCK)

    assert read_previous_lock(str(output), upgrade=False) == LOCK
    assert read_previous_lock(str(output), upgrade=True) is None
    assert read_previous_lock("-", upgrade=False) is None


def test_the_lock_left_behind_is_what_the_next_run_reads(tmp_path: Path) -> None:
    output = str(tmp_path / "pylock.toml")

    assert lock_left_behind(output, False, "lock") == b"lock"
    assert lock_left_behind(output, True, "lock") is None
    assert lock_left_behind("-", False, "lock") is None


def test_the_digest_tells_starting_points_apart() -> None:
    digests = {
        previous_lock_digest(None, []),
        previous_lock_digest(LOCK, []),
        previous_lock_digest(LOCK + b"\n", []),
        previous_lock_digest(LOCK, ["other"]),
    }

    assert len(digests) == 4
    assert previous_lock_digest(None, ["other"]) == previous_lock_digest(None, [])
    assert previous_lock_digest(LOCK, ["a", "b"]) == previous_lock_digest(
        LOCK, ["b", "a"]
    )


def test_the_fast_path_reads_the_upgrade_options() -> None:
    options = parse_lock_arguments(["demo", "-U", "-P", "a", "--upgrade-package=b"])

    assert options is not None
    assert options.upgrade
    assert options.upgrade_packages == ["a", "b"]


WRITTEN = b"""\
created-by = "kpip"
lock-version = "1.0"

[[packages]]
name = "demo"
version = "1.2"
[[packages.wheels]]
name = "demo-1.2-py3-none-any.whl"
url = "https://files.invalid/demo-1.2-py3-none-any.whl"
[packages.wheels.hashes]
sha256 = "00"

[[packages]]
name = "other"
version = "3.0"
[packages.sdist]
name = "other-3.0.tar.gz"
"""


def test_a_lock_kpip_wrote_is_read_from_its_lines() -> None:
    """A wheel's own ``name`` is not taken for the package's."""
    assert lock_preferences(WRITTEN, []) == {"demo": "1.2", "other": "3.0"}


def test_a_lock_another_tool_wrote_is_read_as_toml() -> None:
    other_tool = WRITTEN.replace(b'created-by = "kpip"', b'created-by = "uv"')
    reordered = other_tool.replace(
        b'name = "demo"\nversion = "1.2"', b'version = "1.2"\nname = "demo"'
    )

    assert lock_preferences(reordered, []) == {"demo": "1.2", "other": "3.0"}


def test_an_escaped_value_is_read_as_toml() -> None:
    escaped = WRITTEN.replace(b'version = "1.2"', b'version = "1\\u002e2"')

    assert lock_preferences(escaped, []) == {"demo": "1.2", "other": "3.0"}
