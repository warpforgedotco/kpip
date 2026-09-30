"""Where a ``--target`` install writes."""

from __future__ import annotations

import os


def target_prefix() -> str | None:
    return os.environ.get("KPIP_TARGET_PREFIX")


def target_paths() -> list[str] | None:
    prefix = target_prefix()
    if prefix is None:
        return None
    from kpip.host.locations.sysconfig_scheme import get_scheme

    scheme = get_scheme("kpip", prefix=prefix)
    return [scheme.purelib, scheme.platlib]
