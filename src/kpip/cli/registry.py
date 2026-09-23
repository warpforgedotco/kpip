from __future__ import annotations

from types import ModuleType

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Callable

    from kpip.cli.parser import ArgumentParser

    CommandRunner = Callable[[list[str]], int]

    ParserFactory = Callable[[], ArgumentParser]


class CommandSpec:
    """How to reach one command without importing the rest of them.

    Both module paths are strings, so a spec costs nothing until dispatch
    needs it.  ``parser_module`` is separate from ``module_path`` on purpose:
    ``kpip install --help`` has to build a parser but must not load the
    installer to do it.
    """

    __slots__ = (
        "_module",
        "_parser_module",
        "module_path",
        "name",
        "needs_execution_context",
        "needs_logging",
        "needs_tempdir",
        "parser_factory",
        "parser_module_path",
        "runner",
        "visible",
    )

    def __init__(
        self,
        name: str,
        module_path: str,
        runner: str | None = "run",
        parser_factory: str | None = "create_parser",
        visible: bool = True,
        needs_logging: bool = True,
        needs_tempdir: bool = True,
        needs_execution_context: bool = True,
        parser_module_path: str | None = None,
    ) -> None:
        self.name = name
        self.module_path = module_path
        self.parser_module_path = parser_module_path or f"kpip.cli.parsers.{name}"
        self._module: ModuleType | None = None
        self._parser_module: ModuleType | None = None
        self.runner = runner
        self.parser_factory = parser_factory
        self.visible = visible
        self.needs_logging = needs_logging
        self.needs_tempdir = needs_tempdir
        self.needs_execution_context = needs_execution_context

    @property
    def module(self) -> ModuleType:
        if self._module is None:
            from importlib import import_module

            self._module = import_module(self.module_path)
        return self._module

    @property
    def parser_module(self) -> ModuleType:
        if self._parser_module is None:
            from importlib import import_module

            self._parser_module = import_module(self.parser_module_path)
        return self._parser_module

    def load_runner(self) -> CommandRunner | None:
        if self.runner is None:
            return None

        return getattr(self.module, self.runner)

    def create_parser(self) -> ArgumentParser:
        if self.parser_factory is not None:
            factory: ParserFactory = getattr(self.parser_module, self.parser_factory)

            return factory()

        from kpip.cli.parser import ArgumentParser

        return ArgumentParser(prog=f"kpip {self.name}")


COMMAND_SPECS = (
    CommandSpec("install", "kpip.cli.install", "run_install"),
    CommandSpec("wheel", "kpip.cli.wheel", "run_wheel"),
    CommandSpec("index", "kpip.cli.index", "run_index"),
    CommandSpec("download", "kpip.cli.download", "run_download"),
    CommandSpec("uninstall", "kpip.cli.uninstall", "run_uninstall"),
    CommandSpec(
        "list",
        "kpip.cli.list",
        "run_list",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "freeze",
        "kpip.cli.freeze",
        "run_freeze",
        needs_tempdir=False,
    ),
    CommandSpec(
        "show",
        "kpip.cli.inspect_show",
        "run_show",
        parser_factory="create_show_parser",
        parser_module_path="kpip.cli.parsers.inspection",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "inspect",
        "kpip.cli.inspection",
        "run_inspect",
        parser_factory="create_inspect_parser",
        parser_module_path="kpip.cli.parsers.inspection",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "hash",
        "kpip.cli.inspect_hash",
        "run_hash",
        parser_factory="create_hash_parser",
        parser_module_path="kpip.cli.parsers.inspection",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "check",
        "kpip.cli.inspect_check",
        "run_check",
        parser_factory="create_check_parser",
        parser_module_path="kpip.cli.parsers.inspection",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "cache",
        "kpip.cli.cache",
        "run_cache",
        needs_logging=False,
        needs_tempdir=False,
    ),
    CommandSpec(
        "lock",
        "kpip.cli.lock",
        "run_lock",
        needs_execution_context=False,
    ),
    CommandSpec(
        "help",
        "kpip.cli.entrypoint",
        runner=None,
        parser_factory=None,
        visible=False,
        needs_logging=False,
        needs_tempdir=False,
    ),
)


COMMANDS_internal = {spec.name: spec for spec in COMMAND_SPECS}


def get_command(command: str) -> CommandSpec | None:
    return COMMANDS_internal.get(command)
