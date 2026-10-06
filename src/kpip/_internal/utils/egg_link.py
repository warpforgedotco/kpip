from __future__ import annotations

import os
import re

from kpip._internal.interpreters import search_path, target_interpreter
from kpip._internal.locations import (
    get_site_packages,
    get_user_site,
    site_packages,
    user_site,
)
from kpip._internal.utils.virtualenv import (
    running_under_virtualenv,
    virtualenv_no_global,
)

__all__ = [
    "egg_link_path_from_sys_path",
    "egg_link_path_from_location",
]


def _egg_link_names(raw_name: str) -> list[str]:
    """
    Convert a Name metadata value to a .egg-link name, by applying
    the same substitution as pkg_resources's safe_name function.
    Note: we cannot use canonicalize_name because it has a different logic.

    We also look for the raw name (without normalization) as setuptools 69 changed
    the way it names .egg-link files (https://github.com/pypa/setuptools/issues/4167).
    """
    return [
        re.sub("[^A-Za-z0-9.]+", "-", raw_name) + ".egg-link",
        f"{raw_name}.egg-link",
    ]


def egg_link_path_from_sys_path(raw_name: str) -> str | None:
    """
    Look for a .egg-link file for project name, by walking the target
    interpreter's search path.
    """
    egg_link_names = _egg_link_names(raw_name)
    for path_item in search_path():
        for egg_link_name in egg_link_names:
            egg_link = os.path.join(path_item, egg_link_name)
            if os.path.isfile(egg_link):
                return egg_link
    return None


def egg_link_path_from_location(raw_name: str) -> str | None:
    """
    Return the path for the .egg-link file if it exists, otherwise, None.

    There's 3 scenarios:
    1) not in a virtualenv
       try to find in site.USER_SITE, then site_packages
    2) in a no-global virtualenv
       try to find in site_packages
    3) in a yes-global virtualenv
       try to find in site_packages, then site.USER_SITE
       (don't look in global location)

    For #1 and #3, there could be odd cases, where there's an egg-link in 2
    locations.

    This method will just return the first one found.
    """
    target_site_packages, target_user_site = site_packages, user_site
    if not target_interpreter().is_own:
        target_site_packages, target_user_site = get_site_packages(), get_user_site()
    sites: list[str] = []
    if running_under_virtualenv():
        sites.append(target_site_packages)
        if not virtualenv_no_global() and target_user_site:
            sites.append(target_user_site)
    else:
        if target_user_site:
            sites.append(target_user_site)
        sites.append(target_site_packages)

    egg_link_names = _egg_link_names(raw_name)
    for site in sites:
        for egg_link_name in egg_link_names:
            egglink = os.path.join(site, egg_link_name)
            if os.path.isfile(egglink):
                return egglink
    return None
