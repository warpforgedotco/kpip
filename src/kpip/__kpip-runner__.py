"""Execute exactly this copy of kpip, within a different environment.

This file is named as it is, to ensure that this module can't be imported via
an import statement.
"""

import sys

PYTHON_REQUIRES = (3, 14)


def version_str(version):
    return ".".join(str(v) for v in version)


if sys.version_info[:2] < PYTHON_REQUIRES:
    raise SystemExit(
        "This version of kpip does not support python {} (requires >={}).".format(
            version_str(sys.version_info[:2]),
            version_str(PYTHON_REQUIRES),
        ),
    )


import runpy  # noqa: E402
from os.path import dirname  # noqa: E402

KPIP_SOURCES_ROOT = dirname(dirname(__file__))
KPIP_PACKAGE_ROOT = dirname(__file__)
if KPIP_PACKAGE_ROOT in sys.path:
    sys.path.remove(KPIP_PACKAGE_ROOT)
if KPIP_SOURCES_ROOT not in sys.path:
    sys.path.insert(0, KPIP_SOURCES_ROOT)

assert __name__ == "__main__", "Cannot run __kpip-runner__.py as a non-main module"
runpy.run_module("kpip", run_name="__main__", alter_sys=True)
