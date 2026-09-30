"""Import-light helpers for locating legacy setuptools ``.egg-link`` files."""

from __future__ import annotations

lazy import os
lazy import re
lazy import sys

_EGG_LINK_NAME_NORMALIZER: re.Pattern[str] | None = None


def egg_link_names(raw_name: str) -> tuple[str, str]:
    """Return the two filename spellings setuptools has historically used."""
    global _EGG_LINK_NAME_NORMALIZER

    normalizer = _EGG_LINK_NAME_NORMALIZER
    if normalizer is None:
        normalizer = re.compile("[^A-Za-z0-9.]+")
        _EGG_LINK_NAME_NORMALIZER = normalizer

    return (
        normalizer.sub("-", raw_name) + ".egg-link",
        f"{raw_name}.egg-link",
    )


def egg_link_path_from_sys_path(raw_name: str) -> str | None:
    """Find the first matching egg-link while preserving ``sys.path`` order."""
    for path_item in sys.path:
        for egg_link_name in egg_link_names(raw_name):
            egg_link = os.path.join(path_item, egg_link_name)
            if os.path.isfile(egg_link):
                return egg_link
    return None
