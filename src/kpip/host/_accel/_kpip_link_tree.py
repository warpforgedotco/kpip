"""``_kpip_link_tree.c``'s loops in Python, for when the C built-in is absent.

Same results as the C: ``(0, len(names))``, or the errno and index of the
first failure.
"""

from __future__ import annotations

import errno
import os
import shutil


def make_directories(root: bytes, names: list, modes: list) -> tuple:
    """``mkdir`` each of ``names`` under ``root`` with its mode, parents first."""
    for index, name in enumerate(names):
        try:
            os.mkdir(os.path.join(root, name), modes[index])

        except OSError as exc:
            return (exc.errno or errno.EIO, index)

    return (0, len(names))


def change_modes(root: bytes, names: list, modes: list) -> tuple:
    """``chmod`` each of ``names`` under ``root`` to its mode."""
    for index, name in enumerate(names):
        try:
            os.chmod(os.path.join(root, name), modes[index])

        except OSError as exc:
            return (exc.errno or errno.EIO, index)

    return (0, len(names))


def link_files(
    source_root: bytes, destination_root: bytes, names: list, start: int
) -> tuple:
    """Hard link each of ``names`` from ``start`` on, between the two roots."""
    join = os.path.join

    for index in range(start, len(names)):
        name = names[index]

        try:
            os.link(join(source_root, name), join(destination_root, name))

        except OSError as exc:
            return (exc.errno or errno.EIO, index)

    return (0, len(names))


def remove_tree(path: bytes) -> int:
    """Remove the directory at ``path`` and everything in it, following no
    link; the errno of the first failure, or 0."""
    failures: list[int] = []

    def failed(function, failed_path, error) -> None:
        if not failures:
            failures.append(getattr(error, "errno", None) or errno.EIO)

    shutil.rmtree(path, onexc=failed)

    return failures[0] if failures else 0
