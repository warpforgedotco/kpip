import os
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from kpip.core.errors import UnsupportedWheel
from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.core.wheel import WheelCandidate, parse_wheel
from kpip.index.candidate_materialization import (
    CandidateMaterializer,
    CandidateStream,
    LazyWheelCandidate,
    candidate_metadata_fingerprint,
)
from kpip.index.links import Link
from kpip.index.provider import CandidateProvider
from kpip.index.source_models import CandidateRecord, MetadataFile
from kpip.resolution.api import ResolutionEngine
from kpip.core.archive import WheelArchive

from ..wheel_helpers import make_wheel


def make_candidate(version: str) -> WheelCandidate:
    return WheelCandidate(
        name="demo-pkg",
        version=Version(version),
        path=Path(f"demo_pkg-{version}-py3-none-any.whl"),
        dependencies=(),
    )


def test_candidate_stream_materializes_on_demand_and_replays() -> None:
    produced: list[str] = []

    def generate() -> Iterator[WheelCandidate]:
        for version in ("3", "2", "1"):
            produced.append(version)
            yield make_candidate(version)

    stream = CandidateStream(generate())

    assert produced == []
    assert stream
    assert produced == ["3"]
    assert stream[1].version == Version("2")
    assert produced == ["3", "2"]
    assert [candidate.version for candidate in stream] == [
        Version("3"),
        Version("2"),
        Version("1"),
    ]
    assert produced == ["3", "2", "1"]
    assert list(stream) == list(stream)
    assert produced == ["3", "2", "1"]


def test_candidate_stream_replays_terminal_error() -> None:
    error = RuntimeError("materialization failed")

    def generate() -> Iterator[WheelCandidate]:
        yield make_candidate("2")
        raise error

    stream = CandidateStream(generate())

    assert stream[0].version == Version("2")
    with pytest.raises(RuntimeError, match="materialization failed") as first:
        list(stream)
    with pytest.raises(RuntimeError, match="materialization failed") as second:
        list(stream)
    assert first.value is error
    assert second.value is error


def test_lazy_wheel_candidate_loads_release_record_once() -> None:
    record = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_url(
            "https://example.invalid/demo-1.0-py3-none-any.whl",
            source_url=None,
        ),
    )
    calls = 0

    def load_record() -> CandidateRecord:
        nonlocal calls
        calls += 1
        return record

    candidate = LazyWheelCandidate(
        None,
        parse_requirement("demo"),
        CandidateMaterializer(),
        record_loader=load_record,
    )

    assert candidate.version == Version("1.0")
    assert candidate.source_url.endswith("demo-1.0-py3-none-any.whl")
    assert calls == 1


def test_candidate_stream_preference_is_lazy_and_has_fallback() -> None:
    stream = CandidateStream(iter([make_candidate("3"), make_candidate("2")]))

    preferred = stream.prefer(lambda candidate: candidate.version == Version("2"))
    fallback = stream.prefer(lambda candidate: candidate.version == Version("1"))

    assert [candidate.version for candidate in preferred] == [Version("2")]
    assert [candidate.version for candidate in fallback] == [Version("3"), Version("2")]


def test_source_hashes_reuse_the_local_artifact_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "demo-1.0.tar.gz"
    artifact.write_bytes(b"artifact")
    record = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_path(artifact, source_url=None),
    )
    materializer = CandidateMaterializer()
    original_open = open
    reads = 0

    def counting_open(*args: Any, **kwargs: Any) -> Any:
        nonlocal reads
        if args and os.fspath(args[0]) == os.fspath(artifact):
            reads += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)

    assert materializer.source_hashes_for(record) == materializer.source_hashes_for(
        record
    )
    assert reads == 1


def test_file_url_identity_stat_is_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "demo-1.0-py3-none-any.whl"
    artifact.write_bytes(b"artifact")
    original_stat = os.stat
    stats = 0

    def counting_stat(*args: Any, **kwargs: Any) -> Any:
        nonlocal stats
        stats += 1
        return original_stat(*args, **kwargs)

    monkeypatch.setattr("kpip.index.links.os.stat", counting_stat)
    record = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_url(artifact.as_uri(), source_url=None),
    )

    assert candidate_metadata_fingerprint(record).startswith("stat:")
    assert stats == 1


def resolve_names(wheelhouse: Path, requirements: list[str]) -> list[str]:
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    return [candidate.name for candidate in engine.resolve(requirements).candidates]


def test_resolution_reads_only_the_metadata_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution must not decompress WHEEL for candidates it only inspects.

    Locating the ``.dist-info`` directory comes free with the central
    directory ZipFile already parsed; reading WHEEL on top of METADATA
    doubles the member decompressions per candidate, and resolution has no
    use for the text.

    Candidate materialization opens the archive through ``WheelArchive`` --
    a leaner central-directory reader tried ahead of ``zipfile.ZipFile`` --
    so both read entry points are patched here; only whichever one the
    resolve actually took records anything.
    """
    make_wheel(tmp_path, "demo-pkg", "demo_pkg", "1.0.0")

    members: list[str] = []
    original_zipfile_read = zipfile.ZipFile.read
    original_wheel_archive_read = WheelArchive.read

    def recording_zipfile_read(
        self: zipfile.ZipFile,
        name: Any,
        pwd: Any = None,
    ) -> bytes:
        members.append(name if isinstance(name, str) else name.filename)
        return original_zipfile_read(self, name, pwd)

    def recording_wheel_archive_read(self: WheelArchive, name: str) -> bytes:
        members.append(name)
        return original_wheel_archive_read(self, name)

    monkeypatch.setattr(zipfile.ZipFile, "read", recording_zipfile_read)
    monkeypatch.setattr(WheelArchive, "read", recording_wheel_archive_read)

    assert resolve_names(tmp_path, ["demo-pkg"]) == ["demo-pkg"]

    assert any(member.endswith("/METADATA") for member in members)
    assert not [member for member in members if member.endswith("/WHEEL")]


def test_resolution_defers_the_wheel_version_check_to_install(
    tmp_path: Path,
) -> None:
    """A future ``Wheel-Version`` is the installer's to reject, not resolution.

    Skipping the WHEEL read moves this diagnostic from resolve time to
    install time.  Nothing is installed unchecked -- ``parse_wheel`` still
    refuses the same archive -- so the trade is only where it surfaces.
    """
    wheel = make_wheel(tmp_path, "future-pkg", "future_pkg", "3.0", wheel_version="3.0")

    assert resolve_names(tmp_path, ["future-pkg"]) == ["future-pkg"]

    with zipfile.ZipFile(wheel) as archive, pytest.raises(UnsupportedWheel):
        parse_wheel(archive, "future-pkg")


def test_materialized_candidate_keeps_the_resolver_layout(tmp_path: Path) -> None:
    """The layout built during materialization must reach the returned
    candidate: it is what lets the installer open the wheel again without
    another central-directory scan."""
    from kpip.install.wheel_archive_runtime import RawWheelArchive, open_wheel_archive
    from kpip.core import archive

    wheel = _write_wheel_with_script(tmp_path, "layout-pkg", "1.0")
    provider = CandidateProvider.from_options(find_links=[str(tmp_path)], no_index=True)
    [candidate] = list(provider.find_candidates(parse_requirement("layout-pkg")))
    layout = candidate.wheel_layout
    assert isinstance(layout, tuple)
    dist_info, members, root_is_purelib = layout
    assert dist_info == "layout_pkg-1.0.dist-info"
    assert members
    assert all(len(member) == 7 for member in members)
    assert root_is_purelib is True
    with zipfile.ZipFile(wheel) as zip_file:
        expected = {info.filename: info.external_attr for info in zip_file.infolist()}
    assert {member[0]: member[6] for member in members} == expected
    script_mode = expected["layout_pkg-1.0.data/scripts/layout-pkg-tool"] >> 16
    assert script_mode & 0o777 == 0o755

    scans = []
    original = archive.WheelArchive.read_central_directory

    def counting(self):  # noqa: ANN001, ANN202
        scans.append(1)
        return original(self)

    archive.WheelArchive.read_central_directory = counting
    try:
        with open_wheel_archive(candidate.path, candidate) as opened:
            assert isinstance(opened, RawWheelArchive)
            assert f"{dist_info}/METADATA" in opened.namelist()
    finally:
        archive.WheelArchive.read_central_directory = original
    assert scans == []


def _write_wheel_with_script(
    wheelhouse: Path,
    project: str,
    version: str,
    *,
    metadata_compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    """A wheel with an executable script member and a chosen METADATA codec."""
    import stat

    dist = project.replace("-", "_")
    dist_info = f"{dist}-{version}.dist-info"
    path = wheelhouse / f"{dist}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{dist}/__init__.py", b"VALUE = 1\n")
        script = zipfile.ZipInfo(f"{dist}-{version}.data/scripts/{project}-tool")
        script.external_attr = (stat.S_IFREG | 0o755) << 16
        script.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(script, b"#!python\nprint('hi')\n")
        metadata = zipfile.ZipInfo(f"{dist_info}/METADATA")
        metadata.compress_type = metadata_compression
        archive.writestr(
            metadata,
            f"Metadata-Version: 2.1\nName: {project}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return path


def test_unsupported_member_compression_falls_back_to_zipfile(tmp_path: Path) -> None:
    """A METADATA the raw reader cannot decode must still resolve and
    materialize -- through zipfile, as it did before the raw reader was used
    for materialization."""
    from kpip.index.candidate_materialization import _open_resolver_wheel_archive

    wheel = _write_wheel_with_script(
        tmp_path,
        "odd-pkg",
        "1.0",
        metadata_compression=zipfile.ZIP_BZIP2,
    )
    with _open_resolver_wheel_archive(os.fspath(wheel)) as archive:
        assert isinstance(archive, zipfile.ZipFile)
    provider = CandidateProvider.from_options(find_links=[str(tmp_path)], no_index=True)
    [candidate] = list(provider.find_candidates(parse_requirement("odd-pkg")))
    assert candidate.name == "odd-pkg"
    assert str(candidate.version) == "1.0"
    assert isinstance(candidate.wheel_layout, tuple)


_WHEEL_METADATA = (
    "Metadata-Version: 2.1\n"
    "Name: demo\n"
    "Version: 1.0\n"
    "Requires-Dist: downstream>=2\n"
    'Requires-Dist: extradep; extra == "cli"\n'
    "Provides-Extra: cli\n"
)


def _remote_wheel_record() -> CandidateRecord:
    return CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_url(
            "https://example.invalid/demo-1.0-py3-none-any.whl",
            source_url=None,
        ),
    )


class _RangeSession:
    """A transport that can read a wheel's metadata without the wheel."""

    auth = None
    cache = None

    def __init__(self, text: str | None = _WHEEL_METADATA) -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, **kwargs: object) -> object:  # pragma: no cover
        raise AssertionError(f"the whole artifact was fetched: {url}")

    def head(self, url: str, **kwargs: object) -> object:  # pragma: no cover
        raise AssertionError(f"unexpected HEAD: {url}")

    def wheel_metadata_text(self, url: str, name: str) -> str | None:
        self.calls.append((url, name))
        return self.text


class _PlainSession(_RangeSession):
    """A transport with no range-reading extra at all."""

    wheel_metadata_text = None  # type: ignore[assignment]


class TestRangedWheelMetadata:
    def test_reads_metadata_without_fetching_the_wheel(self) -> None:
        session = _RangeSession()
        materializer = CandidateMaterializer(session=session)

        metadata = materializer.ranged_wheel_metadata(
            _remote_wheel_record(),
            frozenset(),
        )

        assert metadata is not None
        assert metadata.name == "demo"
        assert metadata.version == Version("1.0")
        assert [str(dep.name) for dep in metadata.dependencies] == ["downstream"]
        assert metadata.provided_extras == frozenset({"cli"})
        assert session.calls == [
            ("https://example.invalid/demo-1.0-py3-none-any.whl", "demo"),
        ]

    def test_requested_extras_select_their_dependencies(self) -> None:
        materializer = CandidateMaterializer(session=_RangeSession())

        metadata = materializer.ranged_wheel_metadata(
            _remote_wheel_record(),
            frozenset({"cli"}),
        )

        assert metadata is not None
        assert sorted(str(dep.name) for dep in metadata.dependencies) == [
            "downstream",
            "extradep",
        ]

    def test_a_transport_without_the_extra_declines(self) -> None:
        materializer = CandidateMaterializer(session=_PlainSession())

        assert (
            materializer.ranged_wheel_metadata(_remote_wheel_record(), frozenset())
            is None
        )

    def test_no_session_declines(self) -> None:
        materializer = CandidateMaterializer(session=None)

        assert (
            materializer.ranged_wheel_metadata(_remote_wheel_record(), frozenset())
            is None
        )

    def test_an_empty_answer_declines(self) -> None:
        materializer = CandidateMaterializer(session=_RangeSession(text=None))

        assert (
            materializer.ranged_wheel_metadata(_remote_wheel_record(), frozenset())
            is None
        )

    def test_a_local_wheel_is_not_read_over_http(self, tmp_path: Path) -> None:
        session = _RangeSession()
        materializer = CandidateMaterializer(session=session)

        record = CandidateRecord(
            name="demo",
            version=Version("1.0"),
            link=Link.from_url(
                (tmp_path / "demo-1.0-py3-none-any.whl").as_uri(),
                source_url=None,
            ),
        )

        assert materializer.ranged_wheel_metadata(record, frozenset()) is None
        assert session.calls == []


def test_unparsable_metadata_falls_back_instead_of_propagating() -> None:
    """A wheel whose Requires-Dist will not parse should cost a full download,
    not abort the whole metadata load."""
    session = _RangeSession(
        text="Metadata-Version: 2.1\nName: demo\nVersion: 1.0\nRequires-Dist: ==\n",
    )
    materializer = CandidateMaterializer(session=session)

    assert (
        materializer.ranged_wheel_metadata(_remote_wheel_record(), frozenset()) is None
    )
    assert session.calls, "the range read was never attempted"


class _RecordingSession:
    """A session whose HTTP cache holds ``fresh`` and which counts requests."""

    def __init__(self, fresh: set[str]) -> None:
        self.fresh = fresh
        self.requested: list[str] = []

    def has_fresh_cached_response(self, url: str) -> bool:
        return url in self.fresh

    def get(self, url: str) -> object:
        self.requested.append(url)
        return object()


def test_metadata_prefetch_leaves_fresh_cached_responses_to_the_resolver() -> None:
    """A fresh cached response hides no latency; fetching it only builds a client."""
    records = tuple(
        CandidateRecord(
            name="demo",
            version=Version(version),
            link=Link.from_url(
                f"https://example.invalid/demo-{version}-py3-none-any.whl",
                source_url=None,
                metadata_file=MetadataFile(None),
            ),
        )
        for version in ("1.0", "2.0")
    )
    cached = "https://example.invalid/demo-1.0-py3-none-any.whl.metadata"
    missing = "https://example.invalid/demo-2.0-py3-none-any.whl.metadata"
    session = _RecordingSession({cached})
    materializer = CandidateMaterializer(session=session)

    try:
        materializer.prefetch_metadata(records)
        assert materializer.prefetched_metadata_future(cached) is None
        future = materializer.prefetched_metadata_future(missing)
        assert future is not None
        future.result()
    finally:
        materializer.close()

    assert session.requested == [missing]
