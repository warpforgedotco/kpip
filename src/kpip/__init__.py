from __future__ import annotations

lazy from kpip.cli import entrypoint

__version__ = "0.0.1"


def main(args: list[str] | None = None) -> int:
    """This is an internal API only meant for use by kpip's own console scripts.

    For additional details, see https://github.com/pypa/pip/issues/7498.
    """

    return entrypoint.main(args, version=None, location=__file__)
