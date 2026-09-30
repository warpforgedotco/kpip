"""Transactional removal of installed distributions."""

from __future__ import annotations

import csv
import ntpath
import os

from kpip.build.metadata import InstalledDistributionStore
from kpip.core.errors import InstallationError
from kpip.host.interpreter_facts import search_path, target_interpreter
from kpip.host.locations.sysconfig_scheme import get_scheme
from kpip.install.bytecode import pyc_name
from kpip.install.transaction import InstallTransaction


class DistributionUninstaller:
    """Remove installed distributions through their recorded files."""

    def __init__(self, paths: list[str] | None = None) -> None:
        self.paths = paths

    def uninstall(self, name: str) -> bool:
        return uninstall_distribution(name, paths=self.paths)


def _script_directories(root: str) -> frozenset[str]:
    """The script directories a distribution installed in ``root`` may write.

    A wheel's ``.data/scripts`` members are the one class of file that
    legitimately lands outside the library root, so removing them has to be
    allowed -- but only in the directories this layout actually puts them in.
    Accepting any parent directory merely *named* ``bin`` would let a crafted
    RECORD reach ``/tmp/anywhere/bin/x``.

    The candidates are the scripts path of the target interpreter, and the
    ones implied by the layouts kpip installs into: ``<root>/bin`` for a
    ``--target`` directory, ``<prefix>/Scripts`` beside a Windows
    ``Lib/site-packages``, and ``<prefix>/bin`` above a POSIX
    ``lib/pythonX.Y/site-packages``.
    """

    candidates = [os.path.join(root, "bin"), os.path.join(root, "Scripts")]

    windows_prefix = os.path.dirname(os.path.dirname(root))
    candidates.append(os.path.join(windows_prefix, "Scripts"))

    posix_prefix = os.path.dirname(windows_prefix)
    candidates.append(os.path.join(posix_prefix, "bin"))

    candidates.append(get_scheme("", interpreter=target_interpreter()).scripts)

    return frozenset(
        os.path.normcase(os.path.realpath(candidate)) for candidate in candidates
    )


def _inside_distribution(path: str, root: str) -> bool:
    """Whether ``path`` is a file this distribution may remove.

    Either it sits under the directory holding the ``.dist-info``, or it is a
    console script in one of this layout's script directories.

    The path is resolved first: ``commonpath`` compares path components
    literally, so an unresolved ``site-packages/../../../etc/passwd`` would
    otherwise look like it starts inside the distribution.
    """
    resolved = os.path.realpath(path)
    try:
        if os.path.commonpath((resolved, root)) == root:
            return True
    except OSError, ValueError:
        pass
    parent = os.path.normcase(os.path.realpath(os.path.dirname(resolved)))
    return parent in _script_directories(root)


def uninstall_distribution(
    name: str,
    *,
    paths: list[str] | None = None,
) -> bool:
    """Remove an installed distribution from its RECORD manifest atomically."""

    distribution = InstalledDistributionStore(paths=paths).find(name)

    if distribution is None:
        return False

    if distribution.info_location and distribution.info_location.endswith(".dist-info"):
        try:
            entries = distribution.read_text("RECORD")

        except FileNotFoundError as exc:
            raise InstallationError(
                f"Cannot uninstall {distribution.raw_name} {distribution.raw_version}: "
                "no RECORD file was found",
            ) from exc

    else:
        entries = None

    root = os.path.realpath(os.fspath(distribution.location))

    recorded_paths: set[str] = set()

    if entries is not None:
        for row in csv.reader(entries.splitlines()):
            if not row or not row[0]:
                continue

            raw_relative = row[0]

            if raw_relative.startswith("/") or (
                os.name == "nt" and ntpath.isabs(raw_relative)
            ):
                # RECORD is whatever the wheel shipped, so an absolute row is
                # followed only where a `..` row would be: inside this
                # distribution, or in the environment's script directory.
                relative_parts: tuple[str, ...] = ()
                path_text = os.path.realpath(os.path.normpath(raw_relative))

                if not _inside_distribution(path_text, root):
                    continue
            else:
                relative_parts = tuple(
                    part for part in raw_relative.split("/") if part and part != "."
                )

                if not relative_parts:
                    continue

                path_text = os.path.join(root, *relative_parts)

            if ".." in relative_parts:
                resolved_text = os.path.realpath(path_text)

            else:
                resolved_text = path_text

            if ".." in relative_parts and not _inside_distribution(
                resolved_text,
                root,
            ):
                continue

            if ".." in relative_parts:
                path_text = resolved_text

            recorded_paths.add(path_text)

            if os.path.splitext(path_text)[1] == ".py":
                recorded_paths.update({f"{path_text}c", f"{path_text[:-3]}.pyo"})
                # The target interpreter's, which RECORD may not list.
                compiled_name = pyc_name(os.path.basename(path_text))
                if compiled_name is not None:
                    recorded_paths.add(
                        os.path.join(
                            os.path.dirname(path_text), "__pycache__", compiled_name
                        )
                    )

    elif distribution.info_location and distribution.info_location.endswith(
        ".egg-info",
    ):
        recorded_paths.add(distribution.info_location)

        egg_link_root = os.path.dirname(distribution.info_location)

        entries = distribution.iter_declared_entries()

        for entry in entries:
            if entry.startswith("/") or (os.name == "nt" and ntpath.isabs(entry)):
                continue

            relative_parts = tuple(
                part for part in entry.split("/") if part and part != "."
            )

            path = os.path.realpath(os.path.join(egg_link_root, *relative_parts))

            if not _inside_distribution(path, root):
                continue

            recorded_paths.add(path)

        if not entries:
            try:
                top_level = distribution.read_text("top_level.txt")

            except FileNotFoundError:
                top_level = ""

            for module_name in top_level.splitlines():
                module_name = module_name.strip()

                if module_name and module_name.isidentifier():
                    recorded_paths.update(
                        {
                            os.path.join(root, module_name),
                            os.path.join(root, f"{module_name}.py"),
                        },
                    )

        egg_links: list[str] = []

        for path_entry in (egg_link_root, *search_path()):
            try:
                with os.scandir(path_entry) as children:
                    egg_links.extend(
                        entry.path
                        for entry in children
                        if entry.name.endswith(".egg-link")
                    )

            except OSError:
                continue

        for egg_link in egg_links:
            if os.path.splitext(os.path.basename(egg_link))[0].casefold() == (
                distribution.raw_name.casefold()
            ):
                recorded_paths.add(egg_link)

    existing = {path for path in recorded_paths if os.path.lexists(path)}

    if not existing:
        return False

    transaction = InstallTransaction()

    for path in existing:
        transaction.delete(path)

    transaction.commit()

    return True
