"""Plain modules for the subinterpreters a compiled kpip unpacks wheels on.

kpip unpacks wheels on subinterpreters (``kpip.install.archive_workers``).
A new interpreter starts with nothing but the import system and imports every
module it needs from files -- ``encodings`` before anything else -- and a
compiled kpip has none: its modules, the standard library's among them, are
compiled into the binary, behind an import hook only the main interpreter
has. Started in the binary, a subinterpreter fails before running a line.

So the binary ships, beside itself, a plain ``.pyc`` of every module a worker
loads. They are found by running a worker the way kpip does -- a real
``InterpreterPoolExecutor`` unpacking a real wheel -- and recording its
``sys.modules``, then compiled by the
binary's own Python, whose bytecode they must be. The main interpreter still
imports its compiled modules, which its import hook finds first; only the
workers read these.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
        # __file__ first: a frozen module keeps its source's path there, and
        # a binary's workers may not have frozen modules to fall back on.
        spec = getattr(module, "__spec__", None)
        origin = getattr(module, "__file__", None) or getattr(spec, "origin", None)
        package = getattr(module, "__path__", None) is not None
        modules.append((name, origin, package))
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
    # Without byte-compilation: with it, a worker's compile pool starts a
    # daemon thread, which a subinterpreter refuses, and the job is done
    # again in the main interpreter.
    pool.submit(
        archive_workers.unpack_in_worker,
        wheel,
        sha256,
        os.path.join(work, "cache"),
        False,
    ).result()
    modules = pool.submit(_kpip_worker_probe.loaded).result()

json.dump(modules, sys.stdout)
"""


def worker_modules(python: str, source_root: Path = SOURCE_ROOT) -> list:
    """``(name, origin, is_package)`` for each module a kpip worker loads.

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


def pure_modules(modules: list) -> list[tuple[str, str, bool]]:
    """The modules there is Python source for, less the probe's own."""
    return [
        (name, origin, package)
        for name, origin, package in modules
        if isinstance(origin, str)
        and origin.endswith(".py")
        and name not in {_PROBE_MODULE, "__main__"}
    ]


def extension_modules(modules: list) -> list[str]:
    """The workers' extension modules: the binary ships them already."""
    suffixes = (".so", ".pyd")
    return sorted(
        name
        for name, origin, _ in modules
        if isinstance(origin, str) and origin.endswith(suffixes)
    )


def compiled_path(name: str, package: bool) -> str:
    """Where ``name``'s ``.pyc`` goes, relative to the runtime directory."""
    parts = name.split(".")
    if package:
        parts.append("__init__")
    return "/".join(parts) + ".pyc"


_COMPILE = """\
import json
import os
import py_compile
import sys

destination = sys.argv[1]
for source, relative in json.load(sys.stdin):
    target = os.path.join(destination, *relative.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    py_compile.compile(
        source,
        cfile=target,
        dfile=relative[:-1],
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
"""


def stage_worker_modules(
    python: str, destination: Path, source_root: Path = SOURCE_ROOT
) -> list[str]:
    """Write the workers' modules to ``destination`` as ``.pyc``; their paths.

    Compiled by ``python``, whose bytecode the binary runs.
    """
    modules = worker_modules(python, source_root)
    pure = pure_modules(modules)
    jobs = [(origin, compiled_path(name, package)) for name, origin, package in pure]
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [python, "-I", "-c", _COMPILE, str(destination)],
        input=json.dumps(jobs),
        check=True,
        text=True,
    )
    return sorted(relative for _, relative in jobs)


if __name__ == "__main__":
    for path in stage_worker_modules(sys.executable, Path(sys.argv[1])):
        print(path)
