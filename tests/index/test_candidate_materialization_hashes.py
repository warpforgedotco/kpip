"""User hashes are checked on every archive the materializer hands out."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from kpip.core.errors import HashMismatch
from kpip.core.hashes import Hashes
from kpip.index.candidate_materialization import CandidateMaterializer


def _candidate(path: Path) -> SimpleNamespace:
    link = SimpleNamespace(
        url=path.as_uri(),
        is_vcs=False,
        is_file=True,
        file_path=str(path),
        hashes=None,
    )
    return SimpleNamespace(link=link, canonical_name="demo", name="demo", version="1.0")


def test_a_mismatch_is_raised_every_time_not_only_the_first(tmp_path: Path) -> None:
    """A caller that catches the mismatch must not leave the path cached for
    the next caller to receive unchecked."""
    archive = tmp_path / "demo-1.0-py3-none-any.whl"
    archive.write_bytes(b"not what was hashed")
    materializer = CandidateMaterializer(
        user_hashes=lambda name: Hashes({"sha256": ["0" * 64]})
    )
    candidate = _candidate(archive)

    for _ in range(2):
        with pytest.raises(HashMismatch):
            materializer.ensure_local_text(candidate)  # type: ignore[arg-type]


def test_a_path_cached_by_another_route_is_still_checked(tmp_path: Path) -> None:
    archive = tmp_path / "demo-1.0-py3-none-any.whl"
    archive.write_bytes(b"not what was hashed")
    materializer = CandidateMaterializer(
        user_hashes=lambda name: Hashes({"sha256": ["0" * 64]})
    )
    candidate = _candidate(archive)
    materializer.local_path_for(candidate)  # type: ignore[arg-type]

    with pytest.raises(HashMismatch):
        materializer.ensure_local_text(candidate)  # type: ignore[arg-type]


def test_a_matching_archive_is_handed_out(tmp_path: Path) -> None:
    import hashlib

    archive = tmp_path / "demo-1.0-py3-none-any.whl"
    archive.write_bytes(b"the real archive")
    digest = hashlib.sha256(b"the real archive").hexdigest()
    materializer = CandidateMaterializer(
        user_hashes=lambda name: Hashes({"sha256": [digest]})
    )

    assert materializer.ensure_local_text(_candidate(archive)) == str(archive)  # type: ignore[arg-type]
