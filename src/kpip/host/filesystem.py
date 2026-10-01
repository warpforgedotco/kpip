from __future__ import annotations

import os
import os.path
from collections.abc import Callable
from functools import wraps
from time import perf_counter, sleep
from typing import BinaryIO, ParamSpec, TypeVar, cast

P = ParamSpec("P")
R = TypeVar("R")


def retry(
    *,
    wait: float,
    stop_after_delay: float,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            deadline = perf_counter() + stop_after_delay
            while True:
                try:
                    return func(*args, **kwargs)
                except BaseException as exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    if perf_counter() > deadline:
                        raise
                    if wait:
                        sleep(wait)

        return cast("Callable[P, R]", wrapper)

    return decorate


def format_size(size: float) -> str:
    if size > 1000 * 1000:
        return f"{size / (1000 * 1000):.1f} MB"
    if size > 1000:
        return f"{size / 1000:.1f} kB"
    return f"{size:.0f} bytes"


# Windows fails a rename while another handle is open, which a scanner or an
# indexer takes transiently; POSIX rename is atomic and has no such failure,
# so there the retry is a Python frame and a clock read per cache entry.
replace = (
    retry(stop_after_delay=1, wait=0.25)(os.replace) if os.name == "nt" else os.replace
)


def set_descriptor_permissions(descriptor: int, path: str, mode: int) -> None:
    """``set_file_permissions`` for a descriptor that has no file object."""
    if os.chmod in os.supports_fd:
        os.chmod(descriptor, mode)
    elif os.chmod in os.supports_follow_symlinks:
        os.chmod(path, mode, follow_symlinks=False)


def set_file_permissions(target_file: BinaryIO, mode: int) -> None:
    if os.chmod in os.supports_fd:
        os.chmod(target_file.fileno(), mode)
    elif os.chmod in os.supports_follow_symlinks:
        os.chmod(target_file.name, mode, follow_symlinks=False)
