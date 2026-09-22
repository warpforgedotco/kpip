"""A hash-less URL artifact's metadata persists under its content hash.

Metadata persists across runs only under an identity that proves the
artifact is the same one. A URL sdist whose link publishes no hash had none,
so it was rebuilt on every lock. Once fetched, its content has one: the
sha256 the lock records anyway. The key is computed only for such
artifacts, never for a VCS checkout, a local file, or a link with a hash.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kpip.core.versions import Version
from kpip.index.candidate_materialization import CandidateMaterializer
from kpip.index.links import Link
from kpip.index.source_models import CandidateRecord


def _materializer(tmp_path: Path) -> CandidateMaterializer:
    return CandidateMaterializer(
        build_options=None,
        build_constraints=(),
        wheel_cache_dir=os.fspath(tmp_path / "wheels"),
        target_key="t",
        build_isolation=True,
        dry_run=False,
        compute_source_hashes=False,
        session=object(),
    )


def _record(url: str, **link_kwargs: Any) -> CandidateRecord:
    return CandidateRecord(
        "demo", Version("1.0"), Link.from_url(url, source_url=None, **link_kwargs)
    )


def test_the_key_is_the_content_hash_of_a_hashless_url_sdist(
    tmp_path: Path, monkeypatch: Any
) -> None:
    materializer = _materializer(tmp_path)
    archive = tmp_path / "demo-1.0.tar.gz"
    archive.write_bytes(b"not really a tarball")
    record = _record("https://files.invalid/demo-1.0.tar.gz")
    monkeypatch.setattr(
        materializer, "ensure_local_text", lambda candidate, **k: os.fspath(archive)
    )

    key = materializer.content_persistent_key(record, frozenset({"x"}))

    import hashlib

    digest = hashlib.sha256(b"not really a tarball").hexdigest()
    # The empty trailing element is the interpreter the metadata was
    # filtered for: nothing targets another one here, so it is the
    # running interpreter.
    assert key == (
        "https://files.invalid/demo-1.0.tar.gz",
        "1.0",
        ("x",),
        f"sha256:{digest}",
        "",
    )


def test_no_key_for_artifacts_that_already_have_an_identity_or_none_at_all(
    tmp_path: Path,
) -> None:
    materializer = _materializer(tmp_path)
    extras: frozenset[str] = frozenset()

    hashed = _record("https://files.invalid/demo-1.0.tar.gz", hashes={"sha256": "ab"})
    assert materializer.content_persistent_key(hashed, extras) is None

    vcs = _record("git+https://example.invalid/demo.git")
    assert materializer.content_persistent_key(vcs, extras) is None

    local = _record(f"file://{tmp_path / 'demo-1.0.tar.gz'}")
    assert materializer.content_persistent_key(local, extras) is None


def test_a_second_run_reads_the_persisted_metadata_instead_of_building(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Two materializers over one cache directory stand in for two runs."""
    from kpip.index.candidate_materialization import CandidateMetadata

    archive = tmp_path / "demo-1.0.tar.gz"
    archive.write_bytes(b"sdist bytes")
    record = _record("https://files.invalid/demo-1.0.tar.gz")
    metadata = CandidateMetadata(
        name="demo",
        version=Version("1.0"),
        dependencies=(),
        provided_extras=frozenset(),
        requires_python=None,
    )

    first = _materializer(tmp_path)
    monkeypatch.setattr(
        first, "ensure_local_text", lambda candidate, **k: os.fspath(archive)
    )
    key = first.content_persistent_key(record, frozenset())
    assert key is not None
    first.persistent_candidate_metadata_cache.put(key, metadata)  # type: ignore[union-attr]
    first.close()

    second = _materializer(tmp_path)
    monkeypatch.setattr(
        second, "ensure_local_text", lambda candidate, **k: os.fspath(archive)
    )
    monkeypatch.setattr(second, "pypi_metadata", lambda *a, **k: None)
    from kpip.build import build_backend

    monkeypatch.setattr(
        build_backend,
        "prepare_project_metadata",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("built")),
    )
    from kpip.core.packaging import parse_requirement

    loaded = second.metadata_loader(record, parse_requirement("demo")).load()

    assert loaded.name == "demo"
    assert loaded.version == Version("1.0")
    second.close()
