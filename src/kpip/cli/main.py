"""Compatibility entrypoint for callers importing ``kpip.cli.main``."""

from __future__ import annotations

from kpip.cli import entrypoint


def main(
    args: list[str] | None = None,
    *,
    version: str | None = None,
    location: str | None = None,
) -> int:
    """Delegate to :func:`kpip.cli.entrypoint.main`.

    This compatibility wrapper preserves the historical ``kpip.cli.main.main``
    entry point used by tests and external callers while routing all dispatch
    through the canonical :mod:`kpip.cli.entrypoint` implementation.
    """

    return entrypoint.main(args, version=version, location=location)
