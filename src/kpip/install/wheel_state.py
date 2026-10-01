"""Existing-installation and bytecode bookkeeping for wheel installs."""

from __future__ import annotations

from typing import TYPE_CHECKING
import csv
import os
import stat
from collections.abc import Iterable, Mapping

from kpip.build.metadata import InstalledDistributionStore
from kpip.core.errors import InstallationError
from kpip.core.names import (
    canonicalize_installed_name,
    installed_name_might_match,
)
from kpip.core.versions import version_of
from kpip.install.uninstall import _inside_distribution
from kpip.install.bytecode import (
    CompileJob,
    compile_modules,
    place_pyc,
    pyc_path,
    target_magic,
)
from kpip.install.wheel_archive import compiled_parts, mapped_parts
from kpip.install.wheel_archive_cache import bytecode_tree

if TYPE_CHECKING:
    from kpip.build.metadata import InstalledMetadataDistribution
    from kpip.install.target import InstallTarget
    from kpip.install.wheel_archive_cache import CachedWheelArchive


class InstalledWheelDistribution:
    """Minimal installed-wheel metadata used by replacement transactions."""

    __slots__ = (
        "canonical_name",
        "info_location",
        "location",
        "raw_name",
        "raw_version",
        "version",
    )

    def __init__(
        self,
        *,
        location: str,
        info_location: str,
        name: str,
        version: str,
    ) -> None:
        self.location = location

        self.info_location = info_location

        self.raw_name = name

        self.raw_version = version

        self.version = version_of(version)

        self.canonical_name = canonicalize_installed_name(name)

    def read_text(self, path: str) -> str:
        with open(os.path.join(self.info_location, path), encoding="utf-8") as file:
            return file.read()


def _wheel_metadata_identity(path: str) -> tuple[str, str] | None:
    name = None

    version = None

    try:
        with open(path, encoding="utf-8") as file:
            for raw_line in file:
                if raw_line in {"\n", "\r\n"}:
                    break

                key, separator, value = raw_line.partition(":")

                if not separator:
                    continue

                key = key.casefold()

                if key == "name" and name is None:
                    name = value.strip()

                elif key == "version" and version is None:
                    version = value.strip()

                if name and version:
                    return name, version

    except OSError, UnicodeDecodeError:
        return None

    return None


def discover_installed_wheels(
    paths: Iterable[str],
    *,
    names: set[str] | None = None,
) -> dict[str, InstalledWheelDistribution] | None:
    """Discover ordinary dist-info installs without loading packaging metadata.



    ``None`` requests the authoritative metadata scanner.  It is returned for

    legacy, malformed, or ambiguous layouts rather than guessing about file

    ownership during an upgrade.

    """

    requested = names

    result: dict[str, InstalledWheelDistribution] = {}

    scanned: set[str] = set()

    for value in paths:
        root = os.path.abspath(os.fspath(value))

        if root in scanned:
            continue

        scanned.add(root)

        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if entry.name.endswith(".dist-info"):
                        suffix = ".dist-info"

                    elif entry.name.endswith(".egg-info"):
                        suffix = ".egg-info"

                    elif entry.name.endswith(".egg-link"):
                        suffix = ".egg-link"

                    else:
                        continue

                    if requested is not None and not installed_name_might_match(
                        entry.name,
                        suffix,
                        requested,
                    ):
                        continue

                    if suffix != ".dist-info":
                        return None

                    if not entry.is_dir(follow_symlinks=False):
                        return None

                    identity = _wheel_metadata_identity(
                        os.path.join(entry.path, "METADATA")
                    )

                    if identity is None:
                        return None

                    name, version = identity

                    canonical_name = canonicalize_installed_name(name)

                    if requested is not None and canonical_name not in requested:
                        return None

                    if canonical_name in result:
                        return None

                    result[canonical_name] = InstalledWheelDistribution(
                        location=root,
                        info_location=entry.path,
                        name=name,
                        version=version,
                    )

        except FileNotFoundError:
            continue

        except OSError:
            return None

    return result


def compiled_files(
    stage_root: str,
    staged: Iterable[tuple[str, str, str, int | None]],
    *,
    archive: CachedWheelArchive | None = None,
    members: Mapping[str, str] | None = None,
) -> list[tuple[str, str, str, int | None]]:
    """The target interpreter's ``.pyc`` beside each staged module, naming
    where the module will live, not the stage.

    A module read from the archive cache entry ``archive`` -- ``members``
    maps its staged path to its name in the wheel -- takes the entry's
    cached bytecode (:func:`place_pyc`); the rest are compiled.
    """
    python_files = [
        (source, destination)
        for source, destination, _, _ in staged
        if os.path.splitext(os.fspath(source))[1] == ".py"
    ]

    if not python_files:
        return []

    tree = bytecode_tree(archive) if archive is not None and members else None

    magic = target_magic() if tree is not None else b""

    placed: list[tuple[str, str, str, int | None]] = []

    planned: list[tuple[str, str]] = []

    jobs: list[CompileJob] = []

    for source, destination in python_files:
        source_text = os.fspath(source)
        output = pyc_path(source_text)
        compiled_destination = pyc_path(os.fspath(destination))

        if output is None or compiled_destination is None:
            continue

        member = members.get(source_text) if members else None

        if member is not None and tree is not None:
            cached = compiled_parts(mapped_parts(member))

            if (
                cached is not None
                and place_pyc(os.path.join(tree, *cached), source_text, output, magic)
                is not None
            ):
                placed.append(
                    (output, compiled_destination, compiled_destination, None)
                )

                continue

        planned.append((output, compiled_destination))

        jobs.append((source_text, output, os.fspath(destination)))

    compile_modules(jobs)

    return placed + [
        (output, compiled_destination, compiled_destination, None)
        for output, compiled_destination in planned
        if os.path.exists(output)
    ]


def existing_paths(
    distribution: InstalledMetadataDistribution | InstalledWheelDistribution | None,
    target: InstallTarget,
) -> tuple[set[str], set[str]]:
    """The files the installed ``distribution`` owns, which replacing it
    in ``target`` deletes."""
    if distribution is None:
        return set(), set()

    if distribution.info_location and distribution.info_location.endswith(".dist-info"):
        try:
            entries = [
                row[0]
                for row in csv.reader(distribution.read_text("RECORD").splitlines())
                if row and row[0]
            ]

        except FileNotFoundError as exc:
            raise InstallationError(
                f"Cannot replace {distribution.raw_name} {distribution.raw_version}: "
                "no RECORD file was found",
            ) from exc

    elif isinstance(distribution, InstalledWheelDistribution):
        raise InstallationError(
            f"Cannot replace {distribution.raw_name} {distribution.raw_version}: "
            "the installed wheel has no dist-info RECORD",
        )

    else:
        entries = distribution.iter_declared_entries()

    root = os.fspath(distribution.location)

    existing: set[str] = set()

    if distribution.info_location and distribution.info_location.endswith(".dist-info"):
        entries.extend(
            os.path.relpath(
                os.path.join(distribution.info_location, name),
                root,
            )
            for name in ("INSTALLER", "REQUESTED", "direct_url.json", "RECORD")
        )

    resolved_root = os.path.realpath(root)

    base = root

    if distribution.info_location and distribution.info_location.endswith(".egg-info"):
        # installed-files.txt rows are relative to the .egg-info directory,
        # not to the library root RECORD rows are relative to, and need not
        # list the directory itself or every file in it: uninstall removes
        # it whole, as pip's does, and so does replacing it.
        base = os.fspath(distribution.info_location)

        if os.path.isdir(base):
            existing.add(os.path.abspath(base))

    for entry in entries:
        path = os.path.join(base, entry)

        try:
            path_stat = os.lstat(path)

        except OSError:
            continue

        is_link = stat.S_ISLNK(path_stat.st_mode)

        owned = os.path.realpath(path) if is_link else os.path.abspath(path)

        # RECORD is whatever the old wheel shipped, and every path kept here
        # is deleted by the upgrade: an absolute row, a `..` row or a link
        # leading out of the environment is refused as uninstall refuses it.
        if (
            is_link
            or os.path.isabs(entry)
            or ".." in entry.replace("\\", "/").split("/")
        ) and not _inside_distribution(owned, resolved_root, target):
            continue

        existing.add(owned)

    return existing, existing


class InstalledTargetInventory:
    """Installed distributions discovered once for an install transaction."""

    __slots__ = ("distributions",)

    def __init__(
        self,
        distributions: Mapping[
            str,
            InstalledMetadataDistribution | InstalledWheelDistribution,
        ],
    ) -> None:
        self.distributions = dict(distributions)

    @classmethod
    def from_target(
        cls,
        target: InstallTarget,
        names: set[str] | None = None,
    ) -> InstalledTargetInventory:
        lightweight = discover_installed_wheels(target.library_roots, names=names)

        if lightweight is not None:
            return cls(lightweight)

        distributions = InstalledDistributionStore(
            paths=[os.fspath(root) for root in target.library_roots],
        ).iter(names=names)

        return cls(
            {
                distribution.canonical_name: distribution
                for distribution in distributions
            },
        )

    def find(
        self,
        name: str,
    ) -> InstalledMetadataDistribution | InstalledWheelDistribution | None:
        return self.distributions.get(name)
