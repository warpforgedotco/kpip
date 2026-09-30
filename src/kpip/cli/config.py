from __future__ import annotations

from typing import TYPE_CHECKING
lazy import configparser
lazy import os
lazy import sys

lazy from kpip.core import run_options
lazy from kpip.core.appdirs import user_config_dir
lazy from kpip.core.errors import ConfigurationError
lazy from kpip.index.config import DEFAULT_INDEX_URL

if TYPE_CHECKING:
    import argparse

NO_INDEX_VALUES = frozenset(("1", "true", "yes", "on"))

CONFIG_BASENAME = "kpip.conf" if os.name != "nt" else "kpip.ini"


class RawConfigParser_internal(configparser.RawConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


class ConfigLocation:
    __slots__ = ("kind", "path")

    def __init__(self, kind: str, path: str) -> None:
        self.kind = kind
        self.path = path


class ConfigurationStore:
    def __init__(self) -> None:
        self.parser_internal: configparser.RawConfigParser | None = None

    def load(self) -> None:
        # --isolated, as pip reads it: the user's own configuration and the
        # environment are left out, the machine's and the environment's
        # files are not.
        isolated = run_options.current.isolated
        paths = [
            os.fspath(location.path)
            for location in config_locations()
            if os.path.isfile(os.fspath(location.path))
            and not (isolated and location.kind in {"user", "env"})
        ]
        if not paths:
            return

        self.parser_internal = new_parser()
        for path in paths:
            try:
                self.parser_internal.read(path, encoding="utf-8")
            except configparser.Error as exc:
                raise ConfigurationError(
                    f"Configuration file could not be loaded.\n{exc}"
                ) from exc

    def get(self, key: str) -> str:
        section, option = split_key(key)
        parser = self.parser_internal
        if parser is not None:
            for candidate in option_spellings(option):
                if parser.has_option(section, candidate):
                    return parser.get(section, candidate)
        raise ConfigurationError(f"No such key - {key}")

    def get_optional(self, key: str) -> str | None:
        try:
            return self.get(key)
        except ConfigurationError:
            return None


def config_locations() -> list[ConfigLocation]:
    config_dirs = os.environ.get("XDG_CONFIG_DIRS")
    if config_dirs and config_dirs.split(os.pathsep)[0]:
        global_path = os.path.join(
            config_dirs.split(os.pathsep)[0],
            "kpip",
            CONFIG_BASENAME,
        )
    else:
        global_path = os.path.join("/etc", "kpip.conf")
    env_config = os.environ.get("KPIP_CONFIG_FILE")
    # As pip documents for PIP_CONFIG_FILE: os.devnull turns every
    # configuration file off, and a file that exists stands in for the user
    # file.
    if env_config == os.devnull:
        return []
    env_path = os.path.expanduser(env_config) if env_config else None
    locations = [ConfigLocation("global", global_path)]
    if not (env_path and os.path.exists(env_path)):
        locations.extend(ConfigLocation("user", path) for path in user_config_paths())
    prefix = os.environ.get("VIRTUAL_ENV") or sys.prefix
    executable_prefix = os.path.dirname(os.path.dirname(sys.executable))
    if os.path.isfile(os.path.join(executable_prefix, "pyvenv.cfg")):
        prefix = executable_prefix
    locations.append(ConfigLocation("site", os.path.join(prefix, CONFIG_BASENAME)))
    if env_path:
        locations.append(ConfigLocation("env", env_path))
    return locations


def split_key(key: str) -> tuple[str, str]:
    if "." not in key:
        raise ConfigurationError(
            "Key does not contain dot separated section and key. "
            "Perhaps you wanted to use 'global.index-url' instead?",
        )
    section, option = key.split(".", 1)
    if not section or not option:
        raise ConfigurationError(f"Invalid configuration key: {key}")
    return section, option


def option_spellings(option: str) -> tuple[str, ...]:
    dotted = option.replace("_", "-")
    underscored = option.replace("-", "_")
    if dotted == underscored:
        return (dotted,)
    return (dotted, underscored)


def new_parser() -> configparser.RawConfigParser:
    return RawConfigParser_internal()


def user_config_paths() -> list[str]:
    """The user configuration files, the later overriding the earlier.

    The file lives in the platform's user config directory, as pip's does:
    ``$XDG_CONFIG_HOME/kpip`` or ``~/.config/kpip`` on Linux, the
    Application Support directory on macOS, ``%APPDATA%`` on Windows. It
    used to be ``~/.config/kpip.conf`` unless XDG_CONFIG_HOME was set, and
    ``$XDG_CONFIG_HOME/kpip/kpip.conf`` when it was -- two files for one
    home directory -- so both are still read, first.
    """

    paths = [os.path.join(os.path.expanduser("~"), ".config", CONFIG_BASENAME)]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        # Where it was read from with XDG_CONFIG_HOME set, on every platform;
        # macOS's own directory ignores the variable.
        paths.append(os.path.join(xdg, "kpip", CONFIG_BASENAME))
    paths.append(os.path.join(user_config_dir("kpip"), CONFIG_BASENAME))
    return list(dict.fromkeys(paths))


class SourceConfig:
    """Where a command looks for distributions."""

    __slots__ = ("extra_index_urls", "find_links", "index_url", "no_index")

    def __init__(
        self,
        find_links: list[str],
        index_url: str | None,
        extra_index_urls: list[str],
        no_index: bool,
    ) -> None:
        self.find_links = find_links

        self.index_url = index_url

        self.extra_index_urls = extra_index_urls

        self.no_index = no_index


def load_source_config(command: str | None = None) -> SourceConfig:
    """Read configured sources for ``command``, then apply ``KPIP_*`` overrides."""

    store = ConfigurationStore()

    # A file that does not parse is an error, not an empty configuration:
    # dropping it would drop its valid settings too, and an install that
    # was told ``no-index`` would quietly go to PyPI.
    store.load()

    def configured(option: str) -> str | None:
        if command is not None:
            value = store.get_optional(f"{command}.{option}")

            if value is not None:
                return value

        return store.get_optional(f"global.{option}")

    raw_find_links = configured("find-links")

    find_links = (
        []
        if raw_find_links is None
        else [line.strip() for line in raw_find_links.splitlines() if line.strip()]
    )

    index_url = configured("index-url") or DEFAULT_INDEX_URL

    raw_extra_index_urls = configured("extra-index-url")

    extra_index_urls = (
        []
        if raw_extra_index_urls is None
        else [
            line.strip() for line in raw_extra_index_urls.splitlines() if line.strip()
        ]
    )

    no_index_value = configured("no-index")

    no_index = (
        no_index_value is not None and no_index_value.strip().lower() in NO_INDEX_VALUES
    )

    if run_options.current.isolated:
        return SourceConfig(find_links, index_url, extra_index_urls, no_index)

    if (value := os.environ.get("KPIP_FIND_LINKS")) is not None:
        find_links = value.split()

    if (value := os.environ.get("KPIP_INDEX_URL")) is not None:
        index_url = value

    if (value := os.environ.get("KPIP_EXTRA_INDEX_URL")) is not None:
        extra_index_urls = value.split()

    if (value := os.environ.get("KPIP_NO_INDEX")) is not None:
        no_index = value.strip().lower() in NO_INDEX_VALUES

    return SourceConfig(find_links, index_url, extra_index_urls, no_index)


def resolve_sources(
    options: argparse.Namespace,
    config: SourceConfig,
) -> SourceConfig:
    """Apply command-line source options over configured defaults.

    ``install`` deliberately does not use this: it concatenates configured and
    command-line find-links and gates the index URL on whether one was given
    explicitly.  See ``install_options.requirement_bundle``.
    """

    find_links = getattr(options, "find_links", None) or config.find_links

    return SourceConfig(
        find_links,
        options.index_url or config.index_url,
        options.extra_index_url or config.extra_index_urls,
        options.no_index or config.no_index,
    )
