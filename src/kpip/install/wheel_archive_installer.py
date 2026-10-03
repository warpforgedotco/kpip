"""Archive-backed batch installer: clone cached wheel trees into a target.

Consumes the immutable, validated trees the archive cache
(:mod:`kpip.install.wheel_archive_cache`) already extracted and unpacked, and
performs the batch clone/relocate/finalize/atomic-swap sequence that installs
them into a real target directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import csv
import errno
import io
import logging
import os
import shutil
import tempfile
from collections.abc import Set as AbstractSet
from concurrent.futures import ThreadPoolExecutor

from kpip.build.metadata import InstalledDistributionStore
from kpip.core.errors import InstallationError
from kpip.host.clone import clone_path
from kpip.install.bytecode import CompileJob, compile_modules, place_pyc, target_magic
from kpip.install.wheel_archive import (
    compiled_parts,
    mapped_parts,
    record_metadata_internal,
    validate_member_parts,
)
from kpip.install.wheel_archive_cache import (
    INSTALL_WORKERS,
    prepare_cached_wheels,
    bytecode_tree,
    remember_tree_listings,
)
from kpip.install.wheel_scripts import (
    generate_entry_point_files,
    rewrite_shebang,
)
from kpip.install.wheel_state import discover_installed_wheels, existing_paths

if TYPE_CHECKING:
    from kpip.build.metadata import InstalledMetadataDistribution
    from kpip.core.direct_url import DirectUrl
    from kpip.install.target import InstallTarget
    from kpip.install.wheel_archive_cache import (
        CachedWheelArchive,
        InstallCandidate,
        WheelInstallCandidate,
        WheelRequest,
    )
    from kpip.install.wheel_state import InstalledWheelDistribution

logger = logging.getLogger(__name__)


_CLONE_WORKERS = min(INSTALL_WORKERS, 4)
"""Threads linking a batch's trees into place. Each makes a syscall per file
or directory and takes the interpreter lock back after it, so more of them
trade the lock rather than link faster: a warm trio install takes 0.21 s
with 2 to 4 and 0.29 s with the machine's 20."""


class _WheelInstallPlan:
    __slots__ = (
        "archive",
        "candidate",
        "direct_url",
        "loses",
        "requested",
        "scripts",
        "wins",
    )

    def __init__(
        self,
        archive: CachedWheelArchive,
        candidate: WheelInstallCandidate,
        *,
        requested: bool,
        direct_url: DirectUrl | None,
        scripts: dict[str, tuple[str, bool]],
    ) -> None:
        self.archive = archive

        self.candidate = candidate

        self.requested = requested

        self.direct_url = direct_url

        self.scripts = scripts

        # Archive members another wheel of the batch also installs: the ones
        # a later wheel's copy replaces, and the ones this copy replaces.
        self.loses: set[str] = set()

        self.wins: set[str] = set()


class _DestinationNode:
    """Typed prefix tree for detecting colliding wheel destinations."""

    __slots__ = ("children", "member", "owner")

    def __init__(self) -> None:
        self.children: dict[str, _DestinationNode] = {}

        self.owner: int | None = None

        # The archive member the owner installs here, when it is one.
        self.member: str | None = None


def _normalized_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.realpath(path)))


def _internal_comparison_path(path: str) -> str:
    """Normalize a validated target path without resolving every parent."""

    return os.path.normcase(os.path.normpath(path))


def _eligible_target(target: InstallTarget, cache_dir: str) -> str | None:
    root = _normalized_path(target.purelib)

    if os.path.lexists(root) and (not os.path.isdir(root) or os.path.islink(root)):
        return None

    if any(
        _normalized_path(path) != root
        for path in (target.platlib, target.headers, target.data)
    ):
        return None

    expected_scripts = _normalized_path(
        os.path.join(root, "Scripts" if os.name == "nt" else "bin"),
    )

    if _normalized_path(target.scripts) != expected_scripts:
        return None

    cache = _normalized_path(cache_dir)

    try:
        if os.path.commonpath((cache, root)) == root:
            return None

    except ValueError:
        pass

    return root


def _normalized_destination(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(os.path.normcase(part) for part in parts)


class _SharedDestination(Exception):
    """Two wheels of a batch install the same file where this path cannot
    leave the later one's copy: a console script, or a member of a wheel's
    ``.data`` directory, which lands only after the trees are cloned. The
    batch goes to the transactional installer, which orders them."""


def _reserve_destination(
    trie: _DestinationNode,
    parts: tuple[str, ...],
    owner: int,
    candidate: WheelInstallCandidate,
    *,
    allow_same_owner: bool = False,
    member: str | None = None,
) -> tuple[int, str | None] | None:
    """Claim ``parts`` for ``owner``.

    A regular file another wheel of the batch already claimed goes to this
    one, as pip leaves the later wheel's copy: the earlier owner and the
    member it would have installed are returned.
    """
    node = trie

    normalized = _normalized_destination(parts)

    for part in normalized:
        if node.owner is not None:
            raise InstallationError(
                f"Cannot install {candidate.canonical_name}: "
                f"duplicate installation destination: {'/'.join(parts)}",
            )

        child = node.children.get(part)

        if child is None:
            child = _DestinationNode()

            node.children[part] = child

        node = child

    terminal = node.owner

    has_children = bool(node.children)

    if terminal is not None and terminal != owner and not has_children:
        if member is None or node.member is None:
            raise _SharedDestination

        earlier = (terminal, node.member)

        node.owner = owner

        node.member = member

        return earlier

    if (
        terminal is not None and not (allow_same_owner and terminal == owner)
    ) or has_children:
        raise InstallationError(
            f"Cannot install {candidate.canonical_name}: "
            f"duplicate installation destination: {'/'.join(parts)}",
        )

    node.owner = owner

    node.member = member

    return None


def _top_destinations(
    archive: CachedWheelArchive, *, pycompile: bool
) -> tuple[list[tuple[str, str | None]], set[str]]:
    """The top-level destination of each of ``archive``'s members, and of
    its ``.pyc`` when that lands elsewhere; and the set of them all.

    Only a top-level module's bytecode leaves its top-level name, for
    ``__pycache__`` beside it.
    """
    pycache = os.path.normcase("__pycache__")

    tops: list[tuple[str, str | None]] = []

    for entry in archive.entries:
        relative = entry[0]

        top, separator, _ = relative.partition("/")

        if top.endswith(".data"):
            mapped = mapped_parts(relative)
            top = mapped[0]
            at_top_level = len(mapped) == 1
        else:
            at_top_level = not separator

        compiled = (
            pycache if pycompile and at_top_level and top.endswith(".py") else None
        )

        tops.append((os.path.normcase(top), compiled))

    names = {top for top, _ in tops}

    names.update(compiled for _, compiled in tops if compiled is not None)

    return tops, names


def _build_plans(
    requests: tuple[WheelRequest, ...],
    candidates: tuple[WheelInstallCandidate, ...],
    archives: tuple[CachedWheelArchive, ...],
    *,
    pycompile: bool = False,
) -> tuple[_WheelInstallPlan, ...]:
    trie = _DestinationNode()

    plans: list[_WheelInstallPlan] = []

    # (earlier plan, later owner, member): the later owner's plan is made
    # after its members are claimed.
    shared: list[tuple[_WheelInstallPlan, int, str]] = []

    summaries = [archive.summary for archive in archives]

    scripts_by_owner = [
        {name: (target, gui) for name, target, gui in summary[4]}
        for summary in summaries
    ]

    # Two wheels can only install the same path under a top-level name they
    # both use -- "bin", "__pycache__", a namespace package. Under any other,
    # a wheel's members are its own, and claiming them one by one, 12,000 for
    # a jupyter install, only found that out: they are claimed only under a
    # name shared. A wheel can still collide with itself, listing a member
    # twice or, compiled, shipping the bytecode compiling would write: such a
    # one is claimed whole.
    pycache = os.path.normcase("__pycache__")

    names_by_owner = [
        {*summary[0], pycache} if pycompile and summary[1] else set(summary[0])
        for summary in summaries
    ]

    owners_by_top: dict[str, int] = {}

    scripts_top = os.path.normcase("Scripts" if os.name == "nt" else "bin")

    for owner, names in enumerate(names_by_owner):
        if scripts_by_owner[owner]:
            names = names | {scripts_top}

        for name in names:
            owners_by_top[name] = owners_by_top.get(name, 0) + 1

    shared_tops = {name for name, owners in owners_by_top.items() if owners > 1}

    for owner, (request, candidate, archive) in enumerate(
        zip(requests, candidates, archives, strict=True),
    ):
        summary = summaries[owner]

        whole = summary[2] or (pycompile and summary[3])

        if whole or not names_by_owner[owner].isdisjoint(shared_tops):
            entry_tops = _top_destinations(archive, pycompile=pycompile)[0]
            members = zip(archive.entries, entry_tops, strict=True)
        else:
            members = iter(())

        for entry, (top, compiled_top) in members:
            if not whole and top not in shared_tops and compiled_top not in shared_tops:
                continue

            relative = entry[0]

            mapped = mapped_parts(relative)

            # A .data member lands by a move after the trees are cloned, which
            # cannot give way to another wheel's copy.
            clonable = not relative.partition("/")[0].endswith(".data")

            earlier = _reserve_destination(
                trie,
                mapped,
                owner,
                candidate,
                member=relative if clonable else None,
            )

            if earlier is not None:
                earlier_owner, earlier_member = earlier

                assert earlier_member is not None

                earlier_plan = plans[earlier_owner]

                if earlier_plan.candidate.canonical_name == candidate.canonical_name:
                    # One distribution twice over is not two sharing a file.
                    raise InstallationError(
                        f"Cannot install {candidate.canonical_name}: "
                        f"duplicate installation destination: {'/'.join(mapped)}",
                    )

                earlier_plan.loses.add(earlier_member)

                shared.append((earlier_plan, owner, relative))

            if pycompile and (compiled := compiled_parts(mapped)) is not None:
                # Bytecode for a shared module is compiled from the copy left,
                # by the wheel that left it.
                _reserve_destination(
                    trie,
                    compiled,
                    owner,
                    candidate,
                    member=relative if clonable else None,
                )

        scripts = scripts_by_owner[owner]

        for name in scripts:
            if os.path.basename(name) != name or name in {".", ".."}:
                raise InstallationError(
                    f"console script {name!r} is outside the scripts directory",
                )

            for generated in (name, f"{name}-script.py", f"{name}.exe"):
                _reserve_destination(
                    trie,
                    ("Scripts" if os.name == "nt" else "bin", generated),
                    owner,
                    candidate,
                    allow_same_owner=True,
                )

        plans.append(
            _WheelInstallPlan(
                archive,
                candidate,
                requested=request[1],
                direct_url=request[2],
                scripts=scripts,
            ),
        )

    for earlier_plan, later_owner, member in shared:
        later_plan = plans[later_owner]

        later_plan.wins.add(member)

        logger.warning(
            "%s and %s both install %s; installing %s's copy",
            earlier_plan.candidate.canonical_name,
            later_plan.candidate.canonical_name,
            member,
            later_plan.candidate.canonical_name,
        )

    return tuple(plans)


def _stage_paths(stage: str, members: set[str]) -> frozenset[str]:
    """Where archive ``members`` land in ``stage``, as the clone spells them."""
    return frozenset(os.path.join(stage, *member.split("/")) for member in members)


def _merge_move(source: str, destination: str) -> None:
    if not os.path.lexists(source):
        return

    if not os.path.lexists(destination):
        os.rename(source, destination)

        return

    if not (
        os.path.isdir(source)
        and not os.path.islink(source)
        and os.path.isdir(destination)
        and not os.path.islink(destination)
    ):
        raise FileExistsError(destination)

    with os.scandir(source) as entries:
        names = tuple(entry.name for entry in entries)

    for name in names:
        _merge_move(
            os.path.join(source, name),
            os.path.join(destination, name),
        )

    os.rmdir(source)


def _relocate_data(stage: str, archive: CachedWheelArchive) -> None:
    data_roots = [name for name in os.listdir(archive.tree) if name.endswith(".data")]

    for data_root in data_roots:
        root = os.path.join(stage, data_root)

        for scheme in ("purelib", "platlib", "data", "headers", "scripts"):
            source = os.path.join(root, scheme)

            destination = (
                os.path.join(stage, "Scripts" if os.name == "nt" else "bin")
                if scheme == "scripts"
                else stage
            )

            _merge_move(source, destination)

        if os.path.lexists(root):
            shutil.rmtree(root)


def _write_new_file(path: str, contents: bytes) -> tuple[str, str]:
    """Write ``contents`` as a new regular file; returns its RECORD row's
    hash and size, computed from the bytes in hand rather than read back.

    Created exclusively, in the one ``open`` it usually takes: what is in the
    way -- a file of the wheel's, a directory, a link -- is removed, and a
    missing directory made, only when the first attempt finds one.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)

    try:
        descriptor = os.open(path, flags, 0o666)

    except FileExistsError:
        _remove_existing(path)
        descriptor = os.open(path, flags, 0o666)

    except FileNotFoundError:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        descriptor = os.open(path, flags, 0o666)

    with open(descriptor, "wb") as file:
        file.write(contents)

    return record_metadata_internal(contents)


def _remove_existing(path: str) -> None:
    """Remove what is at ``path``, a directory whole; nothing there is fine."""
    try:
        os.unlink(path)

    except FileNotFoundError:
        pass

    except IsADirectoryError, PermissionError:
        # Unlinking a directory fails with EISDIR on Linux, EPERM elsewhere.
        if not os.path.isdir(path) or os.path.islink(path):
            raise

        shutil.rmtree(path)


def _file_metadata(path: str) -> tuple[str, str]:
    with open(path, "rb") as file:
        return record_metadata_internal(file.read())


def _materialize_pyc(
    stage: str,
    install_root: str,
    archive: CachedWheelArchive,
    *,
    skip: AbstractSet[str] = frozenset(),
) -> list[tuple[str, str, str]]:
    """Place the wheel's ``.pyc`` files in the staged tree and return their
    RECORD rows, the way the transactional route records them.

    Members in ``skip`` are another wheel's copy in the stage, compiled by
    that wheel: this one's cached bytecode is of its own copy.

    The archive cache holds the target interpreter's bytecode, so each is a
    copy with its header renamed to the staged source (:func:`place_pyc`).
    Members it has none for are compiled in the stage, unless the cache has
    a tree and they are missing from it: they would not compile.
    """

    tree = bytecode_tree(archive)

    magic = target_magic() if tree is not None else b""

    rows: list[tuple[str, str, str]] = []

    uncached: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []

    for relative, _, _, _ in archive.entries:
        if relative in skip:
            continue

        mapped = mapped_parts(relative)

        target = compiled_parts(mapped)

        if target is None:
            continue

        source = os.path.join(stage, *mapped)

        cached = None if tree is None else os.path.join(tree, *target)

        body = (
            None
            if cached is None
            else place_pyc(cached, source, os.path.join(stage, *target), magic)
        )

        if body is None:
            if cached is None or os.path.exists(cached):
                uncached.append((source, mapped, target))

            continue

        rows.append(("/".join(target), *record_metadata_internal(body)))

    rows.extend(_compile_uncached(stage, install_root, uncached))

    return rows


def _compile_uncached(
    stage: str,
    install_root: str,
    members: list[tuple[str, tuple[str, ...], tuple[str, ...]]],
) -> list[tuple[str, str, str]]:
    """Compile members the archive cache had no ``.pyc`` for, in the stage,
    naming where each module will live."""
    if not members:
        return []

    outputs: list[tuple[str, tuple[str, ...]]] = []

    jobs: list[CompileJob] = []

    for source, mapped, target in members:
        # py_compile makes the output's directory.
        output = os.path.join(stage, *target)

        outputs.append((output, target))

        jobs.append((source, output, os.path.join(install_root, *mapped)))

    compile_modules(jobs)

    return [
        ("/".join(target), *_file_metadata(output))
        for output, target in outputs
        if os.path.exists(output)
    ]


def _finalize_wheel(
    stage: str,
    plan: _WheelInstallPlan,
    *,
    install_root: str,
    script_executable: str | None,
    pycompile: bool = False,
) -> None:
    archive = plan.archive

    dist_info = archive.dist_info

    dist_info_root = os.path.join(stage, dist_info)

    script_members: set[str] = set()

    for relative, _, _, _ in archive.entries:
        if "/scripts/" not in relative:
            continue

        parts = validate_member_parts(relative)

        if len(parts) >= 3 and parts[0].endswith(".data") and parts[1] == "scripts":
            mapped = mapped_parts(relative)

            path = os.path.join(stage, "/".join(mapped))

            rewrite_shebang(path, script_executable)

            script_members.add("/".join(mapped))

    installer = os.path.join(dist_info_root, "INSTALLER")

    requested = os.path.join(dist_info_root, "REQUESTED")

    direct_url = os.path.join(dist_info_root, "direct_url.json")

    installer_metadata = _write_new_file(installer, b"kpip\n")

    requested_metadata = _write_new_file(requested, b"") if plan.requested else None

    if not plan.requested:
        try:
            os.unlink(requested)

        except FileNotFoundError:
            pass

    direct_url_metadata = (
        _write_new_file(direct_url, plan.direct_url.to_json().encode("utf-8"))
        if plan.direct_url is not None
        else None
    )

    if plan.direct_url is None:
        try:
            os.unlink(direct_url)

        except FileNotFoundError:
            pass

    generated_names = {
        generated
        for name in plan.scripts
        for generated in (name, f"{name}-script.py", f"{name}.exe")
    }

    scripts_root = os.path.join(stage, "Scripts" if os.name == "nt" else "bin")

    for name in generated_names:
        _remove_existing(os.path.join(scripts_root, name))

    generated_paths: list[str] = []

    if plan.scripts:
        # Written in place: the stage is private to this install, and removed
        # whole if it fails.
        generated_paths.extend(
            path
            for path, _ in generate_entry_point_files(
                plan.scripts,
                scripts_root,
                script_executable,
            )
        )

    compiled_rows = (
        _materialize_pyc(stage, install_root, archive, skip=plan.loses)
        if pycompile
        else ()
    )

    managed = {
        f"{dist_info}/INSTALLER",
        f"{dist_info}/REQUESTED",
        f"{dist_info}/direct_url.json",
    }

    record_relative = f"{dist_info}/RECORD"

    rows: list[tuple[str, str, str]] = []

    for relative, digest, size, _ in archive.entries:
        top = relative.partition("/")[0]

        if top.endswith(".data"):
            mapped = mapped_parts(relative)

            installed_relative = "/".join(mapped)

            first, last = mapped[0], mapped[-1]

        else:
            # The manifest keeps a member as its validated parts joined, which
            # outside ".data" is where it lands: no mapping to compute, for
            # each of a wheel's thousands of members.
            installed_relative = relative

            first, last = top, relative.rpartition("/")[2]

        if installed_relative in managed:
            continue

        if first in {"bin", "Scripts"} and last in generated_names:
            continue

        if installed_relative == record_relative:
            rows.append((installed_relative, "", ""))

            continue

        if installed_relative in script_members:
            digest, size = _file_metadata(os.path.join(stage, installed_relative))

        rows.append((installed_relative, digest, size))

    rows.append((f"{dist_info}/INSTALLER", *installer_metadata))

    if requested_metadata is not None:
        rows.append((f"{dist_info}/REQUESTED", *requested_metadata))

    if direct_url_metadata is not None:
        rows.append((f"{dist_info}/direct_url.json", *direct_url_metadata))

    for path in generated_paths:
        generated_metadata = _file_metadata(path)

        rows.append(
            (
                "/".join(
                    (
                        "Scripts" if os.name == "nt" else "bin",
                        os.path.basename(path),
                    ),
                ),
                *generated_metadata,
            ),
        )

    rows.extend(compiled_rows)

    rows.sort()

    record = io.StringIO(newline="")

    csv.writer(record).writerows(rows)

    _write_new_file(
        os.path.join(dist_info_root, "RECORD"),
        record.getvalue().encode("utf-8"),
    )


def _plan_destinations(
    root: str, plan: _WheelInstallPlan, *, pycompile: bool = False
) -> set[str]:
    destinations: set[str] = set()

    for relative, _, _, _ in plan.archive.entries:
        mapped = mapped_parts(relative)

        destinations.add(os.path.join(root, "/".join(mapped)))

        if pycompile and (compiled := compiled_parts(mapped)) is not None:
            destinations.add(os.path.join(root, "/".join(compiled)))

    scripts_root = os.path.join(root, "Scripts" if os.name == "nt" else "bin")

    for name in plan.scripts:
        destinations.update(
            os.path.join(scripts_root, generated)
            for generated in (name, f"{name}-script.py", f"{name}.exe")
        )

    return destinations


def _stage_path(root: str, stage: str, path: str) -> str | None:
    try:
        relative = os.path.relpath(path, root)

    except ValueError:
        return None

    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None

    return os.path.join(stage, relative)


def _remove_stage_files(stage: str, paths: set[str]) -> None:
    """Remove an owned file batch and prune each parent at most once."""

    parents: set[str] = set()

    for path in sorted(paths, key=lambda item: item.count(os.sep), reverse=True):
        try:
            os.unlink(path)

        except FileNotFoundError:
            pass

        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EISDIR, errno.EPERM} or not (
                os.path.isdir(path) and not os.path.islink(path)
            ):
                raise

            try:
                shutil.rmtree(path)

            except FileNotFoundError:
                pass

        parent = os.path.dirname(path)

        while parent != stage and parent.startswith(stage + os.sep):
            parents.add(parent)

            parent = os.path.dirname(parent)

    for parent in sorted(
        parents,
        key=lambda item: item.count(os.sep),
        reverse=True,
    ):
        try:
            os.rmdir(parent)

        except OSError:
            pass


def _target_exceeds_entry_limit(root: str, limit: int) -> bool:
    entries_seen = 0

    for _, directories, files in os.walk(root):
        entries_seen += len(directories) + len(files)

        if entries_seen > limit:
            return True

    return False


def install_wheels_from_archive_cache(
    requests: tuple[WheelRequest, ...],
    candidates: tuple[InstallCandidate, ...],
    *,
    target: InstallTarget,
    cache_dir: str,
    script_executable: str | None = None,
    force: bool = False,
    preserve_existing: bool = False,
    report: bool = True,
    pycompile: bool = False,
) -> tuple[InstallCandidate, ...] | None:
    """Install into a self-contained target from unpacked archives.


    ``None`` means that the target or cache cannot use this optimization and

    the caller should retain the legacy transactional installation path.

    """

    root = _eligible_target(target, cache_dir)

    if root is None:
        return None

    try:
        archives = prepare_cached_wheels(candidates, cache_dir, pycompile=pycompile)

    except OSError:
        return None

    try:
        plans = _build_plans(requests, candidates, archives, pycompile=pycompile)

    except _SharedDestination:
        return None

    parent = os.path.dirname(root)

    try:
        os.makedirs(parent, exist_ok=True)

        staging_parent = tempfile.mkdtemp(prefix=".kpip-install-", dir=parent)

    except OSError:
        # The stage goes beside the target so the swap is a rename; a
        # parent this user cannot write is no reason to refuse a target
        # they can, which the transactional path writes into in place.
        return None

    stage = os.path.join(staging_parent, "target")

    pool = None

    try:
        root_existed = os.path.isdir(root)

        active_plans = plans

        uninstalling: list[
            InstalledMetadataDistribution | InstalledWheelDistribution
        ] = []

        destinations_by_plan: dict[_WheelInstallPlan, set[str]] = {}

        if root_existed:
            names = {plan.candidate.canonical_name for plan in plans}

            existing = discover_installed_wheels((root,), names=names)

            if existing is None:
                existing = {
                    distribution.canonical_name: distribution
                    for distribution in InstalledDistributionStore(paths=[root]).iter(
                        names=names,
                    )
                }

            selected: list[_WheelInstallPlan] = []

            allowed_existing: set[str] = set()

            removals: set[str] = set()

            for plan in plans:
                distribution = existing.get(plan.candidate.canonical_name)

                if (
                    distribution is not None
                    and distribution.version == plan.candidate.version
                    and not force
                    and not preserve_existing
                ):
                    continue

                selected.append(plan)

                if distribution is None:
                    continue

                owned_paths, old_paths = existing_paths(distribution, target)

                allowed_existing.update(
                    normalized
                    for path in owned_paths
                    if (normalized := _internal_comparison_path(path))
                )

                destinations = _plan_destinations(root, plan, pycompile=pycompile)

                destinations_by_plan[plan] = destinations

                normalized_destinations = {
                    _internal_comparison_path(path) for path in destinations
                }

                removals.update(
                    old_paths
                    if not preserve_existing
                    else {
                        path
                        for path in owned_paths
                        if _internal_comparison_path(path) in normalized_destinations
                    }
                )

                uninstalling.append(distribution)

            active_plans = tuple(selected)

            if not active_plans:
                return candidates

            if len(active_plans) < 4 and _target_exceeds_entry_limit(root, 128):
                return None

            for plan in active_plans:
                destinations = destinations_by_plan.get(plan)

                if destinations is None:
                    destinations = _plan_destinations(root, plan, pycompile=pycompile)

                for destination in destinations:
                    if (
                        os.path.lexists(destination)
                        and _internal_comparison_path(destination)
                        not in allowed_existing
                    ):
                        return None

            clone_path(root, stage)

            staged_removals: set[str] = set()

            for path in removals:
                staged = _stage_path(root, stage, path)

                if staged is None:
                    return None

                staged_removals.add(staged)

            _remove_stage_files(stage, staged_removals)

        active_archives = tuple(plan.archive for plan in active_plans)

        if len(archives) >= 4 or len(plans) >= 4:
            pool = ThreadPoolExecutor(
                max_workers=min(
                    _CLONE_WORKERS,
                    max(len(active_archives), len(active_plans)),
                ),
                thread_name_prefix="kpip-clone",
            )

        try:

            def clone_plan(plan: _WheelInstallPlan) -> None:
                remember_tree_listings(plan.archive)

                clone_path(
                    plan.archive.tree,
                    stage,
                    skip=_stage_paths(stage, plan.loses),
                    replace=_stage_paths(stage, plan.wins - plan.loses),
                )

            if len(active_archives) >= 4 and pool is not None:
                tuple(pool.map(clone_plan, active_plans))

            else:
                for plan in active_plans:
                    clone_plan(plan)

            for archive in active_archives:
                _relocate_data(stage, archive)

        except FileExistsError as exc:
            raise InstallationError(
                "duplicate installation destination while linking cached wheels",
            ) from exc

        if len(active_plans) >= 4 and pool is not None:

            def finalize(plan: _WheelInstallPlan) -> None:
                _finalize_wheel(
                    stage,
                    plan,
                    install_root=root,
                    script_executable=script_executable,
                    pycompile=pycompile,
                )

            tuple(pool.map(finalize, active_plans))

        else:
            for plan in active_plans:
                _finalize_wheel(
                    stage,
                    plan,
                    install_root=root,
                    script_executable=script_executable,
                    pycompile=pycompile,
                )

        if os.path.lexists(root) != root_existed:
            return None

        if root_existed:
            backup = os.path.join(staging_parent, "previous")

            # A target that is a mount point cannot be renamed (EBUSY), nor
            # one whose parent refuses it: nothing has moved yet, and the
            # transactional path installs into it in place.
            try:
                os.rename(root, backup)

            except OSError:
                return None

            try:
                os.rename(stage, root)

            except OSError:
                os.rename(backup, root)

                return None

            except BaseException:
                os.rename(backup, root)

                raise

            shutil.rmtree(backup, ignore_errors=True)

        else:
            try:
                os.rename(stage, root)

            except OSError:
                return None

        if report:
            for distribution in uninstalling:
                logger.info(
                    f"Uninstalling {distribution.raw_name}-{distribution.raw_version}",
                )

                logger.info(
                    f"Successfully uninstalled {distribution.raw_name}-{distribution.raw_version}",
                )

        return candidates

    finally:
        if pool is not None:
            pool.shutdown()

        shutil.rmtree(staging_parent, ignore_errors=True)
