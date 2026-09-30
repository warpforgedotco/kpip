"""The argparse subclasses every command parser is built from."""

from __future__ import annotations

import argparse
import re
import sys

from typing import TYPE_CHECKING

from kpip.cli.general_options import add_general_options, apply_general_options

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any, NoReturn


class HelpFormatter(argparse.HelpFormatter):
    """Keep option metavar placement stable across supported Python versions."""

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

    def parse_args(self, args: Any = None, namespace: Any = None) -> Any:
        """Parse as pip does.

        Every command takes pip's general options, which take effect from
        here on, and positional arguments may come before, between and after
        options.
        """
        add_general_options(self)

        if any(
            action.nargs in (argparse.PARSER, argparse.REMAINDER)
            for action in self._actions
        ):
            # argparse cannot intermix a subcommand or a remainder.
            parsed = super().parse_args(args, namespace)

        else:
            parsed = self.parse_intermixed_args(args, namespace)

        apply_general_options(parsed)

        return parsed

    def error(self, message: str) -> NoReturn:
        if message.startswith("unrecognized arguments: "):
            message = message.removeprefix("unrecognized arguments: ")
            message = f"no such option: {message.split()[0]}"
        usage = self.format_usage().replace("usage:", "Usage:", 1)
        self._print_message(usage, sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")
