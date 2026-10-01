"""Candidate filtering and ranking for the package finder."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from kpip.core.caches import memoized
from kpip.core.hashes import Hashes
from kpip.core.packaging import (
    Requirement,
    SpecifierSet,
    requires_python_version,
    target_python_version,
)
from kpip.core.release_control import ReleaseControl
from kpip.core.target_python import get_supported
from kpip.core.versions import ZERO_VERSION, Version
from kpip.core.wheel import TargetContext, WheelTag
from kpip.index.candidate_filters import (
    allowed_hashes,
    filter_unallowed_hashes,
    supported_tag_ranks,
)
from kpip.index.links import Link
from kpip.index.source_models import (
    INSTALLABLE_ARTIFACT_KINDS,
    SOURCE_ARTIFACT_KINDS,
    ArtifactKind,
    CandidateRecord,
    RejectedCandidate,
    RejectionReason,
)

CandidateT = TypeVar("CandidateT", bound=CandidateRecord)

_UNKNOWN_DIRECT_SOURCE_VERSION = ZERO_VERSION


class CandidateEvaluator:
    __slots__ = (
        "allowed_hashes_internal",
        "hashes_internal",
        "prefer_binary_internal",
        "project_name_internal",
        "release_control_internal",
        "specifier_internal",
        "supported_tag_ranks",
        "supported_tags_internal",
    )

    def __init__(
        self,
        project_name: str,
        *,
        supported_tags: Sequence[WheelTag],
        specifier: SpecifierSet,
        release_control: ReleaseControl | None = None,
        prefer_binary: bool = False,
        hashes: Hashes | None = None,
    ) -> None:
        self.project_name_internal = project_name

        self.supported_tags_internal = tuple(supported_tags)

        self.supported_tag_ranks = supported_tag_ranks(self.supported_tags_internal)

        self.specifier_internal = specifier

        self.release_control_internal = release_control

        self.prefer_binary_internal = prefer_binary

        self.hashes_internal = hashes

        self.allowed_hashes_internal = allowed_hashes(hashes)

    @classmethod
    def create(
        cls,
        project_name: str,
        *,
        target: TargetContext | None = None,
        release_control: ReleaseControl | None = None,
        prefer_binary: bool = False,
        specifier: SpecifierSet | None = None,
        hashes: Hashes | None = None,
    ) -> CandidateEvaluator:
        if target is None:
            supported_tags = get_supported()

        else:
            supported_tags = get_supported(
                version=target.python_version,
                platforms=list(target.platforms),
                impl=target.implementation,
                abis=list(target.abis),
            )

        return cls(
            project_name,
            supported_tags=supported_tags,
            specifier=specifier if specifier is not None else SpecifierSet(),
            release_control=release_control,
            prefer_binary=prefer_binary,
            hashes=hashes,
        )

    def get_applicable_candidates(
        self,
        candidates: list[CandidateT],
    ) -> list[CandidateT]:
        allow_prereleases = self.allow_prereleases_internal()

        if allow_prereleases is None:
            specifier_allows_prereleases = (
                self.specifier_internal.explicitly_allows_prereleases
            )

            if specifier_allows_prereleases:
                applicable = [
                    candidate
                    for candidate in candidates
                    if self.specifier_internal.contains(
                        candidate.version,
                        allow_prereleases=True,
                    )
                ]

            else:
                stable = [
                    candidate
                    for candidate in candidates
                    if not candidate.version.is_prerelease
                    and self.specifier_internal.contains(candidate.version)
                ]

                if stable:
                    applicable = stable

                else:
                    applicable = [
                        candidate
                        for candidate in candidates
                        if self.specifier_internal.contains(
                            candidate.version,
                            allow_prereleases=True,
                        )
                    ]

        else:
            applicable = [
                candidate
                for candidate in candidates
                if not (candidate.version.is_prerelease and not allow_prereleases)
                and self.specifier_internal.contains(
                    candidate.version,
                    allow_prereleases=allow_prereleases,
                )
            ]

        return filter_unallowed_hashes(
            applicable,
            hashes=self.hashes_internal,
            project_name=self.project_name_internal,
        )

    def allow_prereleases_internal(self) -> bool | None:
        if self.release_control_internal is None:
            return None

        return self.release_control_internal.allows_prereleases(
            self.project_name_internal,
        )

    @staticmethod
    def evaluate_parsed_link(
        link: Link,
        parsed: CandidateRecord | RejectedCandidate,
        requirement: Requirement,
        *,
        allow_yanked: bool,
        allow_binary: bool,
        allow_source: bool,
    ) -> CandidateRecord | RejectedCandidate:
        """Apply requirement-specific policy to an already parsed link."""

        unnamed_direct = requirement.is_unnamed_direct

        if link.kind is ArtifactKind.WHEEL and not allow_binary:
            return CandidateEvaluator.reject(
                link,
                RejectionReason.UNSUPPORTED_ARTIFACT,
                "binary distributions are disabled",
            )

        if link.kind in SOURCE_ARTIFACT_KINDS and not allow_source:
            return CandidateEvaluator.reject(
                link,
                RejectionReason.UNSUPPORTED_ARTIFACT,
                "source distributions are disabled",
            )

        if isinstance(parsed, RejectedCandidate):
            if (
                unnamed_direct
                and link.kind is ArtifactKind.SDIST
                and parsed.reason is RejectionReason.INVALID_VERSION
            ):
                parsed = CandidateRecord(
                    name=requirement.name,
                    version=_UNKNOWN_DIRECT_SOURCE_VERSION,
                    link=link,
                )

            else:
                return parsed

        if not unnamed_direct and parsed.canonical_name != requirement.canonical_name:
            return CandidateEvaluator.reject(
                link,
                RejectionReason.DIFFERENT_PROJECT,
                f"wrong project name: {parsed.name}",
            )

        unknown_direct_source_version = (
            unnamed_direct
            and link.kind in SOURCE_ARTIFACT_KINDS
            and parsed.version == _UNKNOWN_DIRECT_SOURCE_VERSION
        )

        if not unknown_direct_source_version and not requirement.is_satisfied_by(
            parsed.version,
        ):
            return CandidateEvaluator.reject(
                link,
                RejectionReason.VERSION_MISMATCH,
                f"{parsed.version} does not satisfy {requirement.specifier}",
            )

        if link.requires_python:
            try:
                if not CandidateEvaluator.requires_python_matches(link.requires_python):
                    return CandidateEvaluator.reject(
                        link,
                        RejectionReason.REQUIRES_PYTHON,
                        f"requires Python {link.requires_python}",
                    )

            except ValueError:
                return CandidateEvaluator.reject(
                    link,
                    RejectionReason.REQUIRES_PYTHON,
                    f"invalid Requires-Python: {link.requires_python}",
                )

        if link.is_yanked and not (allow_yanked or requirement.specifier.is_pinned):
            return CandidateEvaluator.reject(
                link,
                RejectionReason.YANKED,
                link.yanked_reason or "yanked",
            )

        if link.kind is ArtifactKind.WHEEL and parsed.tag_rank is None:
            return CandidateEvaluator.reject(
                link,
                RejectionReason.UNSUPPORTED_WHEEL,
                "wheel tags are not supported by this interpreter",
            )

        if link.kind not in INSTALLABLE_ARTIFACT_KINDS:
            return CandidateEvaluator.reject(
                link,
                RejectionReason.UNSUPPORTED_ARTIFACT,
                f"{link.kind.value} candidates are not installable yet",
            )

        return parsed

    @staticmethod
    @memoized(4096)
    def requires_python_matches(
        requires_python: str,
        python: str | None = None,
    ) -> bool:
        """Whether the targeted interpreter satisfies ``requires_python``.

        ``python`` names the target explicitly, for the callers that hold
        one; the rest fall back to the process-wide target a cross-version
        resolve installs, and to the target interpreter when there is none.

        Registered with the cache registry rather than memoized on its own,
        because a verdict is only true of the interpreter it was asked
        about: changing the target has to drop it. It was unregistered
        before, which also meant a benchmark that resets the caches went on
        measuring this one warm.
        """
        if python is None:
            python = target_python_version()

        try:
            specifier = SpecifierSet(requires_python)
        except ValueError:
            # A declaration that does not parse -- ``>=3.6.*`` is common on
            # old releases -- says nothing, and pip ignores it rather than
            # the release: rejecting it made the two choose different
            # versions.
            return True

        return specifier.contains(
            Version(requires_python_version() if python is None else python),
        )

    @staticmethod
    def reject(link: Link, reason: RejectionReason, detail: str) -> RejectedCandidate:
        return RejectedCandidate(link=link, reason=reason, detail=detail)
