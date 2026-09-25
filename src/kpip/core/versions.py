"""PEP 440 versions as their own ordering key.

A :class:`Version` *is* a ``bytes`` string -- the parts
``(epoch, release, suffix, local)`` written so that byte order is PEP 440
order (:func:`version_key`). Sorting, ``max``, dict keys, bisection and the
resolver's interval arithmetic all compare Versions with one ``memcmp`` and
a cached hash, with no Python-level dunder in the way, and there is no
separate "comparison key" to keep in step with the object. The parts
themselves are :attr:`Version.parts`.

The rules that follow from that:

* A Version compares only with a Version. ``version == "1.0"`` is
  ``False`` and ``version < "1.0"`` raises ``TypeError``; parse the text
  first. (Equality with plain bytes of the same key holds, as for any
  bytes subclass -- do not mix the two as keys of one dict.)
* Format with f-strings or ``str()``; indexing or slicing a Version gives
  bytes of its key, not its parts.
* ``marshal`` writes a Version as plain bytes, so one that leaks into an
  on-disk payload comes back as a key, not a Version; :meth:`to_wire`
  produces the record the catalog summaries store and :meth:`from_wire`
  reads it.
* Instances are immutable and interned: ``Version(text)`` returns the
  instance already built for that text while it is in the table, so equal
  texts normally share one object, and the table is bounded and swept.
"""

from __future__ import annotations

from kpip.core.caches import register_table

TYPE_CHECKING = False

if TYPE_CHECKING:
    import re
    from typing import Any


class InvalidVersion(ValueError):
    pass


_version_re: re.Pattern[str] | None = None

_local_separators: re.Pattern[str] | None = None
"""Runs of local-label separators, compiled with :func:`version_re`: only a
version that pattern parses can carry a local label."""


def version_re() -> re.Pattern[str]:
    """The full PEP 440 grammar, compiled on first use.

    ``Version.__new__`` answers a plain dotted-numeric version -- nearly
    everything a resolve parses, and everything an installed-distribution
    listing parses -- without this pattern at all. Compiling it is around half
    a millisecond, and this module is imported by every command that touches a
    requirement, so most of them would pay for a pattern they never match.
    """

    global _version_re, _local_separators

    if _version_re is None:
        # Imported here too: ``re`` costs some 2.5 ms to import, which a
        # plain dotted version never needs.
        import re

        _version_re = re.compile(
            r"""
            ^\s*
            v?
            (?:(?P<epoch>\d+)!)?
            (?P<release>\d+(?:\.\d+)*)
            (?:
                [._-]?
                (?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)
                [._-]?
                (?P<pre_n>\d+)?
            )?
            (?:
                (?:-(?P<post_n1>\d+))
                |
                (?:[._-]?(?P<post_l>post|rev|r)[._-]?(?P<post_n2>\d+)?)
            )?
            (?:
                [._-]?(?P<dev_l>dev)[._-]?(?P<dev_n>\d+)?
            )?
            (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
            \s*$
            """,
            re.IGNORECASE | re.VERBOSE,
        )
        _local_separators = re.compile(r"[-_.]+")

    return _version_re


_PRE_RANK = {
    "a": 0,
    "alpha": 0,
    "b": 1,
    "beta": 1,
    "c": 2,
    "pre": 2,
    "preview": 2,
    "rc": 2,
}
_PRE_LABEL = ("a", "b", "rc")

FINAL_SUFFIX = (3, 0, 0, 0, 1, 0)
_NO_LOCAL: tuple[()] = ()

_VERSIONS_LIMIT = 65536
_versions: dict[str, Version] = register_table({})
# Cleared in place, never rebound, so the bound lookup stays valid.
_versions_get = _versions.get


def _int_code(number: int) -> bytes:
    """``number`` (at least 0) as bytes that sort as the numbers do.

    A length byte, then the big-endian digits without leading zeros: a
    longer number is a larger one, and equal lengths compare digit by digit.
    Self-delimiting, so a code can be followed by anything.
    """
    if number < 256:
        return _INT_CODES[number]
    size = (number.bit_length() + 7) // 8
    if size > 255:
        raise ValueError(f"version number too large: {number}")
    return bytes((size,)) + number.to_bytes(size, "big")


_INT_CODES = [b"\0"] + [bytes((1, number)) for number in range(1, 256)]
_PART_CODES = [b"\1" + code for code in _INT_CODES]
"""A release segment below 256 as it is written into a key, prebuilt."""


def _release_code(release: tuple[int, ...]) -> bytes:
    """A release as sortable bytes: each segment marked, then an end mark.

    The end mark sorts below any segment's, so a release that is a prefix of
    another sorts first, as the shorter tuple does.
    """
    codes = _PART_CODES
    return (
        b"".join(
            [codes[part] if part < 256 else b"\1" + _int_code(part) for part in release]
        )
        + b"\0"
    )


def _suffix_code(suffix: tuple[int, ...]) -> bytes:
    # The first element can be -1 (a bare dev release); shifted to stay >= 0.
    return _int_code(suffix[0] + 1) + b"".join([_int_code(part) for part in suffix[1:]])


def _local_code(local: tuple[tuple[int, Any], ...]) -> bytes:
    """A local label as sortable bytes: each part marked, then an end mark.

    A text part sorts below a numeric one, as its ``(0, text)`` does below
    ``(1, number)``; its text ends with a zero byte, which no label holds.
    """
    if not local:
        return b"\0"
    return (
        b"".join(
            [
                b"\1\0" + value.encode() + b"\0"
                if kind == 0
                else b"\1\1" + _int_code(value)
                for kind, value in local
            ]
        )
        + b"\0"
    )


_FINAL_TAIL = _suffix_code(FINAL_SUFFIX) + _local_code(_NO_LOCAL)


def version_key(
    epoch: int,
    release: tuple[int, ...],
    suffix: tuple[int, ...],
    local: tuple[tuple[int, Any], ...],
) -> bytes:
    """The bytes a Version with these parts is, ``release`` without trailing zeros.

    Every part is written self-delimiting and in its own order, so comparing
    two keys byte by byte compares their parts in turn, as the tuple
    ``(epoch, release, suffix, local)`` would, and equal keys are equal
    versions.
    """
    head = _int_code(epoch) + _release_code(release)
    if suffix == FINAL_SUFFIX and not local:
        return head + _FINAL_TAIL
    return head + _suffix_code(suffix) + _local_code(local)


def release_key(epoch: int, release: tuple[int, ...]) -> bytes:
    """Bytes sorting before every version of ``release``, and after every
    version of an earlier one: the start of their keys."""
    return _int_code(epoch) + _release_code(_trimmed(release))


def _trimmed(release: tuple[int, ...]) -> tuple[int, ...]:
    """A release without trailing zeros, as a Version's key holds it."""
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    return release


def _parse(value: str) -> tuple[int, tuple[int, ...], tuple[int, ...], Any]:
    """``value``'s epoch, release as written, suffix and local label."""
    raw = value.strip()
    if (
        raw
        and raw.replace(".", "").isdecimal()
        and ".." not in raw
        and raw[0] != "."
        and raw[-1] != "."
    ):
        return 0, tuple(map(int, raw.split("."))), FINAL_SUFFIX, _NO_LOCAL
    match = version_re().match(raw)
    if match is None:
        raise InvalidVersion(value)
    (
        epoch_text,
        release_text,
        pre_label,
        pre_number,
        post_number_1,
        post_label,
        post_number_2,
        dev_label,
        dev_number,
        local_text,
    ) = match.groups()
    epoch = int(epoch_text) if epoch_text else 0
    release = tuple(map(int, release_text.split(".")))
    pre = (_PRE_RANK[pre_label.lower()], int(pre_number or 0)) if pre_label else None
    if post_number_1 is not None:
        post: int | None = int(post_number_1)
    elif post_label is not None:
        post = int(post_number_2 or 0)
    else:
        post = None
    dev = int(dev_number or 0) if dev_label is not None else None

    if pre is None and post is None and dev is None:
        suffix = FINAL_SUFFIX
    elif pre is None and post is None:
        suffix = (-1, 0, 0, 0, 0, dev or 0)
    else:
        suffix = (
            3 if pre is None else pre[0],
            0 if pre is None else pre[1],
            0 if post is None else 1,
            0 if post is None else post,
            1 if dev is None else 0,
            dev or 0,
        )

    if local_text is not None:
        # Compiled with the pattern that matched this version.
        separators = _local_separators
        assert separators is not None
        local: Any = tuple(
            (1, int(part)) if part.isdigit() else (0, part)
            for part in separators.sub(".", local_text.lower()).split(".")
        )
    else:
        local = _NO_LOCAL
    return epoch, release, suffix, local


class _FromText:
    """A field of a Version built from its wire record, read from its text.

    A parsed Version holds ``release`` and ``parts`` in its own ``__dict__``,
    which a non-data descriptor defers to. One built by ``from_wire`` holds
    only its key and text, and parses the text here, once, when either is
    first asked for.
    """

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, version: Version | None, owner: type | None = None) -> Any:
        if version is None:
            return self
        epoch, release, suffix, local = _parse(version.public)
        fields = version.__dict__
        fields["release"] = release
        fields["parts"] = (epoch, _trimmed(release), suffix, local)
        return fields[self.name]


class Version(bytes):
    """A parsed PEP 440 version; see the module docstring for the rules."""

    release: tuple[int, ...] = _FromText()  # ty: ignore[invalid-assignment]
    parts: tuple[int, tuple[int, ...], tuple[int, ...], Any] = _FromText()  # ty: ignore[invalid-assignment]
    """``(epoch, release without trailing zeros, suffix, local)``, the fields
    the key is written from, which compare as the key does."""

    def __new__(cls, value: str) -> Version:
        cached = _versions_get(value)
        if cached is not None:
            return cached

        epoch, release, suffix, local = _parse(value)
        normalized = _trimmed(release)
        try:
            key = version_key(epoch, normalized, suffix, local)
        except ValueError as error:
            raise InvalidVersion(value) from error

        self = bytes.__new__(cls, key)
        fields = self.__dict__
        fields["release"] = release
        fields["parts"] = (epoch, normalized, suffix, local)
        if len(_versions) >= _VERSIONS_LIMIT:
            _versions.clear()
        _versions[value] = self
        return self

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"Version is immutable (tried to set {name!r})")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"Version is immutable (tried to delete {name!r})")

    def __reduce__(self) -> tuple[Any, ...]:
        return (Version, (self.public,))

    def __copy__(self) -> Version:
        return self

    def __deepcopy__(self, memo: object) -> Version:
        return self

    @property
    def public(self) -> str:
        """The canonical PEP 440 spelling."""
        fields = self.__dict__
        public = fields.get("public")
        if public is None:
            public = fields["public"] = self._format_public()
        return public

    def _format_public(self) -> str:
        epoch, _release, suffix, local = self.parts
        parts = [f"{epoch}!" if epoch else "", ".".join(map(str, self.release))]
        if suffix != FINAL_SUFFIX:
            pre_rank, pre_number, post_rank, post_number, dev_rank, dev_number = suffix
            if 0 <= pre_rank < 3:
                parts.append(f"{_PRE_LABEL[pre_rank]}{pre_number}")
            if post_rank:
                parts.append(f".post{post_number}")
            if dev_rank == 0:
                parts.append(f".dev{dev_number}")
        if local:
            parts.append("+" + ".".join(str(part[1]) for part in local))
        return "".join(parts)

    def __str__(self) -> str:
        return self.public

    def __repr__(self) -> str:
        return f"<Version({self.public!r})>"

    @property
    def epoch(self) -> int:
        return self.parts[0]

    @property
    def is_prerelease(self) -> bool:
        parts = self.__dict__.get("parts")
        if parts is None:
            # Built from its wire record: the canonical text answers without
            # parsing it, as only a pre or dev release spells "a", "b", "rc"
            # or "dev" before its local label, and "post" shares no letter.
            head = self.public.partition("+")[0]
            return "a" in head or "b" in head or "r" in head or "d" in head
        suffix = parts[2]
        return suffix[0] != 3 or suffix[4] == 0

    @property
    def local(self) -> str | None:
        local = self.parts[3]
        if not local:
            return None
        return ".".join(str(part[1]) for part in local)

    @property
    def public_key(self) -> bytes:
        """The key without the local label: the start of the key of every
        local version of this one's public version."""
        epoch, release, suffix, _local = self.parts
        return version_key(epoch, release, suffix, _NO_LOCAL)[:-1]

    @property
    def base_version(self) -> str:
        """Epoch and release only, without pre/post/dev/local markers."""
        release = ".".join(map(str, self.release))
        epoch = self.parts[0]
        return f"{epoch}!{release}" if epoch else release

    def to_wire(self) -> tuple[str, bytes]:
        """The record cached catalog summaries store: ``(public, key)``.

        The key is kept on disk so a sorted summary can be bisected without
        rebuilding its Versions, and so a Version can be rebuilt from it
        without parsing; the text is the source of truth for everything
        else. One bytes object per version, where the key used to be four
        nested tuples for ``marshal`` to build and free.
        """
        return (self.public, bytes(self))

    @classmethod
    def from_wire(cls, state: Any) -> Version:
        """The Version for a :meth:`to_wire` record, through the intern table.

        Built from the stored key, not by parsing the text again: a warm
        airflow resolve reads 58,000 of these, 17,000 of them new to the
        process. The key is what parsing the text gave when it was stored,
        and the text is the canonical spelling, so the two agree; the parts
        are read back from the text if something asks for them
        (``_FromText``).
        """
        # A hit skips building one, which a warm catalog read does for
        # nearly every version it lists.
        text = state[0]
        cached = _versions_get(text)
        if cached is not None:
            return cached
        self = bytes.__new__(cls, state[1])
        self.__dict__["public"] = text
        if len(_versions) >= _VERSIONS_LIMIT:
            _versions.clear()
        _versions[text] = self
        return self


def version_of(value: Version | str) -> Version | None:
    """The Version for an attribute that may still be text.

    Installed-distribution records carry the version as the text read from
    METADATA or RECORD; comparing that text with a Version must parse it
    first (a Version never compares equal to text). None when the text is
    not a PEP 440 version, which a caller treats as "not the same version".
    """
    if isinstance(value, Version):
        return value
    try:
        return Version(value)
    except InvalidVersion:
        return None


ZERO_VERSION = Version("0")
"""The one "no declared version" sentinel (unknown direct sources, the resolver root)."""
