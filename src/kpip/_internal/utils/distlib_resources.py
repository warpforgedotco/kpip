"""Let distlib find its Windows launchers in the compiled kpip.

``distlib.scripts`` reads its launchers when it is imported, through a
resource finder it picks by the type of its package's loader, and it knows
only the standard library's loaders. The compiled kpip's modules have
Nuitka's, so the import failed with "Unable to locate finder", and every
install with it. There, as from source, the launchers are files in the
package's directory, which distlib's file system finder reads. Import this
module before ``distlib.scripts``.
"""

from __future__ import annotations

from kpip._vendor import distlib
from kpip._vendor.distlib.resources import ResourceFinder, register_finder


def register() -> None:
    """Read distlib's package resources from its directory, whatever loads it."""
    register_finder(distlib.__loader__, ResourceFinder)


register()
