"""``kpip.cli.main.main``, which the tests call to run kpip in-process."""

from __future__ import annotations

from kpip.cli import entrypoint


def main(args: list[str] | None = None) -> int:
    """Delegate to :func:`kpip.cli.entrypoint.main`."""

    return entrypoint.main(args)
