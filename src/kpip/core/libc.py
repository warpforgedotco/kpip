"""Which C library this interpreter is linked against, and at what version.

A Linux wheel is not tagged ``linux_x86_64``; almost every wheel on PyPI is
tagged ``manylinux_2_17_x86_64`` or ``musllinux_1_2_x86_64``, and whether one
of those is installable here is a fact about the running libc, not about
``sysconfig.get_platform()``. Without this module every binary package on
Linux falls back to building from source.

Detection is ordered by cost, and stops as soon as it has an answer:

1. ``os.confstr("CS_GNU_LIBC_VERSION")`` -- one syscall-free lookup, and it
   answers on every glibc system, which is nearly all of them;
2. ``gnu_get_libc_version`` through ctypes, for a glibc that does not publish
   the confstr;
3. the ELF ``PT_INTERP`` of the running interpreter, and if that names musl,
   the loader is run to print its version banner.

Only step 3 costs a subprocess, and only on musl. Everything is memoized for
the life of the process.
"""

from __future__ import annotations

import os
import re
import struct
import sys

from kpip.core.caches import memoized

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import IO


GLIBC = "glibc"
MUSL = "musl"


MANYLINUX_ARCHES = frozenset(
    (
        "x86_64",
        "aarch64",
        "ppc64",
        "ppc64le",
        "s390x",
        "loongarch64",
        "riscv64",
    ),
)
"""Architectures a manylinux wheel may be built for without an ABI check.

``i686`` and ``armv7l`` are absent deliberately: on those two the tag also
asserts an ABI (32-bit x86, and ARM hard-float EABI5), which is read off the
interpreter's own ELF header instead.
"""

LEGACY_MANYLINUX = {
    "manylinux1": (2, 5),
    "manylinux2010": (2, 12),
    "manylinux2014": (2, 17),
}
"""The pre-PEP 600 aliases, as the glibc version each one stands for."""


class _ELFInvalid(ValueError):
    pass


class _ELFFile:
    """Just enough of the ELF header to answer the two questions asked here.

    See https://refspecs.linuxfoundation.org/elf/gabi4+/ch4.eheader.html.
    """

    __slots__ = (
        "_e_phentsize",
        "_e_phnum",
        "_e_phoff",
        "_file",
        "_p_fmt",
        "_p_idx",
        "capacity",
        "encoding",
        "flags",
        "machine",
    )

    def __init__(self, file: IO[bytes]) -> None:
        self._file = file
        try:
            ident = self._read("16B")
        except struct.error as error:
            raise _ELFInvalid("unable to parse identification") from error
        if bytes(ident[:4]) != b"\x7fELF":
            raise _ELFInvalid("not an ELF file")

        self.capacity = ident[4]  # 1 = 32-bit, 2 = 64-bit.
        self.encoding = ident[5]  # 1 = little endian, 2 = big endian.

        try:
            header_format, self._p_fmt, self._p_idx = _ELF_FORMATS[
                (self.capacity, self.encoding)
            ]
        except KeyError as error:
            raise _ELFInvalid("unrecognized ELF capacity or encoding") from error

        try:
            (
                _,
                self.machine,
                _,
                _,
                self._e_phoff,
                _,
                self.flags,
                _,
                self._e_phentsize,
                self._e_phnum,
            ) = self._read(header_format)
        except struct.error as error:
            raise _ELFInvalid("unable to parse ELF header") from error

    def _read(self, fmt: str) -> tuple[int, ...]:
        return struct.unpack(fmt, self._file.read(struct.calcsize(fmt)))

    @property
    def interpreter(self) -> str | None:
        """The path in the ``PT_INTERP`` program header, if there is one."""
        for index in range(self._e_phnum):
            self._file.seek(self._e_phoff + self._e_phentsize * index)
            try:
                entry = self._read(self._p_fmt)
            except struct.error:
                continue
            if entry[self._p_idx[0]] != 3:  # Not PT_INTERP.
                continue
            self._file.seek(entry[self._p_idx[1]])
            return os.fsdecode(self._file.read(entry[self._p_idx[2]])).strip("\0")
        return None


_ELF_FORMATS = {
    (1, 1): ("<HHIIIIIHHH", "<IIIIIIII", (0, 1, 4)),
    (1, 2): (">HHIIIIIHHH", ">IIIIIIII", (0, 1, 4)),
    (2, 1): ("<HHIQQQIHHH", "<IIQQQQQQ", (0, 2, 5)),
    (2, 2): (">HHIQQQIHHH", ">IIQQQQQQ", (0, 2, 5)),
}

_EM_386 = 3
_EM_ARM = 40
_EI_CLASS_32 = 1
_EI_DATA_LSB = 1
_EF_ARM_ABIMASK = 0xFF000000
_EF_ARM_ABI_VER5 = 0x05000000
_EF_ARM_ABI_FLOAT_HARD = 0x00000400


def _open_elf(path: str) -> _ELFFile | None:
    try:
        with open(path, "rb") as handle:
            return _ELFFile(handle)
    except (OSError, TypeError, ValueError):
        return None


@memoized(1)
def _interpreter_elf_facts() -> tuple[int, int, int, int] | None:
    """``(capacity, encoding, machine, flags)`` for the running interpreter."""
    try:
        with open(sys.executable, "rb") as handle:
            elf = _ELFFile(handle)
            return (elf.capacity, elf.encoding, elf.machine, elf.flags)
    except (OSError, TypeError, ValueError):
        return None


@memoized(1)
def _interpreter_elf_interpreter() -> str | None:
    try:
        with open(sys.executable, "rb") as handle:
            return _ELFFile(handle).interpreter
    except (OSError, TypeError, ValueError):
        return None


def manylinux_arch_supported(arch: str) -> bool:
    """Whether a manylinux wheel for ``arch`` can run on this interpreter.

    For most architectures the tag carries no ABI claim beyond the
    architecture itself. ``i686`` and ``armv7l`` do, and the claim is checked
    against the interpreter's own ELF header.
    """
    if arch in MANYLINUX_ARCHES:
        return True
    facts = _interpreter_elf_facts()
    if facts is None:
        return False
    capacity, encoding, machine, flags = facts
    if arch == "i686":
        return (
            capacity == _EI_CLASS_32 and encoding == _EI_DATA_LSB and machine == _EM_386
        )
    if arch == "armv7l":
        return (
            capacity == _EI_CLASS_32
            and encoding == _EI_DATA_LSB
            and machine == _EM_ARM
            and flags & _EF_ARM_ABIMASK == _EF_ARM_ABI_VER5
            and flags & _EF_ARM_ABI_FLOAT_HARD == _EF_ARM_ABI_FLOAT_HARD
        )
    return False


_GLIBC_VERSION_RE = re.compile(r"(\d+)\.(\d+)")
_MUSL_VERSION_RE = re.compile(r"Version (\d+)\.(\d+)")


def _glibc_version_from_confstr() -> tuple[int, int] | None:
    try:
        text = os.confstr("CS_GNU_LIBC_VERSION")
    except (AttributeError, OSError, ValueError):
        return None
    if not text:
        return None
    # "glibc 2.39"; a forked glibc may append junk ("2.20-2014.11"), which the
    # two-component match discards.
    match = _GLIBC_VERSION_RE.search(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _glibc_version_from_ctypes() -> tuple[int, int] | None:
    try:
        import ctypes
    except ImportError:
        return None
    try:
        namespace = ctypes.CDLL(None)
    except (OSError, TypeError):
        # A statically linked or musl interpreter: dlopen(NULL) fails.
        return None
    try:
        get_version = namespace.gnu_get_libc_version
    except AttributeError:
        return None
    get_version.restype = ctypes.c_char_p
    try:
        raw = get_version()
    except Exception:
        return None
    text = raw.decode("ascii", "replace") if isinstance(raw, bytes) else str(raw)
    match = _GLIBC_VERSION_RE.match(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _musl_version() -> tuple[int, int] | None:
    loader = _interpreter_elf_interpreter()
    if loader is None or "musl" not in loader:
        return None
    import subprocess

    try:
        completed = subprocess.run(  # noqa: S603
            [loader],
            check=False,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [
        line for line in (raw.strip() for raw in completed.stderr.splitlines()) if line
    ]
    if len(lines) < 2 or lines[0][:4] != "musl":
        return None
    match = _MUSL_VERSION_RE.match(lines[1])
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


@memoized(1)
def detect() -> tuple[str, int, int] | None:
    """``(kind, major, minor)`` for this interpreter's libc, or None.

    None means "not a Linux interpreter, or the libc could not be
    identified" -- in which case no manylinux or musllinux wheel is
    considered compatible, which is the conservative answer.
    """
    if not sys.platform.startswith("linux"):
        return None
    glibc = _glibc_version_from_confstr() or _glibc_version_from_ctypes()
    if glibc is not None:
        return (GLIBC, glibc[0], glibc[1])
    musl = _musl_version()
    if musl is not None:
        return (MUSL, musl[0], musl[1])
    return None
