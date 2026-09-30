"""Logging setup for commands that produce log output."""

from __future__ import annotations

import logging
import sys


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


def configure_logging(log_file: str | None = None) -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "kpip_core_handler", False):
            root.setLevel(logging.INFO)
            break
    else:
        handler = logging.StreamHandler(sys.stderr)
        setattr(handler, "kpip_core_handler", True)
        handler.setFormatter(KpipFormatter())
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    if log_file is not None:
        file_handler = logging.FileHandler(log_file)
        setattr(file_handler, "kpip_core_log_file", True)
        file_handler.setFormatter(KpipFormatter())
        root.addHandler(file_handler)
    root.setLevel(logging.INFO)
