"""Resolve the version of the running kpip."""

from __future__ import annotations

import kpip
from kpip.core.utils import current_version

KPIP_DISTRIBUTION_NAME = "kpip"

KPIP_DISTRIBUTION_NAMES = frozenset((KPIP_DISTRIBUTION_NAME, "kpip"))


def get_kpip_version() -> str:
    """Return kpip's version from the application context or ``kpip.__version__``."""

    context_version = current_version()

    if context_version is not None:
        return context_version

    return kpip.__version__
