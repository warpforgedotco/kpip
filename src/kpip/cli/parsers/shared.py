"""The options pip's commands share, declared once.

pip groups them in ``cmdoptions``: where packages are looked for, which of
their files may be chosen, which interpreter they are chosen for, and what
``install``, ``download``, ``wheel`` and ``lock`` all take to name and build
their requirements. Each function here adds one such group to a parser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import argparse
import datetime
import os
import re

from kpip.core.packaging import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any


class OrderedValue(argparse.Action):
    """Keep a value under its own option and in the order it came among its
    counterpart's.

    ``--no-binary`` and ``--only-binary`` undo each other, as ``--all-releases``
    and ``--only-final`` do, so what they add up to depends on the order they
    were given in; ``ordered`` names the list both options of a pair write
    ``(option, value)`` to.
    """

    def __init__(
        self,
        option_strings: Sequence[str],
        dest: str,
        ordered: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(option_strings, dest, **kwargs)
        self.ordered = ordered

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        setattr(namespace, self.dest, [*getattr(namespace, self.dest), values])
        setattr(
            namespace,
            self.ordered,
            [
                *getattr(namespace, self.ordered),
                (self.option_strings[0].removeprefix("--"), values),
            ],
        )


class RefreshPackage(argparse.Action):
    """Collect the projects whose index pages are revalidated, as pip does:
    ``:all:`` stands for every project and ``:none:`` empties the set."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        existing = set(getattr(namespace, self.dest))
        new = str(values).split(",")

        while ":all:" in new:
            existing = {":all:"}
            del new[: new.index(":all:") + 1]
            if ":none:" not in new:
                new = []

        for name in new:
            if name == ":none:":
                existing.clear()
            else:
                existing.add(canonicalize_name(name))

        setattr(namespace, self.dest, existing)


def uploaded_prior_to(value: str) -> datetime.datetime:
    """An ISO 8601 datetime, or ``PnD`` for that many days ago."""
    match = re.match(r"^P(\d+)D$", value, re.ASCII)
    if match:
        return datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            days=int(match.group(1))
        )

    try:
        moment = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid value: {value!r}: {exc}. "
            "Expected an ISO 8601 datetime string "
            "(e.g., '2023-01-01' or '2023-01-01T00:00:00Z') "
            "or a duration in days (e.g., 'P3D')"
        ) from None

    # As pip: a datetime given without an offset is in the local timezone.
    return moment.astimezone() if moment.tzinfo is None else moment


def python_version(value: str) -> str:
    """``3``, ``37``, ``3.7`` or ``3.7.3``, as pip reads ``--python-version``."""
    parts = value.split(".")

    if len(parts) > 3:
        raise argparse.ArgumentTypeError(
            f"invalid --python-version value: {value!r}: "
            "at most three version parts are allowed"
        )

    if value and not all(part.isdigit() for part in parts):
        raise argparse.ArgumentTypeError(
            f"invalid --python-version value: {value!r}: "
            "each version part must be an integer"
        )

    return value


def source_directory(value: str) -> str:
    return os.path.abspath(os.path.expanduser(value))


def add_index_options(parser: argparse.ArgumentParser) -> None:
    """pip's "Package Index Options"."""
    parser.add_argument("-i", "--index-url", "--pypi-url", dest="index_url")
    parser.add_argument("--extra-index-url", action="append", default=[])
    parser.add_argument("--no-index", action="store_true")
    parser.add_argument(
        "--refresh-package",
        action=RefreshPackage,
        default=frozenset(),
    )
    parser.add_argument("-f", "--find-links", action="append", default=[])
    parser.add_argument("--uploaded-prior-to", type=uploaded_prior_to)


def add_selection_options(parser: argparse.ArgumentParser) -> None:
    """pip's "Package Selection Options"."""
    parser.add_argument("--pre", action="store_true")
    parser.add_argument(
        "--all-releases",
        action=OrderedValue,
        ordered="release_control",
        default=[],
    )
    parser.add_argument(
        "--only-final",
        action=OrderedValue,
        ordered="release_control",
        default=[],
    )
    parser.add_argument(
        "--no-binary",
        action=OrderedValue,
        ordered="format_control",
        default=[],
    )
    parser.add_argument(
        "--only-binary",
        action=OrderedValue,
        ordered="format_control",
        default=[],
    )
    parser.add_argument("--prefer-binary", action="store_true")
    parser.set_defaults(release_control=[], format_control=[])


def add_target_python_options(parser: argparse.ArgumentParser) -> None:
    """The interpreter distributions are chosen for, when not the running one."""
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--python-version", type=python_version)
    parser.add_argument("--implementation")
    parser.add_argument("--abi", action="append", default=[])


def add_requirement_options(parser: argparse.ArgumentParser) -> None:
    """What ``install``, ``download``, ``wheel`` and ``lock`` all take to name
    their requirements and build them."""
    parser.add_argument("requirements", nargs="*")
    parser.add_argument("--group", dest="groups", action="append", default=[])
    parser.add_argument(
        "-r",
        "--requirement",
        dest="requirement_files",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--requirements-from-script",
        dest="requirements_from_scripts",
        action="append",
        default=[],
    )
    parser.add_argument(
        "-c",
        "--constraint",
        dest="constraint_files",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--build-constraint",
        dest="build_constraint_files",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--no-deps", "--no-dependencies", dest="no_deps", action="store_true"
    )
    parser.add_argument(
        "--only-deps", "--only-dependencies", dest="only_deps", action="store_true"
    )
    parser.add_argument(
        "--src",
        "--source",
        "--source-dir",
        "--source-directory",
        dest="src_dir",
        type=source_directory,
    )
    parser.add_argument("--ignore-requires-python", action="store_true")
    parser.add_argument("--no-build-isolation", action="store_true")
    parser.add_argument("--require-hashes", action="store_true")
    parser.add_argument("--no-require-hashes", action="store_true")

    parser.add_argument("--check-build-dependencies", action="store_true")
    parser.add_argument("--no-clean", action="store_true")

    # Accepted as pip accepts them, and without effect here: every build is a
    # PEP 517 build, and nothing draws a progress bar.
    parser.add_argument("--use-pep517", action="store_true")
    parser.add_argument(
        "--progress-bar", choices=("auto", "on", "off", "raw"), default="auto"
    )


def add_editable_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-e",
        "--editable",
        dest="editables",
        action="append",
        default=[],
    )


def add_config_settings_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-C",
        "--config-settings",
        dest="config_settings",
        action="append",
        default=[],
    )


def add_externally_managed_options(parser: argparse.ArgumentParser) -> None:
    """What ``install`` and ``uninstall`` take about an environment another
    package manager owns, or that root is changing."""
    parser.add_argument("--break-system-packages", action="store_true")
    parser.add_argument("--system", action="store_true")
    parser.add_argument(
        "--root-user-action", choices=("warn", "ignore"), default="warn"
    )
