"""Rules the wheel format states that the installer used to ignore.

``Root-Is-Purelib`` was written into generated wheels and never read back, so
every wheel landed in purelib; ``direct_url.json`` lost its ``subdirectory``
on a round trip; and only the exact bytes ``#!python\\n`` were recognised as a
pseudo-shebang, missing ``#!pythonw`` and the CRLF form.
"""

from __future__ import annotations

import pytest
from kpip.core.direct_url import ArchiveInfo, DirectUrl
from kpip.core.errors import UnsupportedWheel
from kpip.core.wheel import root_is_purelib_from_text
from kpip.install.wheel_archive import destination_internal_parts_text
from kpip.install.wheel_scripts import rewrite_shebang


class _SplitTarget:
    """A scheme whose purelib and platlib differ, as system layouts do.

    The roots are synthetic rather than the real ``/usr`` ones: the
    destination resolver calls ``realpath`` on them, and on a merged-``/usr``
    distribution ``/usr/lib64`` is a symlink to ``/usr/lib``.  Borrowing the
    system paths there collapses purelib and platlib onto each other and hides
    the very distinction these tests exist to pin.
    """

    purelib = "/kpip-test-prefix/lib/python3/site-packages"
    platlib = "/kpip-test-prefix/lib64/python3/site-packages"
    scripts = "/kpip-test-prefix/bin"
    data = "/kpip-test-prefix"
    headers = "/kpip-test-prefix/include/python3"
    resolved_roots_internal = None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Wheel-Version: 1.0\nRoot-Is-Purelib: true\n", True),
        ("Wheel-Version: 1.0\nRoot-Is-Purelib: false\n", False),
        ("Wheel-Version: 1.0\nRoot-Is-Purelib: True\n", True),
        ("Wheel-Version: 1.0\nRoot-Is-Purelib: FALSE\n", False),
        ("Wheel-Version: 1.0\nRoot-Is-Purelib:   true  \n", True),
        # Anything that is not `true` reads as false.
        ("Wheel-Version: 1.0\nRoot-Is-Purelib: yes\n", False),
    ],
)
def test_root_is_purelib_from_text(text: str, expected: bool) -> None:
    assert root_is_purelib_from_text(text) is expected


def test_root_is_purelib_is_required() -> None:
    with pytest.raises(UnsupportedWheel):
        root_is_purelib_from_text("Wheel-Version: 1.0\nTag: py3-none-any\n")


def test_wheel_root_follows_root_is_purelib() -> None:
    target = _SplitTarget()
    assert (
        destination_internal_parts_text(
            target,
            ("pkg", "mod.py"),
            "pkg/mod.py",
            root_is_purelib=True,
        )
        == "/kpip-test-prefix/lib/python3/site-packages/pkg/mod.py"
    )
    assert (
        destination_internal_parts_text(
            target,
            ("pkg", "_speedups.so"),
            "pkg/_speedups.so",
            root_is_purelib=False,
        )
        == "/kpip-test-prefix/lib64/python3/site-packages/pkg/_speedups.so"
    )


@pytest.mark.parametrize("root_is_purelib", [True, False])
def test_data_directory_is_unaffected(root_is_purelib: bool) -> None:
    """`.data` entries name their own scheme key, whatever the root is."""
    target = _SplitTarget()
    assert (
        destination_internal_parts_text(
            target,
            ("pkg-1.0.data", "scripts", "tool"),
            "pkg-1.0.data/scripts/tool",
            root_is_purelib=root_is_purelib,
        )
        == "/kpip-test-prefix/bin/tool"
    )
    assert (
        destination_internal_parts_text(
            target,
            ("pkg-1.0.data", "platlib", "ext.so"),
            "pkg-1.0.data/platlib/ext.so",
            root_is_purelib=root_is_purelib,
        )
        == "/kpip-test-prefix/lib64/python3/site-packages/ext.so"
    )


def test_direct_url_round_trips_subdirectory() -> None:
    """A package built from a monorepo subdirectory must record which one."""
    data = {"url": "file:///src", "subdirectory": "project", "dir_info": {}}
    parsed = DirectUrl.from_dict(data)
    assert parsed.subdirectory == "project"
    assert parsed.to_dict() == data


def test_direct_url_omits_subdirectory_when_absent() -> None:
    parsed = DirectUrl.from_dict({"url": "file:///src", "dir_info": {}})
    assert parsed.subdirectory is None
    assert "subdirectory" not in parsed.to_dict()


def test_direct_url_compat_keeps_hashes_alongside_hash() -> None:
    """The spec says producers SHOULD emit `hashes`; `hash` is the legacy
    spelling kept for older readers, not a replacement for it."""
    direct = DirectUrl(
        url="https://example.invalid/x.whl",
        archive_info=ArchiveInfo(hashes={"sha256": "abc", "md5": "def"}),
    )
    archive_info = direct.to_dict_compat()["archive_info"]
    assert archive_info["hashes"] == {"sha256": "abc", "md5": "def"}
    assert archive_info["hash"] == "sha256=abc"


@pytest.mark.parametrize(
    "contents, expected",
    [
        (b"#!python\nbody\n", b"#!/opt/py/bin/python3\nbody\n"),
        # A wheel built on Windows carries CRLF.
        (b"#!python\r\nbody\r\n", b"#!/opt/py/bin/python3\nbody\r\n"),
        # The format explicitly allows the pythonw convention.
        (b"#!pythonw\nbody\n", b"#!/opt/py/bin/python3\nbody\n"),
        (b"#!python -X utf8\nbody\n", b"#!/opt/py/bin/python3\nbody\n"),
        (b"#!python", b"#!/opt/py/bin/python3\n"),
        # Anything else is the script's own shebang and is left alone.
        (b"#!/bin/sh\necho hi\n", b"#!/bin/sh\necho hi\n"),
        (b"#!/usr/bin/env python\nbody\n", b"#!/usr/bin/env python\nbody\n"),
        (b"no shebang at all\n", b"no shebang at all\n"),
    ],
)
def test_rewrite_shebang(tmp_path, contents: bytes, expected: bytes) -> None:
    script = tmp_path / "tool"
    script.write_bytes(contents)
    rewrite_shebang(str(script), "/opt/py/bin/python3")
    assert script.read_bytes() == expected


def test_root_is_purelib_default_is_shared_by_both_readers() -> None:
    """A wheel with no Root-Is-Purelib has already passed validation by the
    time either install path asks, so both take the same lenient answer."""
    from kpip.install.wheel_transaction import root_is_purelib_or_default

    assert root_is_purelib_or_default("Wheel-Version: 1.0\n") is True
    assert (
        root_is_purelib_or_default("Wheel-Version: 1.0\nRoot-Is-Purelib: false\n")
        is False
    )


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/env/lib/python3.12/site-packages/pkg/mod.py", True),
        ("/env/lib/python3.12/site-packages/../../../bin/tool", True),
        ("/env/bin/tool", True),
        # A directory merely named "bin" is not this environment's.
        ("/tmp/anywhere/bin/payload", False),
        ("/usr/local/bin/anything", False),
        ("/etc/passwd", False),
        # commonpath compares components literally, so the escape has to be
        # resolved before it is compared.
        ("/env/lib/python3.12/site-packages/../../../../etc/passwd", False),
    ],
)
def test_record_paths_are_confined_to_the_distribution(
    path: str, expected: bool
) -> None:
    """RECORD is whatever the wheel shipped, so a row naming a file outside
    this distribution and its script directory is refused, not followed."""
    from kpip.install.uninstall import _inside_distribution

    assert _inside_distribution(path, "/env/lib/python3.12/site-packages") is expected


def test_scripts_run_with_the_python_a_parent_kpip_names(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build environment's scripts run with its own Python: the kpip that
    installs them may run under another, or be a compiled binary."""
    from kpip.install.wheel_scripts import shebang

    monkeypatch.setenv("KPIP_SCRIPT_PYTHON", "/env/bin/python")
    script = tmp_path / "tool"
    script.write_bytes(b"#!python\nprint(1)\n")

    rewrite_shebang(str(script), None)

    assert script.read_bytes() == b"#!/env/bin/python\nprint(1)\n"
    assert shebang(None, gui=False) == b"#!/env/bin/python\n"
    # A named interpreter still wins.
    assert shebang("/opt/py", gui=False) == b"#!/opt/py\n"


def test_the_build_environments_interpreter_is_quoted_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build environments live under %TEMP%: a profile name with a space."""
    from kpip.install import wheel_scripts

    monkeypatch.setattr(wheel_scripts.os, "name", "nt")
    monkeypatch.setenv(
        "KPIP_SCRIPT_PYTHON", r"C:\Users\John Smith\AppData\Local\Temp\env\python.exe"
    )

    assert wheel_scripts.shebang(None, gui=False) == (
        b'#!"C:\\Users\\John Smith\\AppData\\Local\\Temp\\env\\python.exe"\n'
    )
