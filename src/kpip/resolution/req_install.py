from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

from kpip.build.pep517_hooks import BuildBackendHookCaller, HookMissing
from kpip.core.errors import (
    InstallationError,
)
from kpip.core.hashes import Hashes
from kpip.core.interpreter import build_interpreter
from kpip.core.packaging import (
    Requirement as ParsedRequirement,
)
from kpip.core.packaging import (
    SpecifierSet,
    canonicalize_name,
    marker_applies,
)
from kpip.core.versions import Version
from kpip.index.links import Link

if TYPE_CHECKING:
    import email.message

logger = logging.getLogger(__name__)


class MetadataProvider(Protocol):
    """Prepared distribution view required by requirement metadata consumers."""

    @property
    def metadata(self) -> email.message.Message: ...

    @property
    def version(self) -> Version: ...


class VcsInfo:
    __slots__ = ("vcs",)

    def __init__(self, vcs: str) -> None:
        self.vcs = vcs


class NoOpBuildEnvironment_internal:
    @property
    def python_executable(self) -> str:
        return build_interpreter()

    def __enter__(self) -> NoOpBuildEnvironment_internal:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class InstallRequirement:
    __slots__ = (
        "archive_source_internal",
        "build_env",
        "cached_wheel_source_link",
        "comes_from",
        "config_settings",
        "constraint",
        "distribution_internal",
        "download_info",
        "editable",
        "extras_override",
        "hash_options",
        "install_succeeded",
        "is_wheel_from_cache",
        "isolated",
        "link",
        "local_file_path",
        "marker_internal",
        "metadata_directory",
        "metadata_internal",
        "needs_more_preparation",
        "original_link",
        "pep517_backend",
        "permit_editable_wheels",
        "pyproject_data",
        "pyproject_requires",
        "req",
        "requirements_to_check",
        "satisfied_by",
        "should_reinstall",
        "source_dir",
        "user_supplied",
    )

    def __init__(
        self,
        req: ParsedRequirement | None,
        comes_from: InstallRequirement | str | None = None,
        link: Link | None = None,
        marker_internal: str | None = None,
        editable: bool = False,
        isolated: bool = False,
        hash_options: dict[str, list[str]] | None = None,
        constraint: bool = False,
        config_settings: dict[str, object] | None = None,
        user_supplied: bool = False,
        permit_editable_wheels: bool = False,
        original_link: Link | None = None,
        satisfied_by: MetadataProvider | None = None,
        extras_override: set[str] | None = None,
        source_dir: str | None = None,
        local_file_path: str | None = None,
        download_info: Any = None,
        is_wheel_from_cache: bool = False,
        cached_wheel_source_link: Link | None = None,
        metadata_internal: email.message.Message | None = None,
        distribution_internal: MetadataProvider | None = None,
        archive_source_internal: str | os.PathLike[str] | None = None,
        needs_more_preparation: bool = False,
        build_env: Any = None,
        pyproject_requires: list[str] | None = None,
        requirements_to_check: list[str] | None = None,
        metadata_directory: str | None = None,
        pyproject_data: dict[str, object] | None = None,
        pep517_backend: BuildBackendHookCaller | None = None,
        should_reinstall: bool = False,
        install_succeeded: bool | None = None,
    ) -> None:
        self.req = req

        self.comes_from = comes_from

        self.link = link

        self.marker_internal = marker_internal

        self.editable = editable

        self.isolated = isolated

        self.hash_options = hash_options if hash_options is not None else {}

        self.constraint = constraint

        self.config_settings = config_settings

        self.user_supplied = user_supplied

        self.permit_editable_wheels = permit_editable_wheels

        self.original_link = original_link

        self.satisfied_by = satisfied_by

        self.extras_override = extras_override

        self.source_dir = source_dir

        self.local_file_path = local_file_path

        self.download_info = download_info

        self.is_wheel_from_cache = is_wheel_from_cache

        self.cached_wheel_source_link = cached_wheel_source_link

        self.metadata_internal = metadata_internal

        self.distribution_internal = distribution_internal

        self.archive_source_internal = archive_source_internal

        self.needs_more_preparation = needs_more_preparation

        self.build_env = (
            build_env if build_env is not None else NoOpBuildEnvironment_internal()
        )

        self.pyproject_requires = pyproject_requires

        self.requirements_to_check = (
            requirements_to_check if requirements_to_check is not None else []
        )

        self.metadata_directory = metadata_directory

        self.pyproject_data = pyproject_data

        self.pep517_backend = pep517_backend

        self.should_reinstall = should_reinstall

        self.install_succeeded = install_succeeded

        self.__post_init__()

    def __post_init__(self) -> None:
        if self.link is None and self.req is not None and self.req.url is not None:
            self.link = Link(self.req.url)

        if self.local_file_path is None and self.link is not None and self.link.is_file:
            self.local_file_path = self.link.file_path

    @property
    def extras(self) -> set[str]:
        if self.extras_override is not None:
            return set(self.extras_override)

        return set(self.req.extras) if self.req is not None else set()

    @property
    def name(self) -> str | None:
        return self.req.name if self.req is not None else None

    @property
    def specifier(self) -> SpecifierSet:
        if self.req is None:
            raise ValueError("requirement has no parsed requirement")

        return self.req.specifier

    @property
    def markers(self) -> str | None:
        if self.marker_internal is not None:
            return self.marker_internal

        return self.req.marker if self.req is not None else None

    @property
    def is_wheel(self) -> bool:
        return self.link is not None and self.link.filename.endswith(".whl")

    @property
    def is_direct(self) -> bool:
        """Whether this requirement was specified with a direct URL."""

        return self.req is not None and self.req.url is not None

    @property
    def is_pinned(self) -> bool:
        """Whether this requirement is constrained to one exact version."""

        if self.req is None:
            raise ValueError("requirement has no parsed requirement")

        return self.req.specifier.is_pinned

    def hashes(self, trust_internet: bool = True) -> Hashes:
        values = {
            algorithm: list(digests) for algorithm, digests in self.hash_options.items()
        }

        link = self.link if trust_internet else None

        if link is not None and link.hash and link.hash_name:
            existing = values.get(link.hash_name)
            if existing is None:
                values[link.hash_name] = [link.hash]
            else:
                existing.append(link.hash)

        return Hashes(values)

    def is_satisfied_by(self, candidate: object) -> bool:
        if self.req is None:
            return False

        expected = self.req.name

        if expected.startswith("file://") and self.link is not None:
            expected = self.link.filename.split("-", 1)[0]

        return getattr(candidate, "name", None) == expected

    def match_markers(self, extras_requested: Iterable[str] = ()) -> bool:
        return marker_applies(self.markers, extras=extras_requested)

    def get_dist(self) -> MetadataProvider:
        if self.distribution_internal is None:
            raise AssertionError(f"InstallRequirement {self} has no distribution")

        return self.distribution_internal

    @property
    def metadata(self) -> email.message.Message:
        if self.metadata_internal is None:
            distribution = self.get_dist()

            self.metadata_internal = distribution.metadata

        return self.metadata_internal

    def assert_source_matches_version(self) -> None:
        if self.req is None or self.metadata_internal is None:
            return

        requested = str(self.req.specifier)

        actual = self.metadata_internal.get("version")

        if requested and actual and requested != f"=={actual}":
            logger.warning(
                "Requested %s%s, but installing version %s",
                self.req.name,
                requested,
                actual,
            )

    def warn_on_mismatching_name(self) -> None:
        """Normalize the requirement name to generated distribution metadata."""

        if self.req is None or self.metadata_internal is None:
            return

        metadata_name = self.metadata_internal.get("name")

        if not metadata_name:
            return

        if canonicalize_name(self.req.name) == canonicalize_name(metadata_name):
            return

        logger.warning(
            "Generating metadata for package %s produced metadata for project "
            "name %s. Fix your #egg=%s fragments.",
            self.name,
            canonicalize_name(metadata_name),
            self.name,
        )

        self.req = ParsedRequirement(
            name=canonicalize_name(metadata_name),
            specifier=self.req.specifier,
            extras=self.req.extras,
            url=(
                self.req.url
                or (
                    self.link.url
                    if self.link is not None
                    and (
                        self.link.is_existing_dir
                        or self.link.is_file
                        or self.link.is_vcs
                    )
                    else None
                )
            ),
            marker=self.req.marker,
            raw=self.req.raw,
        )

    def prepare_metadata(self) -> None:
        """Ask the configured backend to generate the project metadata."""

        if self.source_dir is None or self.pep517_backend is None:
            raise InstallationError(f"Cannot prepare metadata for {self}")

        metadata_root = tempfile.mkdtemp(prefix="kpip-modern-metadata-")

        editable = self.editable and self.permit_editable_wheels

        try:
            if editable:
                metadata_name = self.pep517_backend.prepare_metadata_for_build_editable(
                    metadata_root,
                    config_settings=self.config_settings,
                )
            else:
                metadata_name = self.pep517_backend.prepare_metadata_for_build_wheel(
                    metadata_root,
                    config_settings=self.config_settings,
                )
        except HookMissing as exc:
            hook_name = (
                "prepare_metadata_for_build_editable"
                if editable
                else "prepare_metadata_for_build_wheel"
            )
            raise InstallationError(
                f"Build backend for {self} is missing the required '{hook_name}' hook",
            ) from exc

        self.metadata_directory = os.path.join(metadata_root, str(metadata_name))

        self.warn_on_mismatching_name()

        self.assert_source_matches_version()

    def __str__(self) -> str:
        return (
            str(self.req)
            if self.req is not None
            else str(self.link.url if self.link else "")
        )

    def __repr__(self) -> str:
        return f"<InstallRequirement object: {self} editable={self.editable}>"

    def from_path(self) -> str | None:
        """Format the requirement and its source provenance."""

        if self.req is None:
            return None

        result = str(self.req)

        if self.comes_from:
            source = (
                self.comes_from
                if isinstance(self.comes_from, str)
                else self.comes_from.from_path()
            )

            if source:
                result += "->" + source

        return result

    @property
    def unpacked_source_directory(self) -> str:
        if self.source_dir is None:
            raise ValueError(f"No source directory for {self}")

        subdirectory = self.link.subdirectory_fragment if self.link else None

        return os.path.join(self.source_dir, subdirectory or "")

    @property
    def setup_py_path(self) -> str:
        return os.path.join(self.unpacked_source_directory, "setup.py")
