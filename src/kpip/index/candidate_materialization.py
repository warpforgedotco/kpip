"""Build, cache, and materialize resolved package candidates."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import urllib.parse
import zipfile
from itertools import chain, islice
from threading import RLock

from kpip.build.build import build_wheel_from_source, unpack_source_internal
from kpip.core.logger import get_logger
from kpip.core.errors import (
    BuildError,
    InstallationError,
    KpipError,
    UnsupportedWheel,
)
from kpip.core.hashes import file_hashes
from kpip.core.http import HttpStatusError, raise_for_status, response_text
from kpip.core.packaging import (
    Requirement,
    canonicalize_name,
    marker_applies,
    parse_requirement,
    target_python_version,
)
from kpip.core.versions import Version, ZERO_VERSION
from kpip.core.wheel import (
    LazyWheelLayout,
    WheelCandidate,
    validate_wheel_with_metadata,
    wheel_candidate,
    wheel_candidate_from_path,
    wheel_dist_info_dir,
)
from kpip.core.wheel_metadata import parse_metadata_headers
from kpip.index.artifacts import ArtifactLocator
from kpip.index.candidate_cache import (
    built_wheel_cache_key,
    cache_built_wheel as store_cached_wheel,
)
from kpip.index.candidate_cache import (
    cached_wheel_for_link,
    emit_build_message,
)
from kpip.index.candidate_metadata_cache import (
    CacheKey,
    CandidateMetadataCache,
    get_candidate_metadata_cache,
)
from kpip.index.candidate_stream import CandidateStream
from kpip.index.metadata_cache import get_wheel_metadata_cache
from kpip.index.prefetch import Prefetcher
from kpip.index.release_facts_cache import get_release_facts_cache
from kpip.index.source_models import (
    SOURCE_ARTIFACT_KINDS,
    ArtifactKind,
    CandidateMetadata,
    CandidateRecord,
    LazyCandidateMetadata,
)
from kpip.index.vcs import (
    git_revision,
    is_immutable_vcs_link,
    release_checkout,
    resolve_git_commit,
)
from kpip.index.vcs import vcs_scheme
from kpip.core.archive import WheelArchive, WheelhouseUnavailable

TYPE_CHECKING = False

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from kpip.index.links import Link
    from collections.abc import (
        Callable,
        Generator,
        Iterable,
        Iterator,
        Mapping,
        Sequence,
    )
    from typing import Any

    from kpip.core.http import HttpSession

logger = get_logger(__name__)


_EXTRA_MARKER_RE = re.compile(r"extra\s*(?:==|in)\s*['\"]([^'\"]+)['\"]")

_METADATA_WORKERS = 32

# The extras slot of the key a VCS candidate's name and version persist under.
_VCS_CANDIDATE_EXTRAS = ("*vcs-candidate*",)
_PREPARED_SDIST_LIMIT = 8

# How many of a release's wheels to ask for a PEP 658 metadata sidecar before
# giving up and reading the source distribution itself. They carry the same
# metadata, so the first that answers settles it; the rest are only for an
# index that advertises a sidecar it will not serve.
_SIBLING_METADATA_ATTEMPTS = 3

# What a release says about itself: name, version, dependencies, the extras
# it offers and the interpreters it supports.
_ReleaseMetadata = tuple[
    str,
    Version,
    tuple[Requirement, ...],
    frozenset[str],
    str | None,
]

# How many source distributions may have their metadata prepared at once.
# Each one is a build environment and a backend subprocess, so this trades
# a bounded amount of memory and CPU for the serial wait; past a handful the
# subprocesses contend for the same cores and stop paying for themselves.
_SOURCE_BUILD_WORKERS = 4

# Below this artifact size (PEP 700 ``size``), metadata-over-ranges is not
# worth it: the 2-3 range round-trips cost more than downloading the wheel
# outright, and the full download lands in the artifact cache where an
# eventual install reuses it.  pip's fast-deps was a net loss on small
# wheels for exactly this reason (pypa/pip#8670).
_RANGED_METADATA_MIN_WHEEL_BYTES = 1 * 1024 * 1024


# The in-memory metadata key: artifact, its identity, version, the extras
# asked for, and the interpreter the markers were evaluated against.
_MetadataKey = tuple[str, str, str, frozenset[str], str]


class _ArchiveMemberInfo:
    """The fields of a zip member that reading a wheel's metadata looks at.

    Slotted rather than a ``NamedTuple``: creating a NamedTuple class reads
    its annotations, which on Python 3.14 imports ``annotationlib`` and
    ``ast`` behind it. ``ZipEntryInfo`` is declared as read-only properties,
    which plain attributes satisfy just as a named tuple's fields did, and
    nothing reads these by position.
    """

    __slots__ = (
        "CRC",
        "compress_size",
        "compress_type",
        "external_attr",
        "file_size",
        "header_offset",
    )

    def __init__(
        self,
        compress_type: int,
        CRC: int,  # noqa: N803 - zipfile's spelling, matched deliberately
        compress_size: int,
        file_size: int,
        header_offset: int,
        external_attr: int,
    ) -> None:
        self.compress_type = compress_type
        self.CRC = CRC
        self.compress_size = compress_size
        self.file_size = file_size
        self.header_offset = header_offset
        self.external_attr = external_attr


class _ResolverWheelArchive:
    """ZipFile-shaped adapter over :class:`WheelArchive`.

    A wrong-package backtracking resolve can open thousands of candidate
    wheels just to read their METADATA -- and ``zipfile.ZipFile.__init__``
    unconditionally builds a full ``ZipInfo`` for every member of each one,
    cost that's unrelated to the handful of headers resolution actually
    reads. ``WheelArchive`` (already relied on by the install-time raw
    archive reader) does the same central-directory scan without that
    per-member object construction, and only for the common case a wheel's
    zip always is -- non-zip64, non-encrypted, deflate/stored -- so this
    adapter is only ever handed to :func:`wheel_dist_info_dir`,
    :func:`wheel_archive_identity`, and :func:`wheel_candidate`, the same
    trio a real ``zipfile.ZipFile`` already serves here.
    """

    __slots__ = ("NameToInfo", "_archive")

    def __init__(self, archive: WheelArchive) -> None:
        self._archive = archive

        modes = archive.modes

        self.NameToInfo = {
            name: _ArchiveMemberInfo(*member, modes.get(name, 0))
            for name, member in archive.members.items()
        }

    def getinfo(self, name: str) -> _ArchiveMemberInfo:
        try:
            return self.NameToInfo[name]

        except KeyError:
            raise KeyError(f"There is no item named {name!r} in the archive") from None

    def read(self, name: str) -> bytes:
        if name not in self.NameToInfo:
            raise KeyError(f"There is no item named {name!r} in the archive")

        try:
            return self._archive.read(name)

        except WheelhouseUnavailable as exc:
            raise zipfile.BadZipFile(f"Bad archive member {name!r}: {exc}") from exc

    def namelist(self) -> list[str]:
        return list(self.NameToInfo)

    def open(self, name: str) -> io.BytesIO:
        return io.BytesIO(self.read(name))

    def close(self) -> None:
        self._archive.file.close()

    def __enter__(self) -> _ResolverWheelArchive:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _open_resolver_wheel_archive(
    path_text: str,
    *,
    metadata_only: bool = False,
) -> _ResolverWheelArchive | zipfile.ZipFile:
    """Open a wheel for metadata-only reads, preferring the faster reader.

    Falls back to a real ``zipfile.ZipFile`` for anything ``WheelArchive``
    doesn't cover (zip64, encryption, an unusual compression method, or any
    other parsing surprise) -- so this only ever costs the speedup, never
    correctness.

    ``metadata_only`` keeps just the ``.dist-info`` members a metadata read
    opens, which is every member a caller that wants no layout back will
    ask for.  A caller that keeps the layout needs the whole directory.
    """

    try:
        file = open(path_text, "rb", buffering=0)  # noqa: SIM115

        archive = WheelArchive(file, metadata_only=metadata_only)

        if any(member[0] not in {0, 8} for member in archive.members.values()):
            file.close()

            return zipfile.ZipFile(path_text)

    except (OSError, ValueError, WheelhouseUnavailable):
        try:
            file.close()

        except UnboundLocalError:
            pass

        return zipfile.ZipFile(path_text)

    return _ResolverWheelArchive(archive)


def project_provided_extras(project: object) -> frozenset[str]:
    optional_dependencies = getattr(project, "optional_dependencies", {})

    extras = set(optional_dependencies)

    extras.update(getattr(project, "provided_extras", ()))

    for dependency in getattr(project, "dependencies", ()):
        marker = getattr(parse_requirement(dependency), "marker", None)

        if marker is not None:
            extras.update(_EXTRA_MARKER_RE.findall(str(marker)))

    return frozenset(extras)


def project_dependencies(
    project: object,
    requested_extras: frozenset[str],
) -> tuple[Requirement, ...]:
    values = list(getattr(project, "dependencies", ()))

    optional_dependencies = getattr(project, "optional_dependencies", {})

    for extra in requested_extras:
        values.extend(optional_dependencies.get(extra, ()))

    dependencies = []
    for value in values:
        requirement = parse_requirement(value)
        if not marker_applies(requirement.marker, extras=requested_extras):
            continue
        if requirement.name.startswith(("file://", "http://", "https://")):
            path = urllib.parse.unquote(urllib.parse.urlsplit(requirement.name).path)
            name = path.rstrip("/").rsplit("/", 1)[-1]
            if name:
                requirement = Requirement(
                    name=name,
                    specifier=requirement.specifier,
                    extras=requirement.extras,
                    url=requirement.url or requirement.name,
                    marker=requirement.marker,
                    raw=requirement.raw,
                )
        dependencies.append(requirement)
    return tuple(dependencies)


def candidate_metadata_fingerprint(candidate: CandidateRecord) -> str:
    """Return a cheap identity for persistent candidate metadata."""

    sha256 = candidate.link.hashes.get("sha256")

    if sha256 is not None:
        return f"sha256:{sha256}"

    local_identity = candidate.link.local_identity_internal

    if local_identity is not None:
        return local_identity

    if candidate.link.is_vcs and vcs_scheme(candidate.link.url) == "git":
        commit = resolve_git_commit(candidate.link.url)

        if commit is not None:
            return f"git:{commit}"

    if candidate.link.is_file:
        try:
            stat = os.stat(candidate.link.file_path)

        except OSError:
            pass

        else:
            local_identity = (
                f"stat:{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
            )
            candidate.link.local_identity_internal = local_identity
            return local_identity

    return candidate.link.url


class LazyWheelCandidate(WheelCandidate):
    """Resolver candidate whose metadata is cheap and whose wheel is deferred."""

    __slots__ = (
        "_record_internal",
        "_version_internal",
        "materialized_internal",
        "materializer_internal",
        "record_loader_internal",
        "requirement_internal",
    )

    def __init__(
        self,
        record: CandidateRecord | None,
        requirement: Requirement,
        materializer: CandidateMaterializer,
        record_loader: Callable[[], CandidateRecord] | None = None,
        version: Version | None = None,
    ) -> None:
        self._record_internal = record

        self._version_internal = (
            version
            if version is not None
            else (record.version if record is not None else None)
        )

        self.requirement_internal = requirement

        self.materializer_internal = materializer

        self.record_loader_internal = record_loader

        self.materialized_internal: WheelCandidate | None = None

    @property
    def record_internal(self) -> CandidateRecord:
        record = self._record_internal

        if record is None:
            loader = self.record_loader_internal

            if loader is None:
                raise RuntimeError("lazy candidate has no record loader")

            record = loader()

            self._record_internal = record

        return record

    def build_candidate(self) -> WheelCandidate:
        candidate = self.materialized_internal

        if candidate is None:
            candidates = list(
                self.materializer_internal.iter_materialize(
                    self.requirement_internal,
                    (self.record_internal,),
                ),
            )

            if not candidates:
                raise BuildError(
                    f"Unable to materialize candidate {self.record_internal.name}",
                )

            candidate = candidates[0]

            self.materialized_internal = candidate

        return candidate

    def materialize(self) -> WheelCandidate:
        """Return the concrete wheel candidate at an explicit build boundary."""

        return self.build_candidate()

    @property
    def name(self) -> str:
        return self.record_internal.name

    @property
    def version(self) -> Version:
        version = self._version_internal

        if version is None:
            version = self.record_internal.version

            self._version_internal = version

        return version

    @property
    def path(self) -> str:
        if (
            self.materializer_internal.dry_run
            and self.record_internal.link.kind in SOURCE_ARTIFACT_KINDS
        ):
            if not self.record_internal.link.is_file:
                return str(self.record_internal.link.filename)

            local_path = self.materializer_internal.local_path_for(
                self.record_internal,
            )

            assert local_path is not None

            return self.materializer_internal.ensure_local_text(
                self.record_internal,
                local_path=local_path,
            )

        return self.materialize().path

    @property
    def dependencies(self) -> tuple[Requirement, ...]:
        return self.record_internal.metadata().dependencies

    @property
    def metadata_version(self) -> Version:
        """The release the candidate's own metadata declares.

        May differ from ``version`` (the catalog/filename-declared release)
        for a mislabeled or malformed artifact.
        """
        return self.record_internal.metadata().version

    @property
    def provided_extras(self) -> frozenset[str]:
        return self.record_internal.metadata().provided_extras

    @property
    def requires_python(self) -> str | None:
        requires_python = self.record_internal.link.requires_python

        if requires_python is not None:
            return requires_python

        return self.record_internal.metadata().requires_python

    @property
    def source_url(self) -> str:
        return self.record_internal.link.url

    @property
    def source_filename(self) -> str:
        """Artifact filename from the index record, without materializing it."""
        return str(self.record_internal.link.filename)

    @property
    def source_hashes(self) -> dict[str, str] | None:
        return self.materializer_internal.source_hashes_for(self.record_internal)

    @property
    def source_kind(self) -> str:
        return self.record_internal.link.kind.value

    @property
    def source_is_direct(self) -> bool:
        """Whether the artifact came from an explicit direct URL requirement."""
        return self.requirement_internal.url is not None

    @property
    def source_vcs(self) -> str | None:
        if not self.record_internal.link.is_vcs:
            return None

        return vcs_scheme(self.record_internal.link.url)

    @property
    def source_vcs_revision(self) -> str | None:
        if not self.record_internal.link.is_vcs:
            return None

        return self.materializer_internal.vcs_revision(self.record_internal.link.url)

    @property
    def from_cache(self) -> bool:
        candidate = self.materialized_internal

        return candidate.from_cache if candidate is not None else False

    @property
    def yanked_reason(self) -> str | None:
        return self.record_internal.link.yanked_reason

    @property
    def wheel_layout(self) -> object | None:
        return self.materialize().wheel_layout


class CandidateMaterializer:
    def __init__(
        self,
        *,
        build_options: dict[str, dict[str, object]] | None = None,
        build_constraints: list[str] | None = None,
        wheel_cache_dir: str | os.PathLike[str] | None = None,
        target_key: str | None = None,
        build_isolation: bool = True,
        dry_run: bool = False,
        compute_source_hashes: bool = False,
        session: HttpSession | None = None,
        release_metadata_links: Callable[
            [Requirement, Version],
            Sequence[Link],
        ]
        | None = None,
    ) -> None:
        self.release_metadata_links = release_metadata_links

        self.sibling_metadata_cache: dict[
            tuple[str, str],
            _ReleaseMetadata | None,
        ] = {}

        # Whether this resolve has had to read a source distribution the
        # hard way. Nothing speculates until it has: a graph served
        # entirely by wheels never blocks on a build, so looking for
        # builds to start is pure overhead on it.
        self.prepares_source_metadata = False

        self.source_build_lock = RLock()

        self.source_build_pool: ThreadPoolExecutor | None = None

        self.source_builds: dict[_MetadataKey, Any] = {}

        self.build_options = build_options

        self.build_constraints = build_constraints

        self.wheel_cache_dir = wheel_cache_dir

        self.target_key = target_key

        self.build_isolation = build_isolation

        self.dry_run = dry_run

        self.compute_source_hashes = compute_source_hashes

        self.session = session

        self.persistent_metadata_cache = (
            get_wheel_metadata_cache(wheel_cache_dir)
            if wheel_cache_dir is not None
            else None
        )

        self.persistent_candidate_metadata_cache = (
            get_candidate_metadata_cache(wheel_cache_dir)
            if wheel_cache_dir is not None
            else None
        )

        self.persistent_release_facts_cache = (
            get_release_facts_cache(wheel_cache_dir)
            if wheel_cache_dir is not None
            else None
        )

        self.artifacts = None

        self.invalid_links: set[str] = set()

        self.wheel_candidates: dict[
            tuple[str, str, frozenset[str]],
            WheelCandidate,
        ] = {}

        self.metadata_cache: dict[
            _MetadataKey,
            CandidateMetadata,
        ] = {}

        self.release_metadata_cache: dict[
            tuple[str, str],
            tuple[
                str,
                Version,
                tuple[Requirement, ...],
                frozenset[str],
                str | None,
            ]
            | None,
        ] = {}

        self.artifact_fingerprint_cache: dict[str, str] = {}

        self.source_hash_cache: dict[str, dict[str, str] | None] = {}

        self.prepared_sdist_sources: dict[
            str,
            tuple[tempfile.TemporaryDirectory[str], str],
        ] = {}

        self.local_artifacts: dict[str, str] = {}

        self.vcs_revisions: dict[str, str] = {}

        self.metadata_prefetcher: Prefetcher[Any, str] | None = None

        self.metadata_prefetch_lock = RLock()

    def local_path_for(self, candidate: CandidateRecord) -> str | None:
        if not candidate.link.is_file:
            return None

        url = candidate.link.url

        cached = self.local_artifacts.get(url)

        if cached is None:
            cached = candidate.link.file_path

            self.local_artifacts[url] = cached

        return cached

    def ensure_local_text(
        self,
        candidate: CandidateRecord,
        *,
        local_path: str | None = None,
    ) -> str:
        if not candidate.link.is_vcs:
            cached = self.local_artifacts.get(candidate.link.url)

            if cached is not None:
                return cached

        if candidate.link.is_file:
            path = (
                os.fspath(local_path)
                if local_path is not None
                else self.local_path_for(candidate)
            )

            assert path is not None

            self.local_artifacts[candidate.link.url] = path

            return path

        if self.artifacts is None:
            self.artifacts = ArtifactLocator(
                self.session,
                cache_dir=self.wheel_cache_dir,
            )

        path = self.artifacts.ensure_local_text(
            candidate.link.url,
            is_vcs=candidate.link.is_vcs,
            local_path=local_path,
            hashes=(candidate.link.hashes if not candidate.link.is_vcs else None),
        )

        if candidate.link.is_vcs:
            self.vcs_revisions[candidate.link.url] = git_revision(path)

        path_text = path

        if not candidate.link.is_vcs:
            self.local_artifacts[candidate.link.url] = path_text

        return path_text

    def vcs_build_commit(self, candidate: CandidateRecord) -> str | None:
        """The commit a git candidate's build is identified by, if resolvable.

        Resolving it (one ls-remote, memoized) also records it as the URL's
        revision, so the lock's own hashing and the wheel cache key agree
        without a checkout.  A checkout that follows overrides the revision
        with what it actually checked out.
        """

        link = candidate.link

        if not link.is_vcs or vcs_scheme(link.url) != "git":
            return None

        commit = resolve_git_commit(link.url)

        if commit is not None:
            self.vcs_revisions.setdefault(link.url, commit)

        return commit

    def vcs_revision(self, url: str) -> str | None:
        """Return the revision observed while materializing a VCS candidate."""

        return self.vcs_revisions.get(url)

    def artifact_fingerprint(self, candidate: CandidateRecord) -> str:
        key = candidate.link.url

        fingerprint = self.artifact_fingerprint_cache.get(key)

        if fingerprint is None:
            fingerprint = candidate_metadata_fingerprint(candidate)

            self.artifact_fingerprint_cache[key] = fingerprint

            if fingerprint.startswith("git:"):
                # The lock records this commit; a clone, if one happens,
                # overrides it with what it actually checked out.
                self.vcs_revisions.setdefault(key, fingerprint[4:])

        return fingerprint

    def content_persistent_key(
        self,
        candidate: CandidateRecord,
        requested_extras: frozenset[str],
    ) -> CacheKey | None:
        """A persistent metadata key from the artifact's own content.

        Only for an artifact fetched by URL whose link publishes no hash and
        which is not a VCS checkout or a local file; ``None`` otherwise, or
        when it cannot be fetched.  Fetching it here is what the metadata
        read is about to do anyway.
        """

        link = candidate.link

        if (
            self.persistent_candidate_metadata_cache is None
            or link.is_vcs
            or link.is_file
            or link.hashes
            or link.kind not in (ArtifactKind.SDIST, ArtifactKind.WHEEL)
            or not link.url.startswith(("http://", "https://"))
        ):
            return None

        # Not source_hashes_for: it declines to fetch in a dry run, while the
        # metadata read this serves fetches regardless.  The digest is kept
        # under the URL so the lock's own hashing finds it too.
        cached = self.source_hash_cache.get(link.url)

        if cached is None:
            try:
                local = self.ensure_local_text(
                    candidate,
                    local_path=self.local_path_for(candidate),
                )

                cached = file_hashes(local)

            except (KpipError, OSError, ValueError):
                return None

            self.source_hash_cache[link.url] = cached

        digest = cached.get("sha256")

        if digest is None:
            return None

        return (
            link.url,
            candidate.version.public,
            tuple(sorted(requested_extras)),
            f"sha256:{digest}",
            target_python_version() or "",
        )

    def persisted_vcs_candidate(self, link: Link) -> tuple[str, Version] | None:
        """The name and version persisted for a VCS link's current commit.

        Learning them otherwise means a clone and a metadata build.  The
        commit comes from one ``git ls-remote``; a persisted entry from an
        earlier run under that commit answers, and its absence means the
        clone happens as before.
        """

        if not link.is_vcs or self.persistent_candidate_metadata_cache is None:
            return None

        probe = CandidateRecord("", Version("0"), link)

        fingerprint = self.artifact_fingerprint(probe)

        if not fingerprint.startswith("git:"):
            return None

        metadata = self.persistent_candidate_metadata_cache.get(
            (
                link.url,
                "",
                _VCS_CANDIDATE_EXTRAS,
                fingerprint,
                target_python_version() or "",
            ),
        )

        if metadata is None:
            return None

        return metadata.name, metadata.version

    def persistent_metadata_cache_for(
        self,
        candidate: CandidateRecord,
    ) -> CandidateMetadataCache | None:
        """The persistent metadata cache, for artifacts whose identity can pin it.

        Cached metadata never expires, which is sound only while the cache key
        proves the artifact is the same one: a published hash, or a local
        file's stat identity.  When the fingerprint falls back to the bare
        URL -- an index that publishes no hashes -- a republished artifact
        under the same name would be served the old metadata forever, so such
        candidates keep to the per-process memory cache and are re-read on
        the next run.
        """

        cache = self.persistent_candidate_metadata_cache

        if cache is None:
            return None

        if self.artifact_fingerprint(candidate) == candidate.link.url:
            return None

        return cache

    def source_hashes_for(self, candidate: CandidateRecord) -> dict[str, str] | None:
        hashes = candidate.link.hashes

        if hashes:
            return dict(hashes)

        if candidate.link.kind not in SOURCE_ARTIFACT_KINDS:
            return None

        if candidate.link.is_vcs:
            url = candidate.link.url

            if self.vcs_revision(url) is None:
                local = self.ensure_local_text(candidate)
                release_checkout(local)

            return None

        if self.dry_run and not candidate.link.is_file:
            return None

        fingerprint = self.artifact_fingerprint(candidate)

        if fingerprint in self.source_hash_cache:
            cached = self.source_hash_cache[fingerprint]

            return None if cached is None else dict(cached)

        local = self.ensure_local_text(
            candidate,
            local_path=self.local_path_for(candidate),
        )

        try:
            result = file_hashes(local)

        except OSError:
            self.source_hash_cache[fingerprint] = None

            return None

        self.source_hash_cache[fingerprint] = result

        return dict(result)

    def remember_prepared_sdist(
        self,
        candidate: CandidateRecord,
        temporary: tempfile.TemporaryDirectory[str],
        source: str,
    ) -> None:
        fingerprint = self.artifact_fingerprint(candidate)

        previous = self.prepared_sdist_sources.pop(fingerprint, None)

        if previous is not None:
            previous[0].cleanup()

        self.prepared_sdist_sources[fingerprint] = (temporary, source)

        while len(self.prepared_sdist_sources) > _PREPARED_SDIST_LIMIT:
            oldest = next(iter(self.prepared_sdist_sources))
            expired, _ = self.prepared_sdist_sources.pop(oldest)
            expired.cleanup()

    def take_prepared_sdist(
        self,
        candidate: CandidateRecord,
    ) -> tuple[tempfile.TemporaryDirectory[str], str] | None:
        return self.prepared_sdist_sources.pop(
            self.artifact_fingerprint(candidate),
            None,
        )

    def prepare_record(
        self,
        requirement: Requirement,
        candidate: CandidateRecord,
    ) -> CandidateRecord:
        """Attach metadata only when a candidate reaches a consumption boundary."""

        if candidate.metadata_loader is not None:
            return candidate

        return candidate.with_metadata_loader(
            self.metadata_loader(candidate, requirement),
        )

    def materialize(
        self,
        requirement: Requirement,
        accepted: Iterable[CandidateRecord],
    ) -> CandidateStream:
        requested_extras = frozenset(requirement.extras)

        accepted_iterator = iter(accepted)

        first = next(accepted_iterator, None)

        if first is None:
            return CandidateStream(iter(()))

        prefetch_count = 0 if self.has_cached_metadata(first, requested_extras) else 2

        initial_records = [first]

        if prefetch_count > 1:
            initial_records.extend(islice(accepted_iterator, prefetch_count - 1))

        prefetched_records = tuple(
            self.prepare_record(requirement, candidate)
            for candidate in initial_records[:prefetch_count]
        )

        self.prefetch_metadata(prefetched_records, requirement=requirement)

        accepted_records = chain(initial_records, accepted_iterator)

        def generate() -> Iterator[WheelCandidate]:
            invalid_versions: set[tuple[str, Version]] = set()

            for index, candidate in enumerate(accepted_records):
                candidate = (
                    prefetched_records[index]
                    if index < len(prefetched_records)
                    else self.prepare_record(requirement, candidate)
                )

                identity = (candidate.canonical_name, candidate.version)

                if identity in invalid_versions:
                    continue

                if self.release_is_invalid(candidate):
                    invalid_versions.add(identity)

                    continue

                yield LazyWheelCandidate(candidate, requirement, self)

        return CandidateStream(generate())

    def materialize_one(
        self,
        requirement: Requirement,
        record: CandidateRecord,
    ) -> WheelCandidate | None:
        """One record as the lazy candidate :meth:`materialize` would yield.

        For a caller that already holds the single record it wants -- the
        resolver's forward check reading one release -- without the stream,
        the prefetch decision and the generator a whole selection needs.
        ``None`` when the release is known to be invalid.
        """

        candidate = self.prepare_record(requirement, record)

        if self.release_is_invalid(candidate):
            return None

        return LazyWheelCandidate(candidate, requirement, self)

    def release_is_invalid(self, candidate: CandidateRecord) -> bool:
        """Whether the release was recorded as unusable by an earlier run."""

        cache = self.persistent_release_facts_cache

        if cache is None or cache.get(self.negative_fact_key(candidate)) is None:
            return False

        self.invalid_links.add(candidate.link.url)

        return True

    def negative_fact_key(self, candidate: CandidateRecord) -> tuple[str, str, str]:
        return (
            candidate.canonical_name,
            candidate.version.public,
            self.artifact_fingerprint(candidate),
        )

    def metadata_cache_keys(
        self,
        candidate: CandidateRecord,
        requested_extras: frozenset[str],
    ) -> tuple[_MetadataKey, CacheKey]:
        fingerprint = self.artifact_fingerprint(candidate)

        # A VCS commit determines the version, and leaving the version out
        # lets the key be built before the version is known -- which is how
        # a persisted entry spares the clone that would learn it.
        version = "" if candidate.link.is_vcs else candidate.version.public

        # What is cached is the metadata as this resolve reads it, markers
        # already applied, so the interpreter those markers were evaluated
        # against is part of what the entry is. Without it a lock for 3.8
        # persists dependencies that the next lock for this interpreter
        # would read back as its own.
        target = target_python_version() or ""

        return (
            (
                candidate.link.url,
                fingerprint,
                version,
                requested_extras,
                target,
            ),
            (
                candidate.link.url,
                version,
                tuple(sorted(requested_extras)),
                fingerprint,
                target,
            ),
        )

    def has_cached_metadata(
        self,
        candidate: CandidateRecord,
        requested_extras: frozenset[str],
    ) -> bool:
        memory_key, persistent_key = self.metadata_cache_keys(
            candidate,
            requested_extras,
        )

        if memory_key in self.metadata_cache:
            return True

        persistent_cache = self.persistent_metadata_cache_for(candidate)

        return persistent_cache is not None and persistent_cache.contains(
            persistent_key,
        )

    def prefetch_metadata(
        self,
        records: tuple[CandidateRecord, ...],
        *,
        requirement: Requirement | None = None,
    ) -> None:
        if self.session is None:
            return

        requested_extras = frozenset(requirement.extras if requirement else ())

        pending: list[tuple[str, str]] = []

        for candidate in records:
            if candidate.link.kind is not ArtifactKind.WHEEL:
                continue

            metadata_link = candidate.link.metadata_link()

            if metadata_link is None:
                continue

            if self.has_cached_metadata(candidate, requested_extras):
                continue

            pending.append((metadata_link.url, metadata_link.url))

        if not pending:
            return

        with self.metadata_prefetch_lock:
            if self.metadata_prefetcher is None:
                self.metadata_prefetcher = Prefetcher(
                    self.session.get,
                    max_workers=_METADATA_WORKERS,
                )

            for key, url in pending:
                self.metadata_prefetcher.submit(key, url)

    def prefetched_metadata_future(self, url: str) -> Any:
        """The in-flight fetch for ``url``, left in place for its consumer."""
        with self.metadata_prefetch_lock:
            prefetcher = self.metadata_prefetcher

        return None if prefetcher is None else prefetcher.peek(url)

    def take_prefetched_metadata(self, url: str) -> Any:
        with self.metadata_prefetch_lock:
            prefetcher = self.metadata_prefetcher

            future = None if prefetcher is None else prefetcher.take(url)

        return future.result() if future is not None else None

    def close(self) -> None:
        with self.metadata_prefetch_lock:
            prefetcher = self.metadata_prefetcher

            self.metadata_prefetcher = None

        if prefetcher is not None:
            prefetcher.close()

        self.close_source_builds()

        prepared_sources = tuple(self.prepared_sdist_sources.values())

        self.prepared_sdist_sources.clear()

        for temporary, _ in prepared_sources:
            temporary.cleanup()

    def started_metadata(self, key: _MetadataKey) -> Any:
        """The computation already under way for ``key``, if there is one."""
        with self.source_build_lock:
            return self.source_builds.get(key)

    def start_source_metadata(
        self,
        candidate: CandidateRecord,
        requirement: Requirement,
    ) -> None:
        """Begin a source candidate's metadata before the resolver blocks on it.

        Reading a source distribution's metadata means standing up a build
        environment and running the backend -- seconds each, and the resolver
        asks for them one at a time, so a graph with eight of them spends
        most of a cold lock waiting with an idle machine. Each is
        independent, so the ones the resolver has just learned it needs are
        started now and are usually finished, or at least under way, by the
        time it asks.

        Only source candidates are started: a wheel's metadata is a read, and
        :meth:`prefetch_metadata` already overlaps those.
        """
        if candidate.link.kind not in SOURCE_ARTIFACT_KINDS:
            return

        requested_extras = frozenset(requirement.extras)

        if self.has_cached_metadata(candidate, requested_extras):
            return

        key, _ = self.metadata_cache_keys(candidate, requested_extras)

        with self.source_build_lock:
            if key in self.source_builds:
                return

            pool = self.source_build_pool

            if pool is None:
                # Imported here rather than at module scope, as everywhere
                # else that builds a pool: only a resolve that has to read a
                # source distribution ever reaches this.
                from concurrent.futures import ThreadPoolExecutor

                pool = ThreadPoolExecutor(
                    max_workers=_SOURCE_BUILD_WORKERS,
                    thread_name_prefix="kpip-metadata",
                )

                self.source_build_pool = pool

            worker = self.metadata_loader(candidate, requirement, background=True)

            self.source_builds[key] = pool.submit(worker.load)

    def close_source_builds(self) -> None:
        """Drop the metadata pool, abandoning work nothing is waiting for.

        Speculative builds still queued when the solve ends are for versions
        it did not take, so they are cancelled rather than started: a command
        must not sit at exit preparing metadata for a release it already
        decided against.

        The few already running are waited for. They write what they read
        into the metadata caches, under a key naming the interpreter the
        resolve was for, and that target is restored the moment the command
        returns -- a worker still running past it would file its answer
        under whichever interpreter happened to be current by then.
        """
        with self.source_build_lock:
            pool = self.source_build_pool

            self.source_build_pool = None

            self.source_builds = {}

        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

    def metadata_loader(
        self,
        candidate: CandidateRecord,
        requirement: Requirement,
        *,
        background: bool = False,
    ) -> LazyCandidateMetadata:
        """A candidate's metadata, computed at most once, when first asked.

        ``background`` returns the computation itself, for a caller that is
        starting it ahead of demand; the loader handed to a consumer waits
        for such a computation rather than racing it.
        """
        requested_extras = frozenset(requirement.extras)

        key, persistent_key = self.metadata_cache_keys(
            candidate,
            requested_extras,
        )

        persistent_cache = self.persistent_metadata_cache_for(candidate)

        def load() -> CandidateMetadata:
            nonlocal persistent_cache, persistent_key

            cached = self.metadata_cache.get(key)

            if cached is not None:
                return cached

            if persistent_cache is None:
                # An artifact fetched by URL with no published hash has no
                # identity a cache could trust -- until it is fetched, when
                # its content has one.  The fetch is a cache hit on a warm
                # run, so the hash costs a read of the file and saves the
                # build: a URL sdist was rebuilt on every lock before this.
                content_key = self.content_persistent_key(candidate, requested_extras)

                if content_key is not None:
                    persistent_cache = self.persistent_candidate_metadata_cache
                    persistent_key = content_key

            if persistent_cache is not None:
                cached = persistent_cache.get(persistent_key)

                if cached is not None:
                    self.metadata_cache[key] = cached

                    return cached

            if candidate.link.kind in SOURCE_ARTIFACT_KINDS:
                metadata = self.pypi_metadata(candidate, requested_extras)

                if metadata is None or not (
                    requested_extras <= metadata.provided_extras
                ):
                    metadata = self.sibling_wheel_metadata(
                        candidate,
                        requirement,
                        requested_extras,
                    )

                if (
                    metadata is not None
                    and requested_extras <= metadata.provided_extras
                ):
                    self.metadata_cache[key] = metadata

                    if persistent_cache is not None:
                        persistent_cache.put(
                            persistent_key,
                            metadata,
                        )

                    return metadata

            if candidate.link.kind is ArtifactKind.WHEEL:
                metadata_link = candidate.link.metadata_link()

                metadata = self.remote_wheel_metadata(
                    candidate,
                    requested_extras,
                    response=(
                        self.take_prefetched_metadata(metadata_link.url)
                        if metadata_link is not None
                        else None
                    ),
                )

                if metadata is not None:
                    self.metadata_cache[key] = metadata

                    if persistent_cache is not None:
                        persistent_cache.put(
                            persistent_key,
                            metadata,
                        )

                    return metadata

            if candidate.link.kind is ArtifactKind.WHEEL:
                metadata = self.ranged_wheel_metadata(candidate, requested_extras)

                if metadata is not None:
                    self.metadata_cache[key] = metadata

                    if persistent_cache is not None:
                        persistent_cache.put(
                            persistent_key,
                            metadata,
                        )

                    return metadata

            local_path = self.local_path_for(candidate)

            path_text = self.ensure_local_text(candidate, local_path=local_path)

            vcs_path = path_text if candidate.link.is_vcs else None

            if (
                vcs_path is not None
                and persistent_cache is not None
                and persistent_key[3]
                != f"git:{self.vcs_revisions.get(candidate.link.url)}"
            ):
                # The remote moved between resolving the reference and the
                # clone: what was built is not what the key names.
                persistent_cache = None

            if candidate.link.kind in SOURCE_ARTIFACT_KINDS:
                from kpip.build.build_backend import prepare_project_metadata

                # Neither the index nor a sibling wheel could answer, so this
                # release is about to be built. From here the resolve is one
                # that pays for builds, and starting the next ones early is
                # worth what looking for them costs.
                self.prepares_source_metadata = True

                cache_source_hashes = (
                    self.source_hashes_for(candidate)
                    if self.wheel_cache_dir is not None
                    else None
                )
                metadata_vcs_commit = self.vcs_build_commit(candidate)

                metadata_wheel_cache_key = built_wheel_cache_key(
                    candidate,
                    source_hashes=cache_source_hashes,
                    config_settings=None,
                    build_constraints=self.build_constraints,
                    build_isolation=self.build_isolation,
                    target_key=self.target_key,
                    vcs_commit=metadata_vcs_commit,
                )

                path = path_text

                try:
                    if (
                        candidate.link.kind is ArtifactKind.SOURCE_TREE
                        and candidate.link.subdirectory_fragment
                    ):
                        path = os.path.join(path, candidate.link.subdirectory_fragment)

                    prepared_temporary: tempfile.TemporaryDirectory[str] | None = None

                    try:
                        if candidate.link.kind is ArtifactKind.SDIST:
                            prepared_temporary = tempfile.TemporaryDirectory(
                                prefix="kpip-metadata-",
                            )
                            path = unpack_source_internal(
                                path,
                                prepared_temporary.name,
                            )

                        def remember_wheel_if_reusable(wheel_path: str) -> None:
                            if candidate.link.kind is ArtifactKind.SDIST or (
                                candidate.link.kind is ArtifactKind.SOURCE_TREE
                                and (
                                    is_immutable_vcs_link(candidate.link.url)
                                    or (
                                        metadata_vcs_commit is not None
                                        and self.vcs_revisions.get(candidate.link.url)
                                        == metadata_vcs_commit
                                    )
                                )
                            ):
                                store_cached_wheel(
                                    self.wheel_cache_dir,
                                    candidate,
                                    wheel_path,
                                    metadata_wheel_cache_key,
                                    source_hashes=cache_source_hashes,
                                    candidate_name_is_authoritative=(
                                        not requirement.is_unnamed_direct
                                    ),
                                )

                        try:
                            project = prepare_project_metadata(
                                path,
                                build_constraints=self.build_constraints,
                                build_isolation=self.build_isolation,
                                on_wheel_built=remember_wheel_if_reusable,
                            )

                        except BuildError as exc:
                            metadata = self.pypi_metadata(candidate, requested_extras)

                            if metadata is None:
                                raise BuildError(
                                    f"Failed to build '{candidate.name}': {exc}",
                                ) from exc

                        else:
                            metadata = CandidateMetadata(
                                name=project.name,
                                version=Version(project.version),
                                dependencies=project_dependencies(
                                    project,
                                    requested_extras,
                                ),
                                provided_extras=project_provided_extras(project),
                                requires_python=project.requires_python,
                            )

                            if prepared_temporary is not None:
                                self.remember_prepared_sdist(
                                    candidate,
                                    prepared_temporary,
                                    path,
                                )
                                prepared_temporary = None
                    finally:
                        if prepared_temporary is not None:
                            prepared_temporary.cleanup()
                finally:
                    if vcs_path is not None:
                        release_checkout(vcs_path)

            else:
                with _open_resolver_wheel_archive(
                    path_text,
                    metadata_only=True,
                ) as archive:
                    try:
                        dist_info_dir = wheel_dist_info_dir(
                            archive,
                            os.path.basename(path_text)[:-4].split("-", 1)[0],
                        )

                    except UnsupportedWheel as exc:
                        raise InstallationError(str(exc)) from exc

                    built = wheel_candidate(
                        path_text,
                        requested_extras,
                        archive=archive,
                        filename_info=(candidate.name, candidate.version),
                        dist_info_dir=dist_info_dir,
                        include_layout=False,
                        metadata_cache=self.persistent_metadata_cache,
                    )

                metadata = CandidateMetadata(
                    name=built.name,
                    version=built.version,
                    dependencies=built.dependencies,
                    provided_extras=built.provided_extras,
                    requires_python=built.requires_python,
                )

            self.metadata_cache[key] = metadata

            if persistent_cache is not None:
                persistent_cache.put(persistent_key, metadata)

                if candidate.link.is_vcs:
                    # Under an extras-independent key too, so the next run
                    # learns the name and version without a clone.
                    persistent_cache.put(
                        (
                            candidate.link.url,
                            "",
                            _VCS_CANDIDATE_EXTRAS,
                            persistent_key[3],
                            persistent_key[4],
                        ),
                        metadata,
                    )

            return metadata

        if background:
            return LazyCandidateMetadata(load)

        def join() -> CandidateMetadata:
            cached = self.metadata_cache.get(key)

            if cached is not None:
                return cached

            started = self.started_metadata(key)

            return load() if started is None else started.result()

        return LazyCandidateMetadata(join)

    def remote_wheel_metadata(
        self,
        candidate: CandidateRecord,
        requested_extras: frozenset[str],
        response: Any = None,
    ) -> CandidateMetadata | None:
        if self.session is None:
            return None

        metadata_link = candidate.link.metadata_link()

        if metadata_link is None:
            return None

        try:
            if response is None:
                response = self.session.get(metadata_link.url)

            raise_for_status(response)

            return self.metadata_from_headers(
                parse_metadata_headers(response_text(response)),
                requested_extras,
            )

        except (KeyError, OSError, TypeError, ValueError):
            return None

    def ranged_wheel_metadata(
        self,
        candidate: CandidateRecord,
        requested_extras: frozenset[str],
    ) -> CandidateMetadata | None:
        """A wheel's metadata read over HTTP range requests, if that works.

        The tier between a PEP 658 sidecar and downloading the whole wheel.
        An index that publishes no sidecars would otherwise cost a full wheel
        per candidate considered, most of which the resolver discards.

        The session is asked duck-typed rather than through the ``HttpSession``
        protocol: ``index`` may not import ``network``, and this is an extra a
        transport either offers or does not. Whether a host serves ranges is
        the transport's to remember, so nothing is memoized here.
        """
        session = self.session

        if session is None or candidate.link.is_file:
            return None

        size = candidate.link.size

        if size is not None and size < _RANGED_METADATA_MIN_WHEEL_BYTES:
            return None

        reader = getattr(session, "wheel_metadata_text", None)

        if reader is None:
            return None

        try:
            text = reader(candidate.link.url_without_fragment, candidate.name)

            if not text:
                return None

            # Parsing is inside the handler too: a wheel whose Requires-Dist
            # will not parse should cost a full download, not the whole
            # metadata load.
            return self.metadata_from_headers(
                parse_metadata_headers(text),
                requested_extras,
            )

        except (KeyError, OSError, TypeError, ValueError):
            return None

    def metadata_from_headers(
        self,
        headers: Mapping[str, Sequence[str]],
        requested_extras: frozenset[str],
    ) -> CandidateMetadata | None:
        """Parsed ``METADATA`` headers as a candidate's metadata."""
        name = headers.get("name", (None,))[0]

        version = headers.get("version", (None,))[0]

        if name is None or version is None:
            return None

        return CandidateMetadata(
            name=name,
            version=Version(version),
            dependencies=tuple(
                requirement
                for value in headers.get("requires-dist", ())
                if (requirement := parse_requirement(value)) is not None
                if marker_applies(requirement.marker, extras=requested_extras)
            ),
            provided_extras=frozenset(headers.get("provides-extra", ())),
            requires_python=(headers.get("requires-python") or [None])[0],
        )

    def sibling_wheel_metadata(
        self,
        candidate: CandidateRecord,
        requirement: Requirement,
        requested_extras: frozenset[str],
    ) -> CandidateMetadata | None:
        """A source distribution's dependencies, read from a sibling wheel.

        A source distribution states its dependencies only through its build
        backend, and running that backend needs a build environment the
        target may not be able to have: a lock for 3.8 is prepared by
        whichever interpreter kpip runs on, and a C extension pinned for 3.8
        will not compile there. A wheel of the same release carries the very
        metadata that backend would produce, and PEP 658 serves it beside the
        wheel, so the release answers for its own source distribution without
        anything being built or even downloaded.

        Any wheel of the release will do. Tags decide where a wheel can be
        installed, not what it depends on, and a difference between platforms
        belongs in an environment marker, which is carried through here
        unevaluated. Only wheels whose index page advertises the sidecar are
        asked, so an index that publishes none costs nothing.
        """
        session = self.session

        links = self.release_metadata_links

        if session is None or links is None:
            return None

        release_key = (candidate.canonical_name, candidate.version.public)

        if release_key in self.sibling_metadata_cache:
            release = self.sibling_metadata_cache[release_key]

        else:
            release = self.read_sibling_wheel_metadata(
                links(requirement, candidate.version),
                candidate,
            )

            self.sibling_metadata_cache[release_key] = release

        if release is None:
            return None

        name, version, dependencies, extras, requires_python = release

        return CandidateMetadata(
            name=name,
            version=version,
            dependencies=tuple(
                item
                for item in dependencies
                if marker_applies(item.marker, extras=requested_extras)
            ),
            provided_extras=extras,
            requires_python=requires_python,
        )

    def read_sibling_wheel_metadata(
        self,
        links: Sequence[Link],
        candidate: CandidateRecord,
    ) -> _ReleaseMetadata | None:
        """The first of ``links`` whose sidecar describes ``candidate``.

        A sidecar naming another project or release is not this candidate's
        metadata whatever the index served it for, so it is passed over
        rather than adopted: what is read here becomes the dependency graph
        of a distribution nothing else has verified.
        """
        session = self.session

        if session is None:
            return None

        for link in islice(links, _SIBLING_METADATA_ATTEMPTS):
            metadata_link = link.metadata_link()

            if metadata_link is None:
                continue

            try:
                response = session.get(metadata_link.url)

                raise_for_status(response)

                headers = parse_metadata_headers(response_text(response))

            except (HttpStatusError, KeyError, OSError, TypeError, ValueError):
                # An advertised sidecar that does not answer is the index's
                # problem, not a reason to fail: the next wheel, or the
                # build, still has the answer.
                continue

            name = headers.get("name", (None,))[0]

            version = headers.get("version", (None,))[0]

            if name is None or version is None:
                continue

            try:
                parsed_version = Version(version)

            except ValueError:
                continue

            if (
                canonicalize_name(name) != candidate.canonical_name
                or parsed_version != candidate.version
            ):
                continue

            return (
                name,
                parsed_version,
                tuple(
                    requirement
                    for value in headers.get("requires-dist", ())
                    if (requirement := parse_requirement(value)) is not None
                ),
                frozenset(headers.get("provides-extra", ())),
                (headers.get("requires-python") or [None])[0],
            )

        return None

    def pypi_metadata(
        self,
        candidate: CandidateRecord,
        requested_extras: frozenset[str],
    ) -> CandidateMetadata | None:
        """Read release metadata when a PyPI sdist backend cannot run.

        ``None`` means PyPI could not answer, not that the release has no
        dependencies; the caller reads the source distribution itself.
        """

        source_url = candidate.link.source_url or candidate.link.url

        host = urllib.parse.urlparse(source_url).hostname

        if host not in {"pypi.org", "pypi.python.org"}:
            return None

        release_key = (candidate.canonical_name, candidate.version.public)

        if release_key in self.release_metadata_cache:
            release = self.release_metadata_cache[release_key]

            if release is None:
                return None

            name, version, all_dependencies, extras, requires_python = release

            return CandidateMetadata(
                name=name,
                version=version,
                dependencies=tuple(
                    requirement
                    for requirement in all_dependencies
                    if marker_applies(requirement.marker, extras=requested_extras)
                ),
                provided_extras=extras,
                requires_python=requires_python,
            )

        url = (
            "https://pypi.org/pypi/"
            f"{urllib.parse.quote(candidate.canonical_name)}/"
            f"{urllib.parse.quote(candidate.version.public)}/json"
        )

        try:
            if self.session is None:
                return None

            response = self.session.get(url)

            if getattr(response, "status", None) == 404:
                self.release_metadata_cache[release_key] = None

                return None

            raise_for_status(response)

            data = json.loads(response_text(response))

            info = data["info"]

            declared = info.get("requires_dist")

            if declared is None:
                # PyPI reports null both for a release that has no
                # dependencies and for one whose dependencies it never
                # learned -- a source distribution whose PKG-INFO predates
                # Requires-Dist, where only the build backend knows. The two
                # are indistinguishable from here, so neither is claimed:
                # reporting "no dependencies" writes a lock that silently
                # omits a real subtree, which is the worse of the two errors.
                self.release_metadata_cache[release_key] = None

                return None

            else:
                dependencies = tuple(
                    requirement
                    for value in tuple(declared)
                    if (requirement := parse_requirement(value)) is not None
                )

                extras = frozenset(info.get("provides_extra") or ())

                release = (
                    str(info["name"]),
                    Version(str(info["version"])),
                    dependencies,
                    extras,
                    info.get("requires_python"),
                )

            self.release_metadata_cache[release_key] = release

            name, version, all_dependencies, extras, requires_python = release

            return CandidateMetadata(
                name=name,
                version=version,
                dependencies=tuple(
                    requirement
                    for requirement in all_dependencies
                    if marker_applies(requirement.marker, extras=requested_extras)
                ),
                provided_extras=extras,
                requires_python=requires_python,
            )

        except (KeyError, OSError, TypeError, ValueError):
            self.release_metadata_cache[release_key] = None

            return None

    def candidate_from_loaded_metadata(
        self,
        candidate: CandidateRecord,
        path: str,
    ) -> WheelCandidate | None:
        """A local wheel's concrete candidate without reopening the wheel.

        The resolver loaded this record's metadata (for the same extras) to
        decide on it; the concrete candidate repeats it. What the install
        may still need from the file -- its layout -- is computed only when
        a consumer asks, which a warm install whose tree is in the archive
        cache never does.
        """

        loader = candidate.metadata_loader

        if loader is None:
            return None

        try:
            metadata = loader.load()

        except Exception:  # noqa: BLE001 - the full path reports the error
            return None

        return WheelCandidate(
            name=metadata.name,
            version=metadata.version,
            path=path,
            dependencies=metadata.dependencies,
            provided_extras=metadata.provided_extras,
            requires_python=metadata.requires_python,
            wheel_layout=LazyWheelLayout(lambda: self.wheel_layout_for(path)),
        )

    @staticmethod
    def wheel_layout_for(path: str) -> object | None:
        """Read a wheel's layout the way eager materialization does."""

        try:
            with _open_resolver_wheel_archive(path) as archive:
                dist_info_dir, wheel_metadata_text = validate_wheel_with_metadata(
                    archive,
                    os.path.basename(os.fspath(path))[:-4].split("-", 1)[0],
                )

                return wheel_candidate(
                    path,
                    archive=archive,
                    dist_info_dir=dist_info_dir,
                    wheel_metadata_text=wheel_metadata_text,
                ).wheel_layout

        except (OSError, UnsupportedWheel, InstallationError):
            return None

    def iter_materialize(
        self,
        requirement: Requirement,
        accepted: tuple[CandidateRecord, ...],
    ) -> Generator[WheelCandidate, None, list[WheelCandidate]]:
        candidates: list[WheelCandidate] = []

        seen: set[tuple[str, str, str]] = set()

        requested_extras = frozenset(requirement.extras)

        for candidate in accepted:
            from_cache = False

            cache_hashes: dict[str, str] | None = None

            cached_built: WheelCandidate | None = None

            built_cache_key: str | None = None

            cache_source_hashes: dict[str, str] | None = None

            local_path = self.local_path_for(candidate)

            vcs_commit = self.vcs_build_commit(candidate)

            path = None

            if (
                vcs_commit is not None
                and self.wheel_cache_dir is not None
                and not candidates
                and candidate.link.kind is ArtifactKind.SOURCE_TREE
            ):
                # A wheel built from this commit needs no checkout to reuse;
                # look before cloning.  The block below finds it again.
                early_key = built_wheel_cache_key(
                    candidate,
                    source_hashes=None,
                    config_settings=(self.build_options or {}).get(requirement.raw),
                    build_constraints=self.build_constraints,
                    build_isolation=self.build_isolation,
                    target_key=self.target_key,
                    vcs_commit=vcs_commit,
                )

                early = cached_wheel_for_link(
                    self.wheel_cache_dir,
                    candidate,
                    early_key,
                    requested_extras=requested_extras,
                    candidate_name_is_authoritative=(not requirement.is_unnamed_direct),
                )

                if early is not None:
                    path = early[0]

                    from_cache = True

            if path is None:
                path = self.ensure_local_text(candidate, local_path=local_path)

            source_hashes = dict(candidate.link.hashes)

            if not source_hashes and self.artifacts is not None:
                cached_hashes = self.artifacts.hashes_for(candidate.link.url)

                if cached_hashes is not None:
                    source_hashes.update(cached_hashes)

            if (
                self.compute_source_hashes
                and not source_hashes
                and local_path is not None
            ):
                try:
                    with open(local_path, "rb") as file:
                        source_hashes["sha256"] = hashlib.sha256(
                            file.read(),
                        ).hexdigest()

                except OSError:
                    pass

            materialized_vcs_path = (
                path if candidate.link.is_vcs and not from_cache else None
            )

            if (
                candidate.link.kind is ArtifactKind.SOURCE_TREE
                and candidate.link.subdirectory_fragment
                and not from_cache
            ):
                path = os.path.join(path, candidate.link.subdirectory_fragment)

            cache_built_wheel = (
                candidate.link.kind is ArtifactKind.SDIST and not candidates
            ) or (
                candidate.link.kind is ArtifactKind.SOURCE_TREE
                and (
                    is_immutable_vcs_link(candidate.link.url)
                    or (
                        vcs_commit is not None
                        and self.vcs_revisions.get(candidate.link.url) == vcs_commit
                    )
                )
                and not candidates
            )

            if candidate.link.kind in SOURCE_ARTIFACT_KINDS:
                display_name = (
                    requirement.name
                    if canonicalize_name(requirement.name) == candidate.canonical_name
                    else candidate.name
                )

                if requirement.name is None:
                    display_name = candidate.name

                config_settings = (self.build_options or {}).get(requirement.raw)

                cache_source_hashes = (
                    self.source_hashes_for(candidate)
                    if self.wheel_cache_dir is not None
                    else None
                )

                built_cache_key = built_wheel_cache_key(
                    candidate,
                    source_hashes=cache_source_hashes,
                    config_settings=config_settings,
                    build_constraints=self.build_constraints,
                    build_isolation=self.build_isolation,
                    target_key=self.target_key,
                    vcs_commit=vcs_commit,
                )

                cached = cached_wheel_for_link(
                    self.wheel_cache_dir,
                    candidate,
                    built_cache_key,
                    requested_extras=requested_extras,
                    candidate_name_is_authoritative=(not requirement.is_unnamed_direct),
                )

                if cached is not None:
                    path, cache_hashes, cached_built = cached

                    from_cache = True

                    cached_name = os.path.basename(os.fspath(path)).split("-", 1)[0]

                    logger.debug(
                        "use cached built wheel for %s from %s",
                        display_name,
                        candidate.link.url,
                    )

                    emit_build_message(f"Using cached {cached_name}")

                else:
                    emit_build_message("Preparing build dependencies")

                    prepared_sdist = (
                        self.take_prepared_sdist(candidate)
                        if candidate.link.kind is ArtifactKind.SDIST
                        else None
                    )

                    if prepared_sdist is not None:
                        path = prepared_sdist[1]

                    logger.debug(
                        "build source candidate %s from %s",
                        display_name,
                        candidate.link.url,
                    )

                    emit_build_message(f"Building wheel for {display_name}")

                    try:
                        try:
                            path = build_wheel_from_source(
                                path,
                                config_settings=config_settings,
                                build_constraints=self.build_constraints,
                                build_isolation=self.build_isolation,
                            )
                        finally:
                            if prepared_sdist is not None:
                                prepared_sdist[0].cleanup()

                    except BuildError as exc:
                        emit_build_message(f"Failed to build '{display_name}'")

                        raise BuildError(
                            f"Failed to build '{display_name}': {exc}",
                        ) from exc

                    emit_build_message(f"Created wheel for {display_name}")

                    emit_build_message(f"Successfully built {display_name}")

            try:
                if candidate.link.kind is ArtifactKind.WHEEL:
                    cache_key = (
                        candidate.link.url,
                        self.artifact_fingerprint(candidate),
                        requested_extras,
                    )

                    built = self.wheel_candidates.get(cache_key)

                    if built is None and candidate.link.is_file:
                        built = self.candidate_from_loaded_metadata(
                            candidate,
                            path,
                        )

                        if built is not None:
                            self.wheel_candidates[cache_key] = built

                    if built is None:
                        with _open_resolver_wheel_archive(path) as archive:
                            dist_info_dir, wheel_metadata_text = (
                                validate_wheel_with_metadata(
                                    archive,
                                    os.path.basename(os.fspath(path))[:-4].split(
                                        "-",
                                        1,
                                    )[0],
                                )
                            )

                            built = wheel_candidate(
                                path,
                                requested_extras,
                                archive=archive,
                                filename_info=(candidate.name, candidate.version),
                                dist_info_dir=dist_info_dir,
                                wheel_metadata_text=wheel_metadata_text,
                            )

                        self.wheel_candidates[cache_key] = built

                else:
                    built = (
                        cached_built
                        if cached_built is not None
                        else wheel_candidate_from_path(path, requested_extras)
                    )

            except UnsupportedWheel as exc:
                if ".dist-info directory" not in str(exc):
                    self.invalid_links.add(candidate.link.url)

                    logger.warning("%s", exc)

                    continue

                raise

            except ValueError:
                self.invalid_links.add(candidate.link.url)

                print(
                    f"WARNING: Ignoring version {candidate.version} of "
                    f"{candidate.name} since it has invalid metadata",
                    file=sys.stderr,
                )

                continue

            if built.version != candidate.version and candidate.version != ZERO_VERSION:
                print(
                    f"WARNING: {candidate.name} has an inconsistent version: "
                    f"expected '{candidate.version}', but metadata has "
                    f"'{built.version}'",
                )

                if requirement.extras:
                    print(
                        f"Requested {requirement.raw or requirement.name}, "
                        f"but installing version {built.version}",
                    )

                self.invalid_links.add(candidate.link.url)

                continue

            if cache_built_wheel and not from_cache:
                logger.debug(
                    "store cached built wheel for %s from %s",
                    display_name,
                    candidate.link.url,
                )

                store_cached_wheel(
                    self.wheel_cache_dir,
                    candidate,
                    path,
                    built_cache_key,
                    source_hashes=cache_source_hashes,
                    candidate_name_is_authoritative=(not requirement.is_unnamed_direct),
                )

            wheel = WheelCandidate(
                name=built.name,
                version=built.version,
                path=built.path,
                dependencies=built.dependencies,
                provided_extras=built.provided_extras,
                requires_python=built.requires_python or candidate.link.requires_python,
                wheel_layout=built.stored_wheel_layout,
                source_url=candidate.link.url,
                source_hashes=cache_hashes
                if cache_hashes is not None
                else source_hashes,
                source_kind=candidate.link.kind.value,
                source_vcs=vcs_scheme(candidate.link.url)
                if candidate.link.is_vcs
                else None,
                from_cache=from_cache,
                yanked_reason=candidate.link.yanked_reason,
            )

            key = (wheel.canonical_name, str(wheel.version), str(wheel.path))

            if key in seen:
                logger.debug(
                    "dedupe candidate %s==%s path=%s",
                    wheel.name,
                    wheel.version,
                    os.path.basename(wheel.path),
                )

                if materialized_vcs_path is not None:
                    release_checkout(materialized_vcs_path)

                continue

            seen.add(key)

            candidates.append(wheel)

            if materialized_vcs_path is not None:
                release_checkout(materialized_vcs_path)

            logger.debug(
                "candidate ready %s==%s kind=%s",
                candidate.name,
                candidate.version,
                candidate.link.kind.value,
            )

            yield wheel

        logger.debug(
            "materialization completed requirement=%s produced=%d",
            requirement.raw or requirement.name,
            len(candidates),
        )

        return candidates


def validate_build_requirements(source: str | os.PathLike[str]) -> None:
    from kpip.build.build_backend import BackendSpec

    BackendSpec.from_project(source)
