"""A wheelhouse the option tests share, and how they run a command on it."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from kpip.cli.main import main

from tests.wheel_helpers import make_sdist, make_wheel

APP = "app-1.0-py3-none-any.whl"
LIB = "lib-1.0-py3-none-any.whl"
LIB_PRE = "lib-2.0rc1-py3-none-any.whl"
LIB_SDIST = "lib-1.5.tar.gz"


def build_wheelhouse(directory: Path) -> Path:
    """``app`` 1.0, which needs ``lib>=1``, and ``lib`` 1.0 and 2.0rc1."""
    directory.mkdir()
    make_wheel(directory, "app", "app", "1.0", requires=["lib>=1"])
    make_wheel(directory, "lib", "lib", "1.0")
    make_wheel(directory, "lib", "lib", "2.0rc1")
    return directory


def add_newer_sdist(wheelhouse: Path, staging: Path) -> Path:
    """Add ``lib`` 1.5, newer than its newest final wheel, as a source
    distribution only."""
    staging.mkdir()
    sdist = make_sdist(staging, "lib", "lib", "1.5", standalone_backend=True)
    return Path(shutil.copy(sdist, wheelhouse))


def run(command: str, wheelhouse: Path, *args: str | Path) -> int:
    return main(
        [
            command,
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--no-cache-dir",
            *map(str, args),
        ]
    )


def names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
