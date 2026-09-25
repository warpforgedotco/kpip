"""The resolver graphs uv's own CodSpeed suite measures, replayed offline.

uv's ``crates/uv-bench/benches/uv.rs`` resolves three real PyPI graphs from
a warm cache: ``jupyter==1.0.0`` for one environment, the same requirement
universally, and ``apache-airflow[all]==2.9.3`` with the Apache Beam
provider. Each is pinned in time -- ``exclude_newer`` of 2024-09-01 -- and
resolved for CPython 3.11 on an arm64 Mac, so the graph does not move with
PyPI or with the machine running it.

The single-environment graphs are mirrored here with the same inputs: the
same requirements, the same upload cutoff, the same marker environment, and
wheel tags for the same interpreter and platform. (kpip resolves for one
environment, so uv's universal variant has no counterpart.)

Nothing here touches the network while benchmarking. ``python uv_graphs.py
capture`` resolves each graph once against real PyPI through a
:class:`RecordingSession` and writes every response it received into
``corpus/uv_graphs/<name>/``: project pages in ``pages.zip``, everything
else -- ``.metadata`` files, JSON API reads, source distributions -- in
``files.zip``. The benchmarks replay them through a :class:`ReplaySession`,
which answers only requests it has a recording for and fails on anything
else, so a graph that drifts from its corpus is an error rather than a
silent network fetch.

A change to kpip can make a resolve read something the corpus does not
hold -- a prefetch reaching one release further, say. ``python uv_graphs.py
fill`` resolves from the corpus, fetches only what is missing, and adds it.
The archives are written byte-for-byte reproducibly, so one that gained
nothing is left as it was.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from kpip._vendor.urllib3.response import HTTPResponse
from kpip.core import packaging
from kpip.core.wheel import TargetContext
from kpip.index.provider import CandidateProvider
from kpip.network.session import NetworkSession
from kpip.resolution.api import ResolutionEngine

CORPUS = Path(__file__).with_name("corpus") / "uv_graphs"

INDEX_URL = "https://pypi.org/simple"

# uv-bench's ``ExcludeNewer::global(2024-09-01)``.
UPLOADED_PRIOR_TO = datetime.datetime(2024, 9, 1, tzinfo=datetime.timezone.utc)

# uv-bench's ``MARKERS``: CPython 3.11.5 on an arm64 Mac.
MARKERS = {
    "implementation_name": "cpython",
    "implementation_version": "3.11.5",
    "os_name": "posix",
    "platform_machine": "arm64",
    "platform_python_implementation": "CPython",
    "platform_release": "21.6.0",
    "platform_system": "Darwin",
    "platform_version": (
        "Darwin Kernel Version 21.6.0: Mon Aug 22 20:19:52 PDT 2022; "
        "root:xnu-8020.140.49~2/RELEASE_ARM64_T6000"
    ),
    "python_full_version": "3.11.5",
    "python_version": "3.11",
    "sys_platform": "darwin",
}

# uv-bench's ``TAGS``: ``Platform::new(Os::Macos { major: 21, minor: 6 },
# Arch::Aarch64)`` for CPython 3.11.
TARGET = TargetContext(
    platforms=("macosx_21_6_arm64",),
    implementation="cp",
    python_version="3.11",
)

WORKLOADS: dict[str, tuple[str, ...]] = {
    "jupyter": ("jupyter==1.0.0",),
    "airflow": (
        "apache-airflow[all]==2.9.3",
        "apache-airflow-providers-apache-beam>3.0.0",
    ),
}

# Replayed pages stay fresh for the life of a benchmark run: a warm resolve
# must never find one stale and revalidate it.
_FRESH = "max-age=315360000"

_DROPPED_HEADERS = frozenset(
    ("cache-control", "content-encoding", "content-length", "transfer-encoding")
)


@contextlib.contextmanager
def uv_environment() -> Iterator[None]:
    """Resolve as uv-bench does: CPython 3.11.5 markers on an arm64 Mac.

    ``set_target_python_version`` moves Requires-Python and the version
    markers; the platform markers have no option of their own, so the one
    function that answers them is replaced for the duration.
    """
    previous_target = packaging.target_python_version()
    previous_environment = packaging.default_environment

    def environment(extra: str | None = None) -> dict[str, str]:
        return {**MARKERS, "extra": extra or ""}

    packaging.set_target_python_version(MARKERS["python_full_version"])
    packaging.default_environment = environment  # type: ignore[assignment]
    try:
        yield
    finally:
        packaging.default_environment = previous_environment
        packaging.set_target_python_version(previous_target)


def resolve(name: str, session: NetworkSession, cache_dir: str) -> Any:
    """One resolve of a workload, set up the way ``kpip lock`` sets it up."""
    return ResolutionEngine(
        provider=CandidateProvider.from_options(
            index_url=INDEX_URL,
            session=session,
            wheel_cache_dir=cache_dir,
            dry_run=True,
            target=TARGET,
            uploaded_prior_to=UPLOADED_PRIOR_TO,
        ),
        ignore_installed=True,
        python_version=MARKERS["python_full_version"],
    ).resolve(list(WORKLOADS[name]))


Recording = dict[str, tuple[int, str, dict[str, str], bytes]]

_ARCHIVES = ("pages", "files")


def _archive_for(key: str) -> str:
    return "pages" if "/simple/" in key else "files"


def load(name: str) -> Recording:
    """Every recorded response of a workload, keyed as requests are."""
    recording: Recording = {}
    for archive_name in _ARCHIVES:
        path = CORPUS / name / f"{archive_name}.zip"
        if not path.exists():
            continue
        with zipfile.ZipFile(path) as archive:
            for key, status, reason, headers, member in json.loads(
                archive.read("index.json")
            ):
                recording[key] = (status, reason, headers, archive.read(member))
    return recording


def save(name: str, recording: Recording) -> None:
    """Write a workload's responses, the same bytes for the same content."""
    directory = CORPUS / name
    directory.mkdir(parents=True, exist_ok=True)

    def add(archive: zipfile.ZipFile, member: str, data: bytes) -> None:
        info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_LZMA
        archive.writestr(info, data)

    for archive_name in _ARCHIVES:
        entries = []
        with zipfile.ZipFile(directory / f"{archive_name}.zip", "w") as archive:
            members = sorted(
                (key, response)
                for key, response in recording.items()
                if _archive_for(key) == archive_name
            )
            for index, (key, (status, reason, headers, data)) in enumerate(members):
                member = f"bodies/{index}"
                add(archive, member, data)
                entries.append([key, status, reason, headers, member])
            add(archive, "index.json", json.dumps(entries, indent=0).encode())


def _key(method: str, url: str, headers: dict[str, str]) -> str:
    ranges = {name.lower(): value for name, value in headers.items()}.get("range")
    return f"{method} {url}" if ranges is None else f"{method} {url} {ranges}"


def _response(
    url: str,
    status: int,
    reason: str,
    headers: dict[str, str],
    body: bytes,
    *,
    stream: bool,
) -> HTTPResponse:
    return HTTPResponse(
        body=io.BytesIO(body) if stream or not body else body,
        headers={**headers, "Content-Length": str(len(body))},
        status=status,
        reason=reason,
        preload_content=not stream,
        decode_content=False,
        request_url=url,
    )


class RecordingSession(NetworkSession):
    """A live session that keeps a copy of every response it hands back."""

    def __init__(self, replay: Recording | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.replay: Recording = replay or {}
        self.recorded: Recording = {}

    def open_internal(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: Any,
        *,
        stream: bool = False,
    ) -> HTTPResponse:
        replayed = self.replay.get(_key(method, url, headers))
        if replayed is not None:
            status, reason, kept, data = replayed
            return _response(url, status, reason, kept, data, stream=stream)
        live = super().open_internal(method, url, headers, body, timeout)
        data = live.data
        kept = {
            name: value
            for name, value in live.headers.items()
            if name.lower() not in _DROPPED_HEADERS
        }
        self.recorded[_key(method, url, headers)] = (
            live.status,
            live.reason or "",
            kept,
            data,
        )
        return _response(url, live.status, live.reason or "", kept, data, stream=stream)


class ReplaySession(NetworkSession):
    """A session that answers from a recorded corpus and nothing else."""

    def __init__(self, name: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.responses: Recording = {
            key: (status, reason, {**headers, "Cache-Control": _FRESH}, data)
            for key, (status, reason, headers, data) in load(name).items()
        }
        self.requests = 0

    def open_internal(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: Any,
        *,
        stream: bool = False,
    ) -> HTTPResponse:
        key = _key(method, url, headers)
        recorded = self.responses.get(key)
        if recorded is None:
            raise AssertionError(f"no recorded response for {key}; recapture")
        self.requests += 1
        status, reason, kept, data = recorded
        return _response(url, status, reason, kept, data, stream=stream)


@contextlib.contextmanager
def recording_builds() -> Iterator[dict[tuple[str, str], str]]:
    """Keep the core metadata of every source distribution a resolve builds.

    Keyed by canonical name and version, valued as the ``PKG-INFO`` kpip's
    own backend would write for it.
    """
    from kpip.build import build_backend

    built: dict[tuple[str, str], str] = {}
    prepare = build_backend.prepare_project_metadata

    def recording(*args: Any, **kwargs: Any) -> Any:
        project = prepare(*args, **kwargs)
        key = (packaging.canonicalize_name(project.name), project.version)
        built[key] = build_backend.metadata_text(project, for_sdist=True)
        return project

    build_backend.prepare_project_metadata = recording
    try:
        yield built
    finally:
        build_backend.prepare_project_metadata = prepare


def _static_sdist(filename: str, pkg_info: str) -> bytes:
    """An sdist whose only member is a PEP 643 static ``PKG-INFO``."""
    import tarfile

    root = filename.removesuffix(".tar.gz")
    data = pkg_info.encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        member = tarfile.TarInfo(f"{root}/PKG-INFO")
        member.size = len(data)
        member.mtime = 0
        tar.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def _replace_sdists(
    recorded: Recording,
    built: dict[tuple[str, str], str],
    new: set[str],
) -> list[str]:
    """Swap each built sdist for a static one carrying what its build said.

    A warm resolve reads a built sdist's metadata from the cache and never
    opens the archive, so the archive matters only to the priming resolve --
    which would otherwise run every build backend again, and whose corpus
    would carry every archive (pyspark's alone is 320 MB). The page entry
    naming the artifact gets the replacement's digest and size; nothing else
    on the page changes. Only ``new`` responses are replaced: one already in
    the corpus was replaced when it was recorded. Returns the sdists left as
    downloaded, because no build of them was seen.
    """
    import hashlib

    from kpip.core.versions import Version

    replaced: dict[str, tuple[str, int]] = {}
    kept = []
    for key, (status, reason, headers, data) in list(recorded.items()):
        if key not in new:
            continue
        method, url = key.split()[:2]
        filename = url.rsplit("/", 1)[-1]
        if method != "GET" or not filename.endswith(".tar.gz"):
            continue
        project, _, version = filename.removesuffix(".tar.gz").rpartition("-")
        pkg_info = built.get(
            (packaging.canonicalize_name(project), str(Version(version)))
        )
        if pkg_info is None:
            kept.append(filename)
            continue
        body = _static_sdist(filename, pkg_info)
        recorded[key] = (status, reason, headers, body)
        replaced[url] = (hashlib.sha256(body).hexdigest(), len(body))

    for key, (status, reason, headers, data) in list(recorded.items()):
        content_type = {k.lower(): v for k, v in headers.items()}.get("content-type")
        if not replaced or content_type != "application/vnd.pypi.simple.v1+json":
            continue
        page = json.loads(data)
        changed = False
        for entry in page.get("files", ()):
            replacement = replaced.get(entry.get("url"))
            if replacement is not None:
                entry["hashes"] = {"sha256": replacement[0]}
                entry["size"] = replacement[1]
                changed = True
        if changed:
            body = json.dumps(page, separators=(",", ":")).encode("utf-8")
            recorded[key] = (status, reason, headers, body)
    return kept


def record(names: list[str], *, fill: bool) -> None:
    """Resolve each workload against PyPI and store what it fetched.

    ``fill`` answers from the existing corpus first, so only what it lacks
    is fetched and added; otherwise the corpus is recorded afresh.
    """
    import tempfile

    for name in names:
        existing = load(name) if fill else {}
        with (
            tempfile.TemporaryDirectory() as root,
            uv_environment(),
            recording_builds() as built,
        ):
            session = RecordingSession(existing, cache=f"{root}/http")
            result = resolve(name, session, f"{root}/cache")
            merged = {**existing, **session.recorded}
            kept = _replace_sdists(merged, built, set(session.recorded))
            save(name, merged)
            print(
                f"{name}: {len(result.candidates)} packages, "
                f"{len(session.recorded)} responses fetched, {len(merged)} kept, "
                f"sdists kept as downloaded: {kept or 'none'}",
            )


if __name__ == "__main__":
    command = sys.argv[1:2]
    if command not in (["capture"], ["fill"]):
        raise SystemExit("usage: python uv_graphs.py capture|fill [workload ...]")
    record(sys.argv[2:] or list(WORKLOADS), fill=command == ["fill"])
