"""Resolve the version of the running kpip."""

from __future__ import annotations

import kpip
from kpip.core.utils import current_version


def get_kpip_version() -> str:
    """Return kpip's version from the application context or ``kpip.__version__``."""

    context_version = current_version()

    if context_version is not None:
        return context_version

    return kpip.__version__
