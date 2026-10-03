"""Transactional wheel installation.

This module is the migration boundary between wheel preparation and the
filesystem transaction engine. It deliberately does not invoke kpip again.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import tempfile
import zipfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Lock
from typing import TYPE_CHECKING, Protocol

from kpip.build.metadata import (
    InstalledDistributionStore,
    InstalledMetadataDistribution,
)
from kpip.core.errors import InstallationError, UnsupportedWheel
from kpip.core.names import canonicalize_name
from kpip.core.wheel import (
    WheelCandidate,
    root_is_purelib_from_text,
    validate_wheel,
    validate_wheel_with_metadata,
    wheel_candidate,
    wheel_candidate_from_path,
)
from kpip.host.clone import _configured_link_mode, clone_path
from kpip.host.lock import environment_write_lock
from kpip.install.target import InstallTarget
from kpip.install.transaction import InstallTransaction, normalized_internal
from kpip.install.wheel_archive import (
    DestinationCache,
    MemberPaths,
    ResolvedRoots,
    copy_member_with_metadata,
    destination_internal_parts_text,
    installed_mode,
    zip_mode,
    record_metadata_internal,
    validate_member_parts,
)
from kpip.install.wheel_archive_cache import (
    INSTALL_WORKERS,
    CachedWheelArchive,
    remember_tree_listings,
)
from kpip.install.wheel_archive_installer import install_wheels_from_archive_cache
from kpip.install.wheel_archive_runtime import CachedWheelInfo, open_wheel_archive
from kpip.install.wheel_scripts import (
    entry_point_scripts,
    generate_entry_point_files,
    rewrite_shebang,
    script_matches,
    warn_about_scripts_not_on_path,
)
from kpip.install.wheel_state import (
    InstalledTargetInventory,
    InstalledWheelDistribution,
    compiled_files,
    existing_paths,
)
from kpip.network.cache import process_umask
from kpip.install.wheel_transaction_direct import (
    DIRECT_CONTENT_BATCH_LIMIT,
    direct_batch_preflight,
    install_wheels_directly,
)


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from kpip.core.direct_url import DirectUrl

    ExistingDistribution = InstalledMetadataDistribution | InstalledWheelDistribution

    class MemberReader(Protocol):
        """The one method the WHEEL re-read needs, of any archive reader.

        Positional-only: the readers spell the parameter differently.
        """

        def read(self, name: str, /) -> bytes: ...


DIRECT_CONTENT_LIMIT = 64 * 1024
StagedEntry = tuple[str, str, str, int | None]


class ThreadSafePathCache:
    """Lock-guarded destination-path cache shared by parallel install workers."""

    def __init__(self) -> None:
        self.values: DestinationCache = {}
        self.lock = Lock()

    def get(self, key: tuple[str, str]) -> str | None:
        with self.lock:
            return self.values.get(key)

    def __setitem__(self, key: tuple[str, str], value: str) -> None:
        with self.lock:
            self.values[key] = value


def _target_has_distribution_metadata(target: InstallTarget) -> bool:
    """Check for installed metadata before importing metadata discovery code."""
    for root in target.library_roots:
        try:
            with os.scandir(root) as entries:
                if any(
                    entry.name.endswith((".dist-info", ".egg-info", ".egg-link"))
                    for entry in entries
                ):
                    return True
        except OSError:
            return True
    return False


class WheelInstaller:
    """Install wheels into one target using filesystem transactions."""

    def __init__(
        self,
        target: InstallTarget,
        *,
        pycompile: bool = True,
        force: bool = False,
        preserve_existing: bool = False,
        script_executable: str | None = None,
        target_inventory: InstalledTargetInventory | None = None,
    ) -> None:
        self.target = target
        self.pycompile = pycompile
        self.force = force
        self.preserve_existing = preserve_existing
        self.script_executable = script_executable
        self.target_inventory = target_inventory

    def install(
        self,
        path: str,
        *,
        candidate: WheelCandidate | None = None,
        requested: bool = False,
        direct_url: DirectUrl | None = None,
        transaction_sink: list[InstallTransaction] | None = None,
        existing: ExistingDistribution | None = None,
        lookup_existing: bool = True,
        validated_dist_info: str | None = None,
        destination_cache: DestinationCache | None = None,
        stage_root: str | None = None,
        transaction: InstallTransaction | None = None,
        direct: bool = False,
    ) -> WheelCandidate:
        return install_wheel_internal(
            path,
            target=self.target,
            candidate=candidate,
            pycompile=self.pycompile,
            requested=requested,
            force=self.force,
            preserve_existing=self.preserve_existing,
            direct_url=direct_url,
            script_executable=self.script_executable,
            transaction_sink=transaction_sink,
            existing=existing,
            lookup_existing=lookup_existing,
            validated_dist_info=validated_dist_info,
            destination_cache=destination_cache,
            stage_root=stage_root,
            transaction=transaction,
            direct=direct,
            target_inventory=self.target_inventory,
        )

    def validate_batch(
        self,
        paths: Iterable[str],
        *,
        validation_cache: dict[str, str] | None = None,
        destination_cache: DestinationCache | None = None,
    ) -> tuple[WheelCandidate, ...]:
        return validate_wheel_batch(
            paths,
            target=self.target,
            validation_cache=validation_cache,
            destination_cache=destination_cache,
        )


def install_wheel_internal(
    path: str,
    *,
    target: InstallTarget,
    candidate: WheelCandidate | None = None,
    pycompile: bool = True,
    requested: bool = False,
    force: bool = False,
    preserve_existing: bool = False,
    direct_url: DirectUrl | None = None,
    script_executable: str | None = None,
    transaction_sink: list[InstallTransaction] | None = None,
    existing: ExistingDistribution | None = None,
    lookup_existing: bool = True,
    validated_dist_info: str | None = None,
    destination_cache: DestinationCache | None = None,
    stage_root: str | None = None,
    transaction: InstallTransaction | None = None,
    direct: bool = False,
    target_inventory: InstalledTargetInventory | None = None,
) -> WheelCandidate:
    if candidate is None:
        candidate = wheel_candidate_from_path(path, include_layout=False)
    target = target.for_distribution(candidate.canonical_name)
    if lookup_existing:
        if target_inventory is not None:
            existing = target_inventory.find(candidate.canonical_name)
        elif _target_has_distribution_metadata(target):
            existing = InstalledDistributionStore(
                paths=[os.fspath(root) for root in target.library_roots],
            ).find(candidate.name)
        else:
            existing = None
    same_version = existing is not None and existing.version == candidate.version
    if same_version and not force and not preserve_existing:
        return candidate

    # A quiet install says nothing of what it replaces, as pip's does not.
    reports_replacement = (
        existing is not None
        and (not same_version or force)
        and not os.environ.get("KPIP_QUIET")
    )
    if reports_replacement:
        logger.info(f"Uninstalling {existing.raw_name}-{existing.raw_version}")
    if direct and transaction is None:
        raise ValueError("direct wheel installation needs a transaction")

    stage_context = (
        nullcontext(target.purelib)
        if direct
        else (
            tempfile.TemporaryDirectory(prefix="kpip-wheel-stage-")
            if stage_root is None
            else nullcontext(stage_root)
        )
    )
    with stage_context as temporary:
        stage_root_text = temporary
        # Set once the wheel's Root-Is-Purelib is known, below.
        library_root = target.purelib
        library_prefix = library_root.rstrip(os.sep) + os.sep

        def record_relative_path(destination_text: str) -> str:
            if destination_text.startswith(library_prefix):
                return destination_text[len(library_prefix) :]
            return os.path.relpath(destination_text, library_root)

        staged: list[StagedEntry] = []
        record_destination: str | None = None
        dist_info: str | None = None
        stage_directories: set[str] = set()
        resolved_directories = (
            destination_cache if destination_cache is not None else {}
        )
        resolved_roots: ResolvedRoots = target.resolved_roots_internal
        record_metadata: dict[str, tuple[str, str]] = {}
        direct_contents: dict[str, bytes] = {}
        direct_metadata: dict[str, tuple[str, str]] = {}
        direct_content_size = 0
        clone_sources: set[str] = set()
        # Staged modules read from the archive cache, by their name in the
        # wheel: the entry holds their bytecode.
        cached_modules: dict[str, str] = {}
        # The destinations of each top-level directory's members, for a
        # wheel installed from the archive cache, and the directories one of
        # whose members is not a plain copy of the cached tree's file.
        tree_members: dict[str, set[str]] = {}
        mixed_tops: set[str] = set()

        def write_direct(
            destination: str,
            contents: bytes,
            mode: int | None = None,
        ) -> None:
            assert transaction is not None
            destination_text = destination
            os.makedirs(os.path.dirname(destination_text), exist_ok=True)
            transaction.record_created(destination)
            with open(destination_text, "wb") as file:
                file.write(contents)
            if mode is not None:
                os.chmod(destination, mode)

        with open_wheel_archive(path, candidate) as archive:
            if validated_dist_info is None:
                layout = getattr(candidate, "wheel_layout", None)
                if isinstance(layout, CachedWheelArchive):
                    validated_dist_info = layout.dist_info
                elif layout is not None:
                    validated_dist_info = layout[0]
                else:
                    validated_dist_info = validate_wheel(
                        archive,  # ty:ignore[invalid-argument-type]
                        os.path.basename(path)[:-4].split("-", 1)[0],
                    )
            root_is_purelib = wheel_root_is_purelib(archive, validated_dist_info)
            # RECORD keys, the dist-info directory and the metadata files kpip
            # writes into it all have to land in the same root as the wheel
            # body, or a platlib wheel on a split scheme ends up with its
            # dist-info in purelib and RECORD rows that escape it.
            library_root = target.purelib if root_is_purelib else target.platlib
            library_prefix = library_root.rstrip(os.sep) + os.sep
            wheel_record_metadata: dict[str, tuple[str, str]] = {}
            try:
                record_text = archive.read(f"{validated_dist_info}/RECORD").decode(
                    "utf-8",
                )
            except KeyError, UnicodeDecodeError:
                pass
            else:
                for row in csv.reader(io.StringIO(record_text)):
                    if (
                        len(row) >= 3
                        and row[1].startswith("sha256=")
                        and row[2].isdigit()
                    ):
                        wheel_record_metadata[row[0]] = (row[1], row[2])
            member_paths = MemberPaths(
                target,
                stage_root_text,
                resolved_directories=resolved_directories,
                resolved_roots=resolved_roots,
                root_is_purelib=root_is_purelib,
            )
            for member in archive.infolist():
                if member.is_dir():
                    continue
                (
                    relative_parts,
                    source_text,
                    destination_text,
                    record_key,
                ) = member_paths.resolve(member.filename)
                relative_name = relative_parts[-1] if relative_parts else ""
                mode = installed_mode(zip_mode(member))  # ty:ignore[invalid-argument-type]
                if relative_parts and relative_parts[0].endswith(".dist-info"):
                    dist_info = relative_parts[0]
                # The wheel's own RECORD and METADATA, not a vendored copy
                # deeper in the tree: debugpy ships a whole dist-info under
                # its package, which is a file like any other.
                own_metadata = (
                    len(relative_parts) == 2
                    and relative_parts[0] == validated_dist_info
                )
                # A wheel's scripts are its .data/scripts members; a package
                # directory that happens to be named scripts holds modules.
                script_member = (
                    len(relative_parts) >= 3
                    and relative_parts[0].endswith(".data")
                    and relative_parts[1] == "scripts"
                )
                is_record = own_metadata and relative_name == "RECORD"
                direct_content = (
                    getattr(member, "source_path", None) is None
                    and not script_member
                    and not is_record
                    and member.file_size <= DIRECT_CONTENT_LIMIT
                    and direct_content_size + member.file_size
                    <= DIRECT_CONTENT_BATCH_LIMIT
                    and (not pycompile or os.path.splitext(relative_name)[1] != ".py")
                    and relative_name != "entry_points.txt"
                )
                # A cached member is cloned from the cache's tree, so nothing
                # is staged for it but a .py copied for bytecode.
                cached_clone = (
                    isinstance(member, CachedWheelInfo)
                    and not script_member
                    and not is_record
                    and relative_name != "entry_points.txt"
                    and (not pycompile or os.path.splitext(relative_name)[1] != ".py")
                )
                if not direct and not direct_content and not cached_clone:
                    source_parent_text = os.path.dirname(source_text)
                    if source_parent_text not in stage_directories:
                        os.makedirs(source_parent_text, exist_ok=True)
                        stage_directories.add(source_parent_text)
                if script_member:
                    contents = archive.read(member)  # ty:ignore[invalid-argument-type]
                elif is_record:
                    contents = None
                elif direct_content:
                    contents = archive.read(member)  # ty:ignore[invalid-argument-type]
                    direct_content_size += len(contents)
                else:
                    metadata = wheel_record_metadata.get(record_key)
                    if metadata is not None and metadata[1] != str(member.file_size):
                        metadata = None
                    if direct:
                        assert transaction is not None
                        os.makedirs(os.path.dirname(destination_text), exist_ok=True)
                        transaction.record_created(destination_text)
                    cached_member = (
                        member if isinstance(member, CachedWheelInfo) else None
                    )
                    if (
                        cached_member is not None
                        and not direct
                        and relative_name != "entry_points.txt"
                        and (
                            not pycompile or os.path.splitext(relative_name)[1] != ".py"
                        )
                    ):
                        source_text = cached_member.source_path
                        clone_sources.add(source_text)
                        metadata = cached_member.record_metadata
                    elif cached_member is not None and not direct:
                        clone_path(cached_member.source_path, source_text)
                        metadata = cached_member.record_metadata
                    else:
                        metadata = copy_member_with_metadata(
                            archive,  # ty:ignore[invalid-argument-type]
                            member,  # ty:ignore[invalid-argument-type]
                            destination_text if direct else source_text,
                            metadata=metadata,
                        )
                    if direct:
                        direct_metadata[destination_text] = metadata
                    else:
                        record_metadata[source_text] = metadata
                    contents = None
                if direct and contents is not None and not direct_content:
                    write_direct(destination_text, contents, mode)
                if contents is not None and not direct_content and not direct:
                    with open(source_text, "wb") as file:
                        file.write(contents)
                if script_member:
                    if direct:
                        raise InstallationError(
                            "direct wheel installation cannot contain scripts",
                        )
                    rewrite_shebang(source_text, script_executable)
                elif contents is not None:
                    metadata = wheel_record_metadata.get(record_key)
                    if metadata is None or metadata[1] != str(len(contents)):
                        metadata = record_metadata_internal(contents)
                    if direct_content:
                        if direct:
                            write_direct(destination_text, contents, mode)
                        else:
                            direct_contents[destination_text] = contents
                        direct_metadata[destination_text] = metadata
                    elif direct:
                        direct_metadata[destination_text] = metadata
                    else:
                        record_metadata[source_text] = metadata
                top, separator, _ = member.filename.partition("/")
                if (
                    separator
                    and not direct
                    and not top.endswith((".data", ".dist-info"))
                    and top not in mixed_tops
                ):
                    if contents is None and isinstance(member, CachedWheelInfo):
                        tree_members.setdefault(top, set()).add(destination_text)
                    else:
                        mixed_tops.add(top)
                        tree_members.pop(top, None)
                if (
                    pycompile
                    and isinstance(member, CachedWheelInfo)
                    and relative_name.endswith(".py")
                ):
                    cached_modules[source_text] = member.filename
                staged.append((source_text, destination_text, destination_text, mode))
                if is_record:
                    record_destination = destination_text

        if dist_info is None or record_destination is None:
            raise InstallationError(f"Wheel {path} has no valid dist-info metadata")
        record_destination_text = record_destination

        managed_metadata = {
            os.path.join(library_root, dist_info, "INSTALLER"),
            os.path.join(library_root, dist_info, "REQUESTED"),
            os.path.join(library_root, dist_info, "direct_url.json"),
        }
        staged = [
            item
            for item in staged
            if item[2] not in managed_metadata or item[2] == record_destination_text
        ]
        staged_destinations = {destination_text for _, _, destination_text, _ in staged}
        for destination in tuple(direct_contents):
            if destination not in staged_destinations:
                direct_contents.pop(destination, None)
                direct_metadata.pop(destination, None)

        dist_info_stage = os.path.join(stage_root_text, dist_info)
        installer_source = os.path.join(dist_info_stage, "INSTALLER")
        installer_destination = os.path.join(library_root, dist_info, "INSTALLER")
        installer_contents = b"kpip\n"
        if direct:
            write_direct(installer_destination, installer_contents)
        else:
            direct_contents[installer_destination] = installer_contents
        direct_metadata[installer_destination] = record_metadata_internal(
            b"kpip\n",
        )
        staged.append(
            (
                installer_source,
                installer_destination,
                installer_destination,
                None,
            ),
        )

        requested_destination = os.path.join(library_root, dist_info, "REQUESTED")
        if requested:
            requested_source = os.path.join(dist_info_stage, "REQUESTED")
            if direct:
                write_direct(requested_destination, b"")
            else:
                direct_contents[requested_destination] = b""
            direct_metadata[requested_destination] = record_metadata_internal(b"")
            staged.append(
                (
                    requested_source,
                    requested_destination,
                    requested_destination,
                    None,
                ),
            )

        if direct_url is not None:
            direct_url_source = os.path.join(dist_info_stage, "direct_url.json")
            os.makedirs(dist_info_stage, exist_ok=True)
            with open(direct_url_source, "w", encoding="utf-8") as file:
                file.write(direct_url.to_json())
            staged.append(
                (
                    direct_url_source,
                    os.path.join(library_root, dist_info, "direct_url.json"),
                    os.path.join(library_root, dist_info, "direct_url.json"),
                    None,
                ),
            )

        scripts = (
            {}
            if direct
            else entry_point_scripts(
                os.path.join(stage_root_text, dist_info, "entry_points.txt"),
            )
        )
        if scripts:
            script_destinations = {
                os.path.join(target.scripts, generated)
                for name in scripts
                for generated in (name, f"{name}-script.py", f"{name}.exe")
            }
            staged = [item for item in staged if item[1] not in script_destinations]
        script_stage = os.path.join(stage_root_text, ".kpip-scripts")
        script_modes = dict(
            generate_entry_point_files(scripts, script_stage, script_executable),
        )

        if scripts:
            with os.scandir(script_stage) as entries:
                script_sources = tuple(entries)
            for entry in script_sources:
                source = os.path.join(script_stage, entry.name)
                destination = os.path.join(target.scripts, os.path.basename(source))
                mode = script_modes.get(source)
                if mode is None:
                    mode = os.stat(source).st_mode
                staged.append(
                    (
                        source,
                        destination,
                        destination,
                        mode,
                    ),
                )

        if pycompile:
            layout = getattr(candidate, "wheel_layout", None)
            compiled = compiled_files(
                stage_root_text,
                staged,
                archive=layout if isinstance(layout, CachedWheelArchive) else None,
                members=cached_modules,
            )
            staged.extend(compiled)
            if direct:
                assert transaction is not None
                for _, _, destination_text, _ in compiled:
                    transaction.record_created(destination_text)

        record_rows = []
        for source, destination, destination_text, _ in staged:
            if destination_text == record_destination_text:
                record_rows.append((record_relative_path(destination_text), "", ""))
                continue
            metadata = direct_metadata.get(destination_text)
            if metadata is None:
                metadata = record_metadata.get(source)
            if metadata is None:
                with open(source, "rb") as file:
                    metadata = record_metadata_internal(file.read())
            record_rows.append(
                (
                    record_relative_path(destination_text),
                    metadata[0],
                    metadata[1],
                ),
            )
        record_rows.sort()
        record_file = io.StringIO(newline="")
        csv.writer(record_file).writerows(record_rows)
        record_contents = record_file.getvalue().encode("utf-8")
        if direct:
            write_direct(record_destination, record_contents)
        else:
            direct_contents[record_destination] = record_contents
        direct_metadata[record_destination] = record_metadata_internal(
            record_contents,
        )

        owned_paths, old_paths = existing_paths(existing, target)
        if preserve_existing and existing is not None:
            old_paths = set()
        old_path_texts = set(old_paths)
        scripts_text = os.fspath(target.scripts)
        for _, destination, destination_text, _ in staged:
            if (
                not direct and os.path.dirname(destination_text) == scripts_text
            ) and script_matches(destination, scripts):
                owned_paths.add(destination_text)
        new_destinations = {destination_text for _, _, destination_text, _ in staged}
        active_transaction = transaction or InstallTransaction(owned_paths=owned_paths)
        if direct and active_transaction is not transaction:
            raise ValueError("direct wheel installation needs the shared transaction")
        if transaction is not None:
            transaction.owned.update(normalized_internal(path) for path in owned_paths)
        if not direct:
            layout = getattr(candidate, "wheel_layout", None)
            if isinstance(layout, CachedWheelArchive):
                if tree_members and _configured_link_mode() == "hardlink":
                    remember_tree_listings(layout)
                for top, members in tree_members.items():
                    destination_root = os.path.join(library_root, top)
                    if members and all(
                        destination.startswith(destination_root + os.sep)
                        for destination in members
                    ):
                        active_transaction.add_tree(
                            os.path.join(layout.tree, top), destination_root, members
                        )
            for source, destination, destination_text, mode in staged:
                contents = direct_contents.get(destination_text)
                if contents is not None:
                    active_transaction.add_contents(
                        destination_text,
                        contents,
                        mode=mode,
                    )
                else:
                    operation = (
                        active_transaction.add_clone
                        if source in clone_sources
                        else active_transaction.add
                    )
                    operation(source, destination_text, mode=mode)
            for old_path in old_path_texts - new_destinations:
                active_transaction.delete(old_path)
            if transaction is None:
                active_transaction.commit(finalize=transaction_sink is None)
        if transaction_sink is not None and transaction is None:
            transaction_sink.append(active_transaction)
        if reports_replacement:
            logger.info(
                f"Successfully uninstalled {existing.raw_name}-{existing.raw_version}",
            )
    return candidate


def root_is_purelib_or_default(text: str) -> bool:
    """``Root-Is-Purelib`` from WHEEL text, defaulting to purelib.

    The field is required, but a wheel missing it has already passed
    validation by the time either caller asks, and refusing it there would
    reject an install over metadata neither path had to read. Both callers
    take the same lenient answer so they cannot disagree about where a wheel
    belongs.
    """
    try:
        return root_is_purelib_from_text(text)
    except UnsupportedWheel:
        return True


def wheel_root_is_purelib(archive: MemberReader, dist_info: str) -> bool:
    """``Root-Is-Purelib`` for an already-validated wheel.

    The WHEEL file has been read once by validation, but only its version was
    kept; re-reading one small member is cheaper than threading the text
    through every layout cache.
    """
    try:
        raw = archive.read(f"{dist_info}/WHEEL")
    except KeyError, OSError:
        return True
    try:
        return root_is_purelib_or_default(raw.decode())
    except UnicodeDecodeError:
        return True


def validate_wheel_batch(
    paths: Iterable[str],
    *,
    target: InstallTarget,
    validation_cache: dict[str, str] | None = None,
    destination_cache: DestinationCache | None = None,
) -> tuple[WheelCandidate, ...]:
    """Validate a wheel batch before any member of the batch is installed."""
    candidates: list[WheelCandidate] = []
    destinations: set[str] = set()
    resolved_roots: ResolvedRoots = target.resolved_roots_internal
    resolved_directories = destination_cache if destination_cache is not None else {}
    for path in paths:
        with (
            open(path, "rb", buffering=32768) as stream,
            zipfile.ZipFile(stream) as archive,
        ):
            dist_info, wheel_metadata_text = validate_wheel_with_metadata(
                archive,
                os.path.basename(path)[:-4].split("-", 1)[0],
            )
            candidate = wheel_candidate(
                path,
                archive=archive,
                dist_info_dir=dist_info,
                wheel_metadata_text=wheel_metadata_text,
                include_layout=False,
            )
            candidates.append(candidate)
            wheel_target = target.for_distribution(candidate.canonical_name)
            if validation_cache is not None:
                validation_cache[path] = dist_info
            root_is_purelib = root_is_purelib_or_default(wheel_metadata_text)
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative_parts = validate_member_parts(member.filename)
                destination = destination_internal_parts_text(
                    wheel_target,
                    relative_parts,
                    member.filename,
                    resolved_directories=resolved_directories,
                    resolved_roots=resolved_roots,
                    root_is_purelib=root_is_purelib,
                )
                destination_text = destination
                if destination_text in destinations:
                    raise InstallationError(
                        f"Cannot install {canonicalize_name(candidate.name)}: "
                        "multiple wheels target "
                        f"the same path: {destination}",
                    )
                destinations.add(destination_text)
    return tuple(candidates)


_ARCHIVE_SOURCE_KINDS = frozenset({None, "wheel", "sdist"})
"""What a candidate's wheel may have come from to install from the archive cache."""


def install_wheels_transactionally(
    items: Iterable[tuple[str, bool, DirectUrl | None]],
    *,
    target: InstallTarget,
    pycompile: bool = True,
    force: bool = False,
    preserve_existing: bool = False,
    script_executable: str | None = None,
    lookup_existing: bool = True,
    candidates: Iterable[WheelCandidate] | None = None,
    cache_dir: str | None = None,
    warn_script_location: bool = False,
) -> tuple[WheelCandidate, ...]:
    """Install a wheel batch with rollback across every wheel in the batch.

    ``warn_script_location`` warns, as pip does, about console scripts
    installed outside ``PATH``.

    The target-mutating phase -- the existing-state scan included -- runs
    under an advisory lock on the target, so concurrent kpip processes
    driving one environment serialize instead of interleaving file writes.
    Candidate planning stays outside the lock: parsing each wheel's metadata
    touches nothing in the target and would otherwise serialize too.
    """

    # Read before any worker thread writes a file: without /proc the umask
    # is read by setting it, which a file created meanwhile would get.
    process_umask()
    requests = tuple(items)
    planned_candidates = (
        tuple(candidates)
        if candidates is not None
        else tuple(
            wheel_candidate_from_path(path, include_layout=False)
            for path, _, _ in requests
        )
    )
    if len(planned_candidates) != len(requests):
        raise ValueError("candidate count does not match wheel request count")

    with environment_write_lock(target.purelib):
        installed = _install_wheels_locked(
            requests,
            planned_candidates,
            target=target,
            pycompile=pycompile,
            force=force,
            preserve_existing=preserve_existing,
            script_executable=script_executable,
            lookup_existing=lookup_existing,
            cache_dir=cache_dir,
        )
    if warn_script_location:
        warn_about_scripts_not_on_path(
            [path for path, _, _ in requests], target.scripts, script_executable
        )
    return installed


def _install_wheels_locked(
    requests: tuple[tuple[str, bool, DirectUrl | None], ...],
    planned_candidates: tuple[WheelCandidate, ...],
    *,
    target: InstallTarget,
    pycompile: bool,
    force: bool,
    preserve_existing: bool,
    script_executable: str | None,
    lookup_existing: bool,
    cache_dir: str | None,
) -> tuple[WheelCandidate, ...]:
    installer = WheelInstaller(
        target,
        pycompile=pycompile,
        force=force,
        preserve_existing=preserve_existing,
        script_executable=script_executable,
    )
    destination_cache: DestinationCache = {}
    # A wheel built from a source archive is a wheel like any other by now,
    # hashed from its own bytes (``supplied_wheel_digest`` trusts only a
    # wheel's index hash): one sdist in a batch used to send all of it down
    # the staging route, 0.4 s of a 0.6 s warm trio install. A source tree
    # -- a directory or a checkout -- still goes that way.
    if cache_dir is not None and all(
        candidate.source_kind in _ARCHIVE_SOURCE_KINDS
        for candidate in planned_candidates
    ):
        cached_result = install_wheels_from_archive_cache(
            requests,
            planned_candidates,
            target=target,
            cache_dir=cache_dir,
            script_executable=script_executable,
            force=force,
            preserve_existing=preserve_existing,
            pycompile=pycompile,
        )
        if cached_result is not None:
            return cached_result
    target_inventory = (
        InstalledTargetInventory.from_target(
            target,
            names={candidate.canonical_name for candidate in planned_candidates},
        )
        if lookup_existing
        else None
    )
    existing_distributions = (
        {} if target_inventory is None else target_inventory.distributions
    )
    installer.target_inventory = target_inventory
    direct_destination_cache = None
    if not force and not existing_distributions:
        direct_destination_cache = direct_batch_preflight(
            requests,
            planned_candidates,
            target=target,
            pycompile=pycompile,
        )
    if direct_destination_cache is not None:
        return install_wheels_directly(
            requests,
            planned_candidates,
            target=target,
            pycompile=pycompile,
            installer=installer,
            destination_cache=direct_destination_cache,
        )
    with InstallTransaction() as transaction:
        with tempfile.TemporaryDirectory(prefix="kpip-wheel-batch-") as temporary:
            batch_stage = temporary
            parallel = (
                len(requests) >= 4
                and len(requests) <= 64
                and not existing_distributions
            )
            cache_for_workers = destination_cache
            if parallel:
                cache_for_workers = ThreadSafePathCache()

            def install_one(
                index: int,
                request: tuple[str, bool, DirectUrl | None],
                candidate: WheelCandidate,
            ) -> tuple[int, InstallTransaction, WheelCandidate]:
                local_transaction = InstallTransaction()
                local_transaction.owner = candidate.canonical_name
                try:
                    result = installer.install(
                        request[0],
                        candidate=candidate,
                        requested=request[1],
                        direct_url=request[2],
                        existing=None,
                        lookup_existing=False,
                        destination_cache=cache_for_workers,  # ty:ignore[invalid-argument-type]
                        stage_root=os.path.join(batch_stage, str(index)),
                        transaction=local_transaction,
                    )
                except Exception:
                    local_transaction.rollback()
                    raise
                return index, local_transaction, result

            try:
                if parallel:
                    futures = []
                    staged_results = []
                    try:
                        with ThreadPoolExecutor(
                            max_workers=min(INSTALL_WORKERS, len(requests)),
                        ) as pool:
                            futures = [
                                pool.submit(install_one, index, request, candidate)
                                for index, (request, candidate) in enumerate(
                                    zip(requests, planned_candidates),
                                )
                            ]
                            staged_results = [future.result() for future in futures]
                        ordered_results = sorted(
                            staged_results,
                            key=lambda item: item[0],
                        )
                        for _, local_transaction, _ in ordered_results:
                            transaction.adopt(local_transaction)
                        for _, local_transaction, _ in ordered_results:
                            local_transaction.finalize()
                    except Exception:
                        for future in futures:
                            if not future.done() or future.cancelled():
                                continue
                            try:
                                _, local_transaction, _ = future.result()
                            except Exception:
                                continue
                            local_transaction.rollback()
                        raise
                    candidates = tuple(result for _, _, result in ordered_results)
                else:
                    installed: list[WheelCandidate] = []
                    for index, (
                        (path, requested, direct_url),
                        candidate,
                    ) in enumerate(zip(requests, planned_candidates)):
                        transaction.owner = candidate.canonical_name
                        installed.append(
                            installer.install(
                                path,
                                candidate=candidate,
                                requested=requested,
                                direct_url=direct_url,
                                existing=existing_distributions.get(
                                    candidate.canonical_name,
                                ),
                                lookup_existing=False,
                                destination_cache=destination_cache,
                                stage_root=os.path.join(batch_stage, str(index)),
                                transaction=transaction,
                            )
                        )
                    candidates = tuple(installed)
            except Exception:
                transaction.rollback()
                raise
            transaction.commit()
    return candidates
