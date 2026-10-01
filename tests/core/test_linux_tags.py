"""manylinux and musllinux wheel compatibility.

Before these, ``current_platform_tag()`` returned ``linux_x86_64`` and
``platform_matches()`` compared non-Apple platforms by string equality, so no
manylinux or musllinux wheel was ever compatible and every binary package on
Linux fell back to building from source. The tag list stays small -- one libc
tag and the bare ``linux_<arch>`` -- because the matcher compares libc
versions rather than enumerating every tag the host satisfies.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from kpip.core import libc, wheel
from kpip.core.utils import CURRENT_PYTHON_VERSION_DIGITS
from kpip.core.wheel import (
    WheelTag,
    _parse_wheel_filename,
    linux_platform_parts,
    tag_matches,
    wheel_tag_rank,
)


@pytest.fixture
def linux_host(monkeypatch: pytest.MonkeyPatch):
    """Drive the real tag code with a chosen platform string and libc."""

    def configure(platform_tag: str, detected: tuple[str, int, int] | None):
        monkeypatch.setattr(wheel, "current_platform_tag", lambda: platform_tag)
        monkeypatch.setattr(wheel, "detect", lambda: detected)
        wheel.current_platform_tags.cache_clear()
        wheel._supported_wheel_tags.cache_clear()
        return wheel.supported_wheel_tags()

    yield configure
    wheel.current_platform_tags.cache_clear()
    wheel._supported_wheel_tags.cache_clear()


CP = CURRENT_PYTHON_VERSION_DIGITS
OLD = "39"


def _wheel(name: str) -> str:
    """A fixture filename tagged for the interpreter actually running."""
    return name.format(V=CP, OLD=OLD)


def _rank(filename: str, supported: tuple[WheelTag, ...]) -> int | None:
    filename = _wheel(filename)
    parsed = _parse_wheel_filename(filename)
    assert parsed is not None, filename
    return wheel_tag_rank(tuple(parsed.tags), supported)


@pytest.mark.parametrize(
    "filename",
    [
        "numpy-2.1.0-cp{V}-cp{V}-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "lxml-5.3.0-cp{V}-cp{V}-manylinux_2_28_x86_64.whl",
        "charset_normalizer-3.4.0-cp{V}-cp{V}-manylinux2014_x86_64.whl",
        "old-1.0-cp{V}-cp{V}-manylinux2010_x86_64.whl",
        "older-1.0-cp{V}-cp{V}-manylinux1_x86_64.whl",
        "stable-1.0-cp{OLD}-abi3-manylinux_2_28_x86_64.whl",
        "plain-1.0-cp{V}-cp{V}-linux_x86_64.whl",
        "pure-1.0-py3-none-any.whl",
    ],
)
def test_glibc_host_accepts_manylinux(linux_host, filename: str) -> None:
    supported = linux_host("linux_x86_64", ("glibc", 2, 39))
    assert _rank(filename, supported) is not None


@pytest.mark.parametrize(
    "filename",
    [
        # A glibc newer than the host's.
        "toonew-1.0-cp{V}-cp{V}-manylinux_2_99_x86_64.whl",
        # Another architecture.
        "wrongarch-1.0-cp{V}-cp{V}-manylinux_2_17_aarch64.whl",
        # The other libc.
        "musl-1.0-cp{V}-cp{V}-musllinux_1_2_x86_64.whl",
        # Another operating system.
        "win-1.0-cp{V}-cp{V}-win_amd64.whl",
        "mac-1.0-cp{V}-cp{V}-macosx_11_0_arm64.whl",
    ],
)
def test_glibc_host_rejects_incompatible(linux_host, filename: str) -> None:
    supported = linux_host("linux_x86_64", ("glibc", 2, 39))
    assert _rank(filename, supported) is None


def test_musl_host_accepts_musllinux_only(linux_host) -> None:
    supported = linux_host("linux_x86_64", ("musl", 1, 2))
    assert _rank("p-1.0-cp{V}-cp{V}-musllinux_1_2_x86_64.whl", supported) is not None
    assert _rank("p-1.0-cp{V}-cp{V}-musllinux_1_1_x86_64.whl", supported) is not None
    assert _rank("p-1.0-cp{V}-cp{V}-musllinux_1_9_x86_64.whl", supported) is None
    assert _rank("p-1.0-cp{V}-cp{V}-manylinux_2_17_x86_64.whl", supported) is None
    assert _rank("p-1.0-cp{V}-cp{V}-linux_x86_64.whl", supported) is not None


def test_unidentifiable_libc_falls_back_to_bare_linux(linux_host) -> None:
    """No libc, no claim: only what sysconfig itself reports is compatible."""
    supported = linux_host("linux_x86_64", None)
    assert supported[0].platform == "linux_x86_64"
    assert _rank("p-1.0-cp{V}-cp{V}-manylinux_2_17_x86_64.whl", supported) is None
    assert _rank("p-1.0-cp{V}-cp{V}-linux_x86_64.whl", supported) is not None


def test_libc_tag_is_preferred_over_bare_linux(linux_host) -> None:
    supported = linux_host("linux_x86_64", ("glibc", 2, 39))
    manylinux = _rank("p-1.0-cp{V}-cp{V}-manylinux_2_17_x86_64.whl", supported)
    plain = _rank("p-1.0-cp{V}-cp{V}-linux_x86_64.whl", supported)
    assert manylinux is not None
    assert plain is not None
    assert manylinux < plain


def test_supported_tag_list_stays_small(linux_host) -> None:
    """One libc tag plus linux_<arch>, not one tag per glibc minor version."""
    supported = linux_host("linux_x86_64", ("glibc", 2, 39))
    platforms = [tag.platform for tag in supported]
    assert set(platforms) == {"manylinux_2_39_x86_64", "linux_x86_64", "any"}


@pytest.mark.parametrize(
    "runtime, wheel_tag, expected",
    [
        # PEP 600: a newer glibc satisfies every older tag, across majors.
        ("manylinux_2_28_x86_64", "manylinux_2_17_x86_64", True),
        ("manylinux_3_0_x86_64", "manylinux_2_17_x86_64", True),
        ("manylinux_2_17_x86_64", "manylinux_2_28_x86_64", False),
        # The pre-600 aliases mean the glibc versions PEP 599/571/513 name.
        ("manylinux_2_17_x86_64", "manylinux2014_x86_64", True),
        ("manylinux2014_x86_64", "manylinux_2_17_x86_64", True),
        ("manylinux_2_17_x86_64", "manylinux2010_x86_64", True),
        ("manylinux_2_12_x86_64", "manylinux2014_x86_64", False),
        # Architecture is never coerced.
        ("manylinux_2_28_x86_64", "manylinux_2_17_aarch64", False),
        # The two libc families never satisfy one another.
        ("manylinux_2_28_x86_64", "musllinux_1_2_x86_64", False),
        ("musllinux_1_2_x86_64", "manylinux_2_17_x86_64", False),
        ("musllinux_1_2_x86_64", "musllinux_1_1_x86_64", True),
        ("musllinux_1_1_x86_64", "musllinux_1_2_x86_64", False),
    ],
)
def test_libc_version_matching(runtime: str, wheel_tag: str, expected: bool) -> None:
    assert (
        tag_matches(
            WheelTag("cp312", "cp312", runtime),
            WheelTag("cp312", "cp312", wheel_tag),
        )
        is expected
    )


@pytest.mark.parametrize(
    "platform_tag, expected",
    [
        ("manylinux_2_17_x86_64", ("manylinux", 2, 17, "x86_64")),
        ("musllinux_1_2_aarch64", ("musllinux", 1, 2, "aarch64")),
        ("manylinux2014_x86_64", ("manylinux", 2, 17, "x86_64")),
        ("manylinux2010_i686", ("manylinux", 2, 12, "i686")),
        ("manylinux1_x86_64", ("manylinux", 2, 5, "x86_64")),
        ("linux_x86_64", None),
        ("manylinux_x_y_x86_64", None),
        ("manylinux2014", None),
        ("manylinux_2_17", None),
    ],
)
def test_linux_platform_parts(platform_tag: str, expected: tuple | None) -> None:
    assert linux_platform_parts(platform_tag) == expected


def test_manylinux_arch_gate() -> None:
    """The tag carries no ABI claim on these, so no ELF check is needed."""
    assert libc.manylinux_arch_supported("x86_64")
    assert libc.manylinux_arch_supported("aarch64")
    assert libc.manylinux_arch_supported("s390x")
    # i686 and armv7l do assert an ABI, checked against this interpreter's
    # own ELF header -- which on a non-ELF platform cannot be read at all.
    assert not libc.manylinux_arch_supported("nonesuch")


def test_detect_is_none_off_linux() -> None:
    """Nothing here should claim a libc on macOS or Windows."""
    import sys

    if not sys.platform.startswith("linux"):
        assert libc.detect() is None


def _i386_elf(path: Path) -> str:
    """The ELF header of a 32-bit little-endian i386 executable, alone."""
    ident = b"\x7fELF" + bytes((1, 1, 1)) + bytes(9)
    header = struct.pack("<HHIIIIIHHH", 2, 3, 1, 0, 52, 0, 0, 52, 32, 0)
    path.write_bytes(ident + header + bytes(8))
    return str(path)


class _Target:
    def __init__(self, executable: str, *, is_own: bool) -> None:
        self.executable = executable
        self.is_own = is_own


def test_the_elf_header_is_the_target_interpreters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not sys.executable's: compiled, no file is there."""
    target = _Target(_i386_elf(tmp_path / "python"), is_own=False)
    monkeypatch.setattr(libc, "target_interpreter", lambda **_: target)

    assert libc.manylinux_arch_supported("i686")
    assert not libc.manylinux_arch_supported("armv7l")


def test_a_compiled_kpip_without_a_target_reads_its_own_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = _i386_elf(tmp_path / "kpip")
    missing = _Target(str(tmp_path / "python"), is_own=True)
    monkeypatch.setattr(libc, "target_interpreter", lambda **_: missing)
    monkeypatch.setattr(libc, "is_compiled", lambda: True)
    monkeypatch.setattr(libc, "own_command", lambda: [binary])

    assert libc.manylinux_arch_supported("i686")


class _Probed:
    def __init__(self, platform: str, pointer_bits: int) -> None:
        self.platform = platform
        self.pointer_bits = pointer_bits


@pytest.mark.parametrize(
    "kernel, bits, expected",
    [
        ("linux-x86_64", 32, "linux_i686"),
        ("linux-aarch64", 32, "linux_armv8l"),
        ("linux-x86_64", 64, "linux_x86_64"),
        ("linux-armv7l", 32, "linux_armv7l"),
    ],
)
def test_a_32_bit_python_on_a_64_bit_kernel_takes_32_bit_wheels(
    monkeypatch: pytest.MonkeyPatch, kernel: str, bits: int, expected: str
) -> None:
    """sysconfig reads uname, which names the kernel, not the interpreter."""
    monkeypatch.setattr(wheel.sys, "platform", "linux")
    monkeypatch.setattr(
        wheel.interpreter_facts,
        "target_interpreter",
        lambda **_: _Probed(kernel, bits),
    )
    assert wheel.current_platform_tag() == expected


def test_armv8l_also_takes_armv7l_wheels(
    linux_host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wheel, "manylinux_arch_supported", lambda arch: True)
    supported = linux_host("linux_armv8l", ("glibc", 2, 31))
    platforms = list(dict.fromkeys(tag.platform for tag in supported))
    assert platforms == [
        "manylinux_2_31_armv8l",
        "manylinux_2_31_armv7l",
        "linux_armv8l",
        "linux_armv7l",
        "any",
    ]
