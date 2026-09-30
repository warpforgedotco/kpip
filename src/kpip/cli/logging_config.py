"""Logging setup, as pip sets it up.

What a command reports goes through logging: ordinary output to stdout,
warnings and errors to stderr, and how much of it is decided by how many
``-v`` and ``-q`` the command was given.
"""

from __future__ import annotations

import logging
import sys

VERBOSE = 15
"""pip's level between INFO and DEBUG, for what one ``-v`` adds."""


class BrokenStdoutLoggingError(BrokenPipeError):
    pass


class KpipFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if not message.startswith("DEPRECATION: "):
            if record.levelno >= logging.ERROR:
                message = f"ERROR: {message}"
            elif record.levelno >= logging.WARNING:
                message = f"WARNING: {message}"
        return message


class StandardStreamHandler(logging.StreamHandler):
    """Writes to ``sys.stdout`` or ``sys.stderr`` as it is when a record is
    emitted, so a stream replaced after setup still receives the output."""

    kpip_core_handler = True

    def __init__(self, name: str) -> None:
        self.stream_name = name
        super().__init__()

    @property
    def stream(self) -> object:
        return getattr(sys, self.stream_name)

    @stream.setter
    def stream(self, value: object) -> None:
        del value

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        """Let a closed stdout end the command instead of being reported
        once per record by logging itself."""
        error = sys.exc_info()[1]

        if self.stream_name == "stdout" and isinstance(error, BrokenPipeError):
            raise BrokenStdoutLoggingError() from error

        super().handleError(record)


def _below_warning(record: logging.LogRecord) -> bool:
    return record.levelno < logging.WARNING


def level_for(verbosity: int) -> int:
    """The level ``verbosity`` -- the ``-v`` count less the ``-q`` count --
    lets through, by pip's table."""
    if verbosity >= 2:
        return logging.DEBUG
    if verbosity == 1:
        return VERBOSE
    if verbosity == -1:
        return logging.WARNING
    if verbosity == -2:
        return logging.ERROR
    if verbosity <= -3:
        return logging.CRITICAL
    return logging.INFO


_log_file: str | None = None


def set_log_file(log_file: str | None) -> None:
    """Name the file ``--log`` asked for; the next setup opens it."""
    global _log_file
    _log_file = log_file


def configure_logging(verbosity: int = 0) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "kpip_core_handler", False):
            root.removeHandler(handler)
            if isinstance(handler, logging.FileHandler):
                handler.close()
    level = level_for(verbosity)
    formatter = KpipFormatter()
    output = StandardStreamHandler("stdout")
    output.setLevel(level)
    output.addFilter(_below_warning)
    output.setFormatter(formatter)
    errors = StandardStreamHandler("stderr")
    errors.setLevel(max(level, logging.WARNING))
    errors.setFormatter(formatter)
    root.addHandler(output)
    root.addHandler(errors)
    if _log_file is not None:
        file_handler = logging.FileHandler(_log_file)
        setattr(file_handler, "kpip_core_handler", True)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        level = logging.DEBUG
    root.setLevel(level)
