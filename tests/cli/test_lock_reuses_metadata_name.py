"""The lock takes an archive's project name from metadata the resolver read.

It used to unpack and build every URL archive again at the very end of a
lock, only to learn the name its metadata declares. The resolver read that
metadata to resolve the archive and its candidate still holds it; a
candidate without loaded metadata is built as before.
"""

from __future__ import annotations

from types import SimpleNamespace

from kpip.cli.lock import _resolved_metadata_name


def test_the_name_comes_from_the_loaded_metadata() -> None:
    loader = SimpleNamespace(value=SimpleNamespace(name="Werkzeug"), load=lambda: None)
    record = SimpleNamespace(metadata_loader=loader, metadata=lambda: loader.value)
    candidate = SimpleNamespace(record_internal=record, name="werkzeug")

    assert _resolved_metadata_name(candidate) == "Werkzeug"


def test_a_candidate_without_metadata_is_left_to_the_build() -> None:
    assert _resolved_metadata_name(SimpleNamespace(name="x")) is None
    record = SimpleNamespace(metadata_loader=None)
    assert _resolved_metadata_name(SimpleNamespace(record_internal=record)) is None


def test_a_metadata_read_that_fails_is_left_to_the_build() -> None:
    def explode() -> object:
        raise ValueError("bad metadata")

    record = SimpleNamespace(metadata_loader=object(), metadata=explode)

    assert _resolved_metadata_name(SimpleNamespace(record_internal=record)) is None
