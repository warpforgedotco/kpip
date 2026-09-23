"""The argparse subclasses every command parser is built from.

Split out of ``cli.common`` so that importing it is a decision a command
makes, not a toll the entrypoint pays: ``argparse`` costs several
milliseconds and no route that only resolves a command name needs it.

The base classes here are evaluated at class-creation time, so this module
cannot be made lazy -- separating it is the only way to keep its cost off the
startup path.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any, NoReturn


_terminal_columns: int | None = None


def terminal_columns() -> int:
    """``shutil.get_terminal_size().columns``, once, and without ``shutil``.

    ``HelpFormatter.__init__`` asks for the terminal width whenever it is
    built without an explicit one, and ``add_argument`` builds one *per
    argument* to validate the metavar against ``nargs``. So constructing a
    single command parser imports ``shutil`` -- and ``zlib``, ``bz2`` and
    ``lzma`` behind it, none of which argparse uses -- and issues one
    terminal-size ``ioctl`` per option it declares.

    The width cannot change inside one run, so it is read once here, by the
    same rule ``shutil.get_terminal_size`` uses: ``COLUMNS`` wins when it
    parses to a positive integer, otherwise the real terminal, otherwise 80.
    """

    global _terminal_columns

    if _terminal_columns is None:
        try:
            columns = int(os.environ["COLUMNS"])
        except (KeyError, ValueError):
            columns = 0

        if columns <= 0:
            stream = sys.__stdout__
            columns = 80

            if stream is not None:
                try:
                    columns = os.get_terminal_size(stream.fileno()).columns
                except (AttributeError, ValueError, OSError):
                    columns = 80

        _terminal_columns = columns

    return _terminal_columns


class _Uncoloured:
    """The colours argparse asks a theme for when nothing will render them.

    Every field of the real no-colour theme is the empty string, so one
    object that answers to any of them stands in for all of them and keeps
    this module from having to track which fields argparse reads.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> str:
        return ""


_UNCOLOURED = _Uncoloured()


def _as_written(text: str) -> str:
    return text


# Whether ``_theme`` can be assigned at all. It is a plain attribute on
# 3.14 and a read-only property from 3.15, where argparse keeps the colour
# import off this path without any help.
_THEME_IS_WRITABLE = not isinstance(
    getattr(argparse.HelpFormatter, "_theme", None),
    property,
)


def colour_is_possible() -> bool:
    """Whether anything downstream could render colour.

    Deliberately generous, and only in one direction: saying yes costs an
    import that ``can_colorize`` then settles properly, while saying no is
    final. So every case this is unsure about -- a ``PYTHON_COLORS`` value
    that is neither "0" nor "1", ``FORCE_COLOR`` losing to ``NO_COLOR``,
    ``-E`` changing which variables count, a stdout whose ``isatty`` lies
    about a missing ``fileno`` -- answers yes and defers.

    The one case that has to be exact is the no: ``NO_COLOR`` suppresses
    colour only when it is *non-empty*, which is how ``can_colorize`` reads
    it, and an empty one has to fall through to the terminal like any other
    unset variable.
    """
    if os.environ.get("PYTHON_COLORS") is not None:
        return True

    if os.environ.get("FORCE_COLOR"):
        return True

    if os.environ.get("NO_COLOR"):
        return False

    if os.environ.get("TERM") == "dumb":
        return False

    try:
        return sys.stdout.isatty()

    except (AttributeError, ValueError):
        # A replaced or closed stdout cannot be shown colour either.
        return False


class HelpFormatter(argparse.HelpFormatter):
    """Keep option metavar placement stable across supported Python versions."""

    def __init__(
        self,
        prog: str,
        indent_increment: int = 2,
        max_help_position: int = 24,
        width: int | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            prog,
            indent_increment,
            max_help_position,
            terminal_columns() - 2 if width is None else width,
            *args,
            **kwargs,
        )

    def _set_color(self, color: bool, *args: Any, **kwargs: Any) -> None:
        """Settle colour without importing the machinery that renders it.

        ``argparse.HelpFormatter`` imports ``_colorize`` here whatever the
        answer turns out to be, and that brings ``dataclasses``, ``inspect``,
        ``dis`` and ``tokenize`` with it -- milliseconds, on the one path
        whose whole job is to print a help page, and spent even when the
        output is a pipe that cannot show a colour. Nothing is given up by
        asking the cheap question first: a terminal that would show colour
        still reaches the real implementation, which decides as it always
        did.

        This is worth doing on exactly one version. Before 3.14 there is no
        colour and no hook to override. From 3.15 argparse defers the import
        itself -- ``_set_color`` only records the setting, and the theme is
        built lazily by a property that short-circuits before touching
        ``_colorize`` -- so the base method is already cheap and its
        ``_theme`` is read-only, which makes writing one an error rather
        than a saving. 3.14 is the version that pays, and the one this
        answers for.
        """
        # Looked up rather than called directly: the base method only exists
        # from 3.14.
        inherited = getattr(super(), "_set_color", None)

        if inherited is None:
            return

        if not _THEME_IS_WRITABLE or (color and colour_is_possible()):
            inherited(color, *args, **kwargs)

            return

        self._theme = _UNCOLOURED
        self._decolor = _as_written

    def _format_action_invocation(self, action: argparse.Action) -> str:
        if not action.option_strings:
            return super()._format_action_invocation(action)
        if action.nargs == 0:
            return ", ".join(action.option_strings)
        default_metavar = (
            action.metavar if isinstance(action.metavar, str) else action.dest.upper()
        )
        metavar = self._format_args(action, default_metavar)
        option_strings = [*action.option_strings[:-1], action.option_strings[-1]]
        if metavar:
            option_strings[-1] += f" {metavar}"
        return ", ".join(option_strings)

    def _format_usage(
        self,
        usage: str | None,
        actions: Iterable[argparse.Action],
        groups: Iterable[argparse._MutuallyExclusiveGroup],
        prefix: str | None,
    ) -> str:
        rendered = super()._format_usage(usage, actions, groups, prefix)
        return re.sub(r"(?m)(^.*\]) -d\n(\s+)DEST", r"\1\n\2-d DEST", rendered)


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", HelpFormatter)
        super().__init__(*args, **kwargs)

    @property
    def _get_validation_formatter(self) -> NoReturn:
        """Skip 3.14's eager metavar/help-string validation on every ``add_argument``.

        ``_ActionsContainer.add_argument`` gates two validation-only
        ``HelpFormatter`` builds on ``hasattr(self, "_get_validation_formatter")``:
        one checks a tuple ``metavar`` against ``nargs``, the other expands
        the help string. The auto-added ``-h``/``--help`` action always has
        help text, so stock ``argparse`` pays for both on every parser
        construction -- building that formatter's ``_set_color`` step
        unconditionally does ``from _colorize import ...``, which drags in
        ``dataclasses`` and, with it,
        ``inspect``/``dis``/``tokenize``/``ast``/``annotationlib``. That's a
        few milliseconds on every kpip invocation to validate strings that
        get the same validation for real the moment help is actually
        formatted. Shadowing the attribute with a property that raises
        ``AttributeError`` makes ``hasattr`` false, so both checks defer to
        that real formatting -- a malformed metavar or ``%`` in a help
        string still raises, just from ``format_help``/``print_help``
        instead of from ``add_argument``.

        A property rather than deleting the base method: this attribute may
        not exist on older Python versions kpip supports, in which case
        nothing consults ``hasattr`` here and this override is simply
        unused.
        """
        raise AttributeError("_get_validation_formatter")

    def error(self, message: str) -> NoReturn:
        if message.startswith("unrecognized arguments: "):
            message = message.removeprefix("unrecognized arguments: ")
            message = f"no such option: {message.split()[0]}"
        usage = self.format_usage().replace("usage:", "Usage:", 1)
        self._print_message(usage, sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")
