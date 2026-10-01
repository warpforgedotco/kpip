"""The modules the subinterpreters of a compiled kpip import.

kpip unpacks wheels on subinterpreters (``kpip.install.archive_workers``).
A new interpreter imports every module it needs through the standard import
system, ``encodings`` first of all, and cannot see the modules compiled into
the binary. Nuitka embeds bytecode of the modules named with
``--subinterpreter-bytecode`` in the binary's frozen table, where they can
(kpip's patch 0014); the binary itself still runs the compiled ones.

Which modules a worker needs is found by running one the way kpip does -- a
real ``InterpreterPoolExecutor`` unpacking a real wheel -- under the binary's
Python, and listing its ``sys.modules``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from kpip_compile.vendor import REPO_ROOT

SOURCE_ROOT = REPO_ROOT / "src"

_PROBE_MODULE = "_kpip_worker_probe"

_PROBE = '''\
"""Report what a worker interpreter has loaded. Imported into the worker."""

import sys


def loaded():
    modules = []
    for name, module in list(sys.modules.items()):
        # __file__ first: a frozen module keeps its source's path there.
        spec = getattr(module, "__spec__", None)
        origin = getattr(module, "__file__", None) or getattr(spec, "origin", None)
        # An alias, e.g. "collections.abc" for "_collections_abc", is not a
        # module of its own to embed.
        alias = getattr(module, "__name__", name) != name
        modules.append((name, origin, alias))
    return sorted(modules)
'''

_RUN = """\
import base64
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from concurrent.futures import InterpreterPoolExecutor

import _kpip_worker_probe
from kpip.install import archive_workers

work = tempfile.mkdtemp()
wheel = os.path.join(work, "probe-1.0-py3-none-any.whl")
members = {
    "probe/__init__.py": b"VALUE = 1\\n",
    "probe-1.0.data/scripts/probe-script": b"#!python\\nprint(1)\\n",
    "probe-1.0.dist-info/METADATA": (
        b"Metadata-Version: 2.1\\nName: probe\\nVersion: 1.0\\n"
    ),
    "probe-1.0.dist-info/WHEEL": (
        b"Wheel-Version: 1.0\\nGenerator: kpip-compile\\n"
        b"Root-Is-Purelib: true\\nTag: py3-none-any\\n"
    ),
}
record = []
for name, data in members.items():
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    record.append(f"{name},sha256={digest.decode()},{len(data)}")
record.append("probe-1.0.dist-info/RECORD,,")
members["probe-1.0.dist-info/RECORD"] = ("\\n".join(record) + "\\n").encode()
with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
    for name, data in members.items():
        archive.writestr(name, data)
with open(wheel, "rb") as file:
    sha256 = hashlib.sha256(file.read()).hexdigest()

with InterpreterPoolExecutor(max_workers=1) as pool:
    pool.submit(archive_workers._import_kpip).result()
    # A worker only unpacks: the main interpreter byte-compiles.
    pool.submit(
        archive_workers.unpack_in_worker,
        wheel,
        sha256,
        os.path.join(work, "cache"),
    ).result()
    modules = pool.submit(_kpip_worker_probe.loaded).result()

json.dump(modules, sys.stdout)
"""


def worker_modules(python: str, source_root: Path = SOURCE_ROOT) -> list:
    """``(name, origin, is_alias)`` for each module a kpip worker loads.

    Run under ``python``, the binary's interpreter, with kpip from
    ``source_root``.
    """
    with tempfile.TemporaryDirectory(prefix="kpip-worker-probe-") as temp:
        Path(temp, f"{_PROBE_MODULE}.py").write_text(_PROBE, encoding="utf-8")
        env = dict(os.environ)
        # The workers read PYTHONPATH as the main interpreter does.
        env["PYTHONPATH"] = os.pathsep.join((str(source_root), temp))
        env.pop("KPIP_SUBINTERPRETERS", None)
        # -S, as the binary runs: no site, and none of the modules an
        # environment's .pth files import. Not -I: the workers find kpip and
        # the probe through PYTHONPATH.
        result = subprocess.run(
            [python, "-S", "-c", _RUN],
            env=env,
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"a kpip worker could not be run under {python}:\n{result.stderr}"
        )
    return json.loads(result.stdout)


def bytecode_modules(modules: list) -> list[str]:
    """The names to embed: modules with Python source, less aliases and the
    probe's own."""
    return sorted(
        name
        for name, origin, alias in modules
        if isinstance(origin, str)
        and origin.endswith(".py")
        and not alias
        and name not in {_PROBE_MODULE, "__main__"}
    )


def subinterpreter_modules(python: str, source_root: Path = SOURCE_ROOT) -> list[str]:
    """What ``--subinterpreter-bytecode`` names for a kpip built with ``python``."""
    return bytecode_modules(worker_modules(python, source_root))
