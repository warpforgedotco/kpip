from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import Enum
from typing import TYPE_CHECKING, Callable, Protocol

from kpip.core.packaging import Requirement, canonicalize_name
from kpip.core.versions import Version
from kpip.core.wheel import CandidateMetadata

if TYPE_CHECKING:
    from kpip.core.wheel import WheelFile
    from kpip.index.links import Link


class ArtifactKind(Enum):
    WHEEL = "wheel"

    SDIST = "sdist"

    SOURCE_TREE = "source-tree"

    METADATA = "metadata"

    ATTESTATION = "attestation"

    UNKNOWN = "unknown"

    # ``Enum.__hash__`` is a Python-level ``hash(self._name_)``, and these are
    # hashed on every link the index evaluates -- a frozenset membership test
    # per artifact, tens of thousands of them in one resolve. Members are
    # singletons and compare by identity, which is exactly what the inherited
    # hash is, so taking it back gives the same answer from a C slot with no
    # frame to push.
    __hash__ = object.__hash__


SOURCE_ARTIFACT_KINDS = frozenset((ArtifactKind.SDIST, ArtifactKind.SOURCE_TREE))

INSTALLABLE_ARTIFACT_KINDS = frozenset(
    (ArtifactKind.WHEEL, ArtifactKind.SDIST, ArtifactKind.SOURCE_TREE),
)


class RejectionReason(Enum):
    DIFFERENT_PROJECT = "different-project"

    INVALID_VERSION = "invalid-version"

    VERSION_MISMATCH = "version-mismatch"

    REQUIRES_PYTHON = "requires-python"

    YANKED = "yanked"

    UNSUPPORTED_WHEEL = "unsupported-wheel"

    UNSUPPORTED_ARTIFACT = "unsupported-artifact"

    INVALID_WHEEL = "invalid-wheel"

    MISSING_ARTIFACT = "missing-artifact"


class MetadataFile:
    __slots__ = ("hashes",)

    def __init__(self, hashes: dict[str, str] | None) -> None:
        self.hashes = hashes

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MetadataFile) and self.hashes == other.hashes


class VcsReference:
    __slots__ = ("repo_url", "requested_revision", "vcs")

    def __init__(self, vcs: str, repo_url: str, requested_revision: str | None) -> None:
        self.vcs = vcs

        self.repo_url = repo_url

        self.requested_revision = requested_revision


class RejectedCandidate:
    __slots__ = ("detail", "link", "reason")

    def __init__(self, link: Link, reason: RejectionReason, detail: str) -> None:
        self.link = link

        self.reason = reason

        self.detail = detail


class CandidateSelection:
    __slots__ = ("accepted", "rejected")

    def __init__(
        self,
        accepted: tuple[CandidateRecord, ...],
        rejected: tuple[RejectedCandidate, ...],
    ) -> None:
        self.accepted = accepted

        self.rejected = rejected


class CandidateSummary:
    __slots__ = ("is_yanked", "version", "yanked_reason")

    def __init__(
        self,
        version: Version,
        is_yanked: bool,
        yanked_reason: str | None,
    ) -> None:
        self.version = version

        self.is_yanked = is_yanked

        self.yanked_reason = yanked_reason


class LazyCandidateMetadata:
    """A one-shot, memoized metadata computation for a candidate."""

    __slots__ = ("loader", "value")

    def __init__(self, loader: Callable[[], CandidateMetadata]) -> None:
        self.loader = loader

        self.value: CandidateMetadata | None = None

    def load(self) -> CandidateMetadata:
        metadata = self.value

        if metadata is None:
            metadata = self.loader()

            self.value = metadata

        return metadata


class CandidateRecord:
    """Immutable discovery result that does not imply artifact materialization."""

    __slots__ = (
        "_canonical_name",
        "link",
        "metadata_loader",
        "name",
        "tag_rank",
        "version",
        "wheel",
    )

    def __init__(
        self,
        name: str,
        version: Version,
        link: Link,
        wheel: WheelFile | None = None,
        tag_rank: int | None = None,
        metadata_loader: LazyCandidateMetadata | None = None,
    ) -> None:
        self.name = name

        self.version = version

        self.link = link

        self.wheel = wheel

        self.tag_rank = tag_rank

        self.metadata_loader = metadata_loader

        self._canonical_name = canonicalize_name(name)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CandidateRecord) and (
            self.name,
            self.version,
            self.link,
            self.wheel,
            self.tag_rank,
        ) == (other.name, other.version, other.link, other.wheel, other.tag_rank)

    def __hash__(self) -> int:
        return hash((self.name, self.version, self.link, self.wheel, self.tag_rank))

    def with_metadata_loader(
        self,
        metadata_loader: LazyCandidateMetadata,
    ) -> CandidateRecord:
        """This record, carrying a loader, without going through kwargs.

        ``copy_with`` is the general form and builds a dict of every field
        to unpack it again. Attaching a loader is the one copy the resolver
        makes in bulk -- once per candidate it looks at -- and it knows
        every field already.
        """
        return CandidateRecord(
            self.name,
            self.version,
            self.link,
            self.wheel,
            self.tag_rank,
            metadata_loader,
        )

    def copy_with(self, **changes: object) -> CandidateRecord:
        values = {
            "name": self.name,
            "version": self.version,
            "link": self.link,
            "wheel": self.wheel,
            "tag_rank": self.tag_rank,
            "metadata_loader": self.metadata_loader,
        }

        values.update(changes)

        return type(self)(**values)

    @property
    def canonical_name(self) -> str:
        return self._canonical_name

    def sort_key(self, *, prefer_binary: bool) -> tuple[object, object, object, int]:
        wheel_rank = 1 if self.link.kind is ArtifactKind.WHEEL else 0

        tag_rank = -(self.tag_rank if self.tag_rank is not None else 1_000_000)

        yanked_rank = 0 if self.link.is_yanked else 1

        version_key = self.version

        if prefer_binary:
            return (yanked_rank, wheel_rank, version_key, tag_rank)

        return (yanked_rank, version_key, wheel_rank, tag_rank)

    def metadata(self) -> CandidateMetadata:
        loader = self.metadata_loader

        if loader is None:
            raise RuntimeError("candidate metadata loader is not configured")

        metadata = loader.value

        if metadata is None:
            metadata = loader.load()

        return metadata


class UniformRecords(Mapping[Version, tuple[object, ...]]):
    """``records_by_version`` for a catalog every release of which has the
    same records: one source's summary, where they name the source.

    A dict of them cost a resolve an insertion for each of 56,000 releases
    on airflow's graph, each storing the same tuple.
    """

    __slots__ = ("_members", "_ordered", "_records")

    def __init__(
        self, ordered: tuple[Version, ...], records: tuple[object, ...]
    ) -> None:
        self._ordered = ordered
        self._members = frozenset(ordered)
        self._records = records

    def __getitem__(self, version: Version) -> tuple[object, ...]:
        if version in self._members:
            return self._records
        raise KeyError(version)

    def get(self, version: Version, default: object = None) -> object:  # ty: ignore[invalid-method-override]
        return self._records if version in self._members else default

    def __contains__(self, version: object) -> bool:
        return version in self._members

    def __iter__(self) -> Iterator[Version]:
        return iter(self._ordered)

    def __len__(self) -> int:
        return len(self._ordered)


class PackageCatalog:
    """Immutable package metadata shared by candidate and resolver queries.

    ``summaries`` may be built on first use from ``summary_versions`` and
    the positions of its yanked entries: the resolver reads only the
    versions and which are yanked (:attr:`yanked_versions`), and building a
    summary per release cost a warm airflow resolve 56,000 objects.
    """

    __slots__ = (
        "_summaries",
        "_yanked",
        "candidates_by_version",
        "links",
        "links_by_version",
        "records_by_version",
        "summary_versions",
    )

    def __init__(
        self,
        links: tuple[Link, ...],
        candidates_by_version: Mapping[Version, tuple[CandidateRecord, ...]],
        summaries: tuple[CandidateSummary, ...] | None,
        summary_versions: tuple[Version, ...],
        links_by_version: Mapping[Version, tuple[Link, ...]],
        records_by_version: Mapping[Version, tuple[object, ...]] | None = None,
        yanked: Mapping[int, str | None] | None = None,
    ) -> None:
        self.links = links

        self.candidates_by_version = candidates_by_version

        self._summaries = summaries

        # With no summaries given: the positions in ``summary_versions`` of
        # yanked entries, and each one's reason.
        self._yanked = yanked

        self.summary_versions = summary_versions

        self.links_by_version = links_by_version

        self.records_by_version = records_by_version

    @property
    def summaries(self) -> tuple[CandidateSummary, ...]:
        summaries = self._summaries
        if summaries is None:
            yanked = self._yanked or {}
            summaries = tuple(
                [
                    CandidateSummary(version, position in yanked, yanked.get(position))
                    for position, version in enumerate(self.summary_versions)
                ]
            )
            self._summaries = summaries
        return summaries

    @property
    def yanked_versions(self) -> frozenset[Version]:
        """The versions with a yanked entry among the summaries."""
        if self._summaries is None:
            versions = self.summary_versions
            return frozenset([versions[position] for position in self._yanked or ()])
        return frozenset(
            [summary.version for summary in self._summaries if summary.is_yanked]
        )


class PackageSource(Protocol):
    def collect_links(self, requirement: Requirement) -> list[Link]: ...
