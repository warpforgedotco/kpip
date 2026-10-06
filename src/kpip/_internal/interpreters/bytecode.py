"""Compiling installed modules to bytecode with the target interpreter.

Bytecode is only good for the interpreter that wrote it: its magic number
and its cache tag name the ``.pyc`` file. So another Python compiles what is
installed for it, in one process per wheel; kpip's own interpreter compiles
in this process, as pip does.
"""

from __future__ import annotations

import json
import logging
import subprocess

from kpip._internal.exceptions import InstallationError
from kpip._internal.interpreters.facts import Interpreter
from kpip._internal.interpreters.probe import SAFE_PATH

logger = logging.getLogger(__name__)

# Reads a JSON list of source paths and answers with the [source, pyc] of
# each one compiled. compileall says what failed on stdout, which is the
# answer's, so it says it on stderr instead.
_WORKER = r"""
import compileall
import contextlib
import importlib.util
import json
import sys
import warnings

warnings.filterwarnings("ignore")
sys.stdin.reconfigure(encoding="utf-8", errors="surrogateescape")
sys.stdout.reconfigure(encoding="utf-8", errors="surrogateescape")
compiled = []
with contextlib.redirect_stdout(sys.stderr):
    for source in json.load(sys.stdin):
        if compileall.compile_file(source, force=True, quiet=1):
            compiled.append([source, importlib.util.cache_from_source(source)])
json.dump(compiled, sys.stdout)
"""


def compile_with(interpreter: Interpreter, sources: list[str]) -> list[tuple[str, str]]:
    """Compile ``sources`` with ``interpreter``: the (source, pyc) of each
    compiled; one that does not compile, as with pip, is left without."""
    if not sources:
        return []
    try:
        result = subprocess.run(
            [interpreter.executable, "-c", SAFE_PATH + _WORKER],
            input=json.dumps(sources),
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except OSError as exc:
        raise InstallationError(
            f"Could not run {interpreter.executable} to compile bytecode: {exc}"
        ) from exc
    if result.stderr:
        logger.debug(result.stderr)
    if result.returncode != 0:
        raise InstallationError(
            f"Compiling bytecode with {interpreter.executable} failed "
            f"(exit status {result.returncode})."
        )
    return [(source, pyc) for source, pyc in json.loads(result.stdout)]
