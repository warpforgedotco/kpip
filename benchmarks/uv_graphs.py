"""The resolver graphs uv's own CodSpeed suite measures, replayed offline.

uv's ``crates/uv-bench/benches/uv.rs`` resolves real PyPI graphs from a warm
cache: ``jupyter==1.0.0`` and ``apache-airflow[all]==2.9.3`` with the Apache
Beam provider, pinned to uploads before 2024-09-01 and resolved for CPython
3.11 on an arm64 Mac, so the graph moves with neither PyPI nor the machine
running it. Here kpip resolves the same graphs, with the same inputs, through
``kpip install --dry-run``.

Nothing touches the network while benchmarking. ``corpus/uv_graphs/<name>/``
holds every response a resolve receives: project pages in ``pages.zip``,
everything else (``.metadata`` files, source distributions) in
``files.zip``. :func:`replaying` answers kpip's requests from them below its
HTTP cache, so caching and revalidation run as they do against PyPI, and
fails on any request the corpus lacks: a graph that drifts from its corpus is
an error, not a silent fetch.

Source distributions are stored as a static ``PKG-INFO`` of what their build
produced, with each page's digest and size changed to match. While replaying,
kpip reads that ``PKG-INFO`` in place of running a build backend
(:func:`static_sdist_metadata`), so the benchmark measures kpip, not
setuptools in a subprocess.

When a kpip change makes a resolve read something the corpus lacks, ``python
benchmarks/uv_graphs.py fill [name ...]`` resolves against PyPI, answering
from the corpus where it can, and adds what it fetched. An sdist fetched that
way is built for real once and stored as the static ``PKG-INFO`` of that
build. The archives are written byte for byte reproducibly.
"""

from __future__ import annotations

import contextlib
import email.parser
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from kpip._internal.cli import cmdoptions
from kpip._internal.commands import create_command
from kpip._internal.distributions.sdist import SourceDistribution
from kpip._vendor.packaging import markers
from kpip._vendor.requests.adapters import HTTPAdapter
from kpip._vendor.urllib3.response import HTTPResponse

CORPUS = Path(__file__).with_name("corpus") / "uv_graphs"

INDEX_URL = "https://pypi.org/simple"

# uv-bench's ``ExcludeNewer::global(2024-09-01)``.
UPLOADED_PRIOR_TO = "2024-09-01T00:00:00Z"

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

# uv-bench's ``TAGS``: macOS 21.6 on arm64, for CPython 3.11.
TARGET_OPTIONS = (
    "--python-version",
    "3.11",
    "--implementation",
    "cp",
    "--abi",
    "cp311",
    "--platform",
    "macosx_21_6_arm64",
)

WORKLOADS: dict[str, tuple[str, ...]] = {
    "jupyter": ("jupyter==1.0.0",),
    "airflow": (
        "apache-airflow[all]==2.9.3",
        "apache-airflow-providers-apache-beam>3.0.0",
    ),
}

# Responses stay fresh for the life of a benchmark run, so a warm resolve is
# answered from kpip's HTTP cache, as one within PyPI's ``max-age`` is. A
# revalidation kpip forces (``--refresh-package``) gets ``304 Not Modified``.
_FRESH = "max-age=315360000"

_DROPPED_HEADERS = frozenset(
    ("cache-control", "content-encoding", "content-length", "transfer-encoding")
)

Response = tuple[int, str, dict[str, str], bytes]
Recording = dict[str, Response]
Digests = dict[str, list[object]]

_ARCHIVES = ("pages", "files")
_DIGESTS = "sdist-digests.json"


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


def load_digests(name: str) -> Digests:
    """The digest and size of each static sdist, by URL."""
    path = CORPUS / name / "files.zip"
    if not path.exists():
        return {}
    with zipfile.ZipFile(path) as archive:
        if _DIGESTS not in archive.namelist():
            return {}
        return json.loads(archive.read(_DIGESTS))


def served(recording: Recording, digests: Digests) -> Recording:
    """``recording`` with each page naming a static sdist by its digest.

    Pages are stored as the index sent them, so adding an sdist rewrites a
    small file rather than the archive of every page.
    """
    if not digests:
        return recording
    needles = [url.encode("utf-8") for url in digests]
    result = dict(recording)
    for key, (status, reason, headers, data) in recording.items():
        if _archive_for(key) != "pages" or not any(n in data for n in needles):
            continue
        page = json.loads(data)
        for entry in page.get("files", ()):
            replacement = digests.get(entry.get("url"))
            if replacement is not None:
                entry["hashes"] = {"sha256": replacement[0]}
                entry["size"] = replacement[1]
        body = json.dumps(page, separators=(",", ":")).encode("utf-8")
        result[key] = (status, reason, headers, body)
    return result


def save(name: str, recording: Recording, digests: Digests) -> None:
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
            if archive_name == "files" and digests:
                add(
                    archive,
                    _DIGESTS,
                    json.dumps(dict(sorted(digests.items())), indent=0).encode(),
                )


class _Body(io.BytesIO):
    """A response body that reports exhaustion as ``http.client`` does.

    The cache adapter stores a response once its body is read to the end,
    which it detects by ``fp`` becoming ``None``.
    """

    @property
    def fp(self) -> _Body | None:
        return None if self.tell() >= len(self.getbuffer()) else self


class Replay:
    """Answers requests from a recording; ``live`` fetches and keeps the rest."""

    def __init__(self, recording: Recording, *, live: bool = False) -> None:
        self.responses = {
            key: (status, reason, {**headers, "Cache-Control": _FRESH}, data)
            for key, (status, reason, headers, data) in recording.items()
        }
        self.live = live
        self.recorded: Recording = {}
        self.requests = 0

    def send(
        self,
        adapter: HTTPAdapter,
        live_send: Any,
        request: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        key = f"{request.method} {request.url}"
        recorded = self.responses.get(key)
        if recorded is None:
            if not self.live:
                raise AssertionError(
                    f"no recorded response for {key}; "
                    "run `python benchmarks/uv_graphs.py fill`"
                )
            response = live_send(adapter, request, *args, **kwargs)
            data = response.content
            kept = {
                name: value
                for name, value in response.headers.items()
                if name.lower() not in _DROPPED_HEADERS
            }
            recorded = (response.status_code, response.reason or "", kept, data)
            self.recorded[key] = recorded
            self.responses[key] = (
                recorded[0],
                recorded[1],
                {**kept, "Cache-Control": _FRESH},
                data,
            )
            recorded = self.responses[key]
        self.requests += 1
        status, reason, headers, data = recorded
        etag = headers.get("etag") or headers.get("ETag")
        if etag is not None and request.headers.get("If-None-Match") == etag:
            status, reason, data = 304, "Not Modified", b""
        raw = HTTPResponse(
            body=_Body(data),
            headers={**headers, "Content-Length": str(len(data))},
            status=status,
            reason=reason,
            preload_content=False,
            decode_content=False,
            request_url=request.url,
        )
        return adapter.build_response(request, raw)


@contextlib.contextmanager
def _patched(owner: object, name: str, value: object) -> Iterator[None]:
    """Set ``owner.name`` to ``value`` for the duration."""
    previous = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, previous)


@contextlib.contextmanager
def replaying(replay: Replay) -> Iterator[None]:
    """Route every HTTP request kpip makes through ``replay``.

    The hook sits under kpip's cache adapter, which subclasses
    ``HTTPAdapter`` and calls its ``send`` on a cache miss or revalidation.
    """
    live_send = HTTPAdapter.send

    def send(adapter: HTTPAdapter, request: Any, *args: Any, **kwargs: Any) -> Any:
        return replay.send(adapter, live_send, request, *args, **kwargs)

    with _patched(HTTPAdapter, "send", send):
        yield


@contextlib.contextmanager
def uv_environment() -> Iterator[None]:
    """Resolve as uv-bench does: CPython 3.11.5 markers on an arm64 Mac.

    ``--python-version`` and ``--platform`` move Requires-Python and wheel
    tags; markers have no option, so the function answering them is replaced.
    kpip refuses those options with sdists in play, because it would build
    them for the wrong interpreter; :func:`static_sdist_metadata` builds
    nothing, so that check is lifted too.
    """
    with (
        _patched(markers, "default_environment", lambda: dict(MARKERS)),
        _patched(cmdoptions, "check_dist_restriction", lambda *args, **kwargs: None),
    ):
        yield


def _write_metadata_directory(req: Any, pkg_info: bytes) -> None:
    directory = Path(tempfile.mkdtemp(prefix="kpip-bench-metadata-"))
    dist_info = directory / "static.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_bytes(pkg_info)
    req.metadata_directory = str(dist_info)
    if not req.name:
        req._set_requirement()
    req.assert_source_matches_version()


def _is_static(pkg_info: bytes) -> bool:
    """Whether ``PKG-INFO`` is the metadata a build would produce (PEP 643)."""
    message = email.parser.BytesHeaderParser().parsebytes(pkg_info)
    version = tuple(
        int(part) for part in message.get("Metadata-Version", "1.0").split(".")
    )
    dynamic = {field.lower() for field in message.get_all("Dynamic", [])}
    return version >= (2, 2) and not dynamic & {"requires-dist", "requires-python"}


class Built(dict[str, bytes]):
    """The metadata of each sdist read for the corpus, by URL.

    ``approximate`` names those whose build failed and whose ``PKG-INFO``,
    not reliable by PEP 643, stood in for it.
    """

    approximate: list[str]

    def __init__(self) -> None:
        super().__init__()
        self.approximate = []


@contextlib.contextmanager
def static_sdist_metadata(build: set[str] | None = None) -> Iterator[Built]:
    """Take each sdist's metadata from its ``PKG-INFO`` instead of a build.

    The sdists whose URLs are in ``build`` are read as uv reads them: from a
    ``PKG-INFO`` that PEP 643 makes reliable, otherwise from a real build,
    otherwise (the build failed) from ``PKG-INFO`` anyway. What each yields
    is kept, by URL.
    """
    previous = SourceDistribution.prepare_distribution_metadata
    built = Built()

    def prepare(self: SourceDistribution, *args: Any, **kwargs: Any) -> None:
        assert self.req.link is not None and self.req.source_dir is not None
        url = self.req.link.url_without_fragment
        pkg_info = (Path(self.req.source_dir) / "PKG-INFO").read_bytes()
        if build is None or url not in build:
            _write_metadata_directory(self.req, pkg_info)
            return
        if not _is_static(pkg_info):
            try:
                previous(self, *args, **kwargs)
            except Exception as error:
                print(f"building {url} failed ({error}); using its PKG-INFO")
                built.approximate.append(url.rsplit("/", 1)[-1])
            else:
                assert self.req.metadata_directory is not None
                metadata = Path(self.req.metadata_directory) / "METADATA"
                built[url] = metadata.read_bytes()
                return
        _write_metadata_directory(self.req, pkg_info)
        built[url] = pkg_info

    with _patched(SourceDistribution, "prepare_distribution_metadata", prepare):
        yield built


def resolve(name: str, cache_dir: str, report: str) -> int:
    """One ``kpip install --dry-run`` of a workload; returns how many it picked."""
    status = create_command("install").main(
        [
            "--dry-run",
            "--ignore-installed",
            "--quiet",
            "--quiet",
            "--disable-pip-version-check",
            "--no-input",
            "--index-url",
            INDEX_URL,
            "--uploaded-prior-to",
            UPLOADED_PRIOR_TO,
            "--cache-dir",
            cache_dir,
            "--report",
            report,
            *TARGET_OPTIONS,
            *WORKLOADS[name],
        ]
    )
    if status != 0:
        raise RuntimeError(f"resolving {name} failed with status {status}")
    with open(report, encoding="utf-8") as stream:
        return len(json.load(stream)["install"])


def _static_sdist(filename: str, pkg_info: bytes) -> bytes:
    """An sdist, in ``filename``'s archive format, holding only ``PKG-INFO``."""
    buffer = io.BytesIO()
    if filename.endswith(".zip"):
        root = filename.removesuffix(".zip")
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo(f"{root}/PKG-INFO", date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, pkg_info)
        return buffer.getvalue()
    root = filename.removesuffix(".tar.gz")
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        member = tarfile.TarInfo(f"{root}/PKG-INFO")
        member.size = len(pkg_info)
        member.mtime = 0
        tar.addfile(member, io.BytesIO(pkg_info))
    # gzip writes the current time into its header; zero it.
    data = bytearray(buffer.getvalue())
    data[4:8] = b"\0\0\0\0"
    return bytes(data)


class _NewSdists(set[str]):
    """Every sdist URL the corpus does not hold: every one it holds is static."""

    def __init__(self, stored: set[str]) -> None:
        super().__init__()
        self.stored = stored

    def __contains__(self, url: object) -> bool:
        return url not in self.stored


def fill(names: list[str]) -> None:
    """Resolve each workload against PyPI and add what the corpus lacked.

    Sdists already in the corpus are static; one fetched now is built for
    real, once, and stored as the static ``PKG-INFO`` of that build.
    """
    for name in names:
        recording = load(name)
        digests = load_digests(name)
        replay = Replay(served(recording, digests), live=True)
        with (
            tempfile.TemporaryDirectory() as root,
            uv_environment(),
            replaying(replay),
            static_sdist_metadata(
                build=_NewSdists({key.split(" ", 1)[1] for key in recording})
            ) as built,
        ):
            count = resolve(name, f"{root}/cache", f"{root}/report.json")
        for key, (status, reason, headers, data) in replay.recorded.items():
            url = key.split(" ", 1)[1]
            if url in built:
                data = _static_sdist(url.rsplit("/", 1)[-1], built[url])
                digests[url] = [hashlib.sha256(data).hexdigest(), len(data)]
            recording[key] = (status, reason, headers, data)
        print(
            f"{name}: {count} packages, {len(replay.recorded)} responses added, "
            f"approximate sdist metadata: {built.approximate or 'none'}"
        )
        save(name, recording, digests)


if __name__ == "__main__":
    command = sys.argv[1:2]
    if command != ["fill"]:
        raise SystemExit("usage: python benchmarks/uv_graphs.py fill [workload ...]")
    fill(sys.argv[2:] or list(WORKLOADS))
