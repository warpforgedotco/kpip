"""Module loggers that do not import ``logging`` in order to be declared.

``import logging`` is among the most expensive imports left on a quiet
command. It pulls in ``traceback``, which from Python 3.14 pulls in
``_colorize``, and ``dataclasses`` and ``inspect`` behind that: close to ten
milliseconds, spent so that a run which emits no records at all can declare
the loggers it will not use.

A module asks for its logger here instead and is given a stand-in that
fetches the real one the first time it has something to say. Debug and info
records are dropped while nothing in the process has imported ``logging``,
because nothing that could handle them can exist yet -- no handler, no
configuration, not even ``lastResort``. Anything at warning or above always
resolves, so a message that would have reached ``logging.lastResort`` on
stderr still reaches it.
"""

from __future__ import annotations

import sys

# ``logging.WARNING``, spelled here so that asking about a level does not
# import the module this exists to avoid importing.
_WARNING = 30

TYPE_CHECKING = False

if TYPE_CHECKING:
    from logging import Logger
    from typing import Any


class ModuleLogger:
    """One module's logger, fetched when it is first needed."""

    __slots__ = ("_name", "_target")

    def __init__(self, name: str) -> None:
        self._name = name
        self._target: Logger | None = None

    def _resolve(self) -> Logger:
        target = self._target

        if target is None:
            import logging

            target = logging.getLogger(self._name)

            self._target = target

        return target

    def _unheard(self) -> bool:
        """Whether a record now could not reach anything."""
        return self._target is None and "logging" not in sys.modules

    def debug(self, *args: Any, **kwargs: Any) -> None:
        if self._unheard():
            return

        self._resolve().debug(*args, **kwargs)

    def info(self, *args: Any, **kwargs: Any) -> None:
        if self._unheard():
            return

        self._resolve().info(*args, **kwargs)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._resolve().warning(*args, **kwargs)

    def error(self, *args: Any, **kwargs: Any) -> None:
        self._resolve().error(*args, **kwargs)

    def exception(self, *args: Any, **kwargs: Any) -> None:
        self._resolve().exception(*args, **kwargs)

    def critical(self, *args: Any, **kwargs: Any) -> None:
        self._resolve().critical(*args, **kwargs)

    def log(self, *args: Any, **kwargs: Any) -> None:
        self._resolve().log(*args, **kwargs)

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - logging's spelling
        if self._unheard():
            # The same answer the emit methods above act on: with nothing
            # configured, a warning still reaches ``lastResort`` on stderr
            # and anything below it goes nowhere. Reporting every level as
            # disabled would have let ``if isEnabledFor(WARNING)`` drop a
            # warning that calling ``warning()`` outright would print.
            return level >= _WARNING

        return self._resolve().isEnabledFor(level)


def get_logger(name: str) -> ModuleLogger:
    """This module's logger, without importing ``logging`` to say so."""
    return ModuleLogger(name)
