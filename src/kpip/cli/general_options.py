"""pip's general options: declared on every command, applied as it parses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kpip.core import run_options

if TYPE_CHECKING:
    import argparse
    from typing import Any

# (option strings, keyword arguments). A command that declares one of these
# itself keeps its own declaration.
_GENERAL_OPTIONS: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
    (("-q", "--quiet"), {"action": "count", "default": 0}),
    (("-v", "--verbose"), {"action": "count", "default": 0}),
    (("--isolated",), {"action": "store_true"}),
    (("--debug",), {"action": "store_true"}),
    (("--no-input",), {"action": "store_true"}),
    (("--no-color",), {"action": "store_true"}),
    (("--no-python-version-warning",), {"action": "store_true"}),
    (("--disable-pip-version-check",), {"action": "store_true"}),
    (
        ("--keyring-provider",),
        {"choices": ("auto", "disabled", "import", "subprocess")},
    ),
    (("--proxy",), {}),
    (("--retries",), {"type": int}),
    (("--resume-retries",), {"type": int}),
    (("--timeout", "--default-timeout"), {"type": float}),
    (
        ("--exists-action",),
        {"action": "append", "default": [], "choices": ("s", "i", "w", "b", "a")},
    ),
    (("--trusted-host",), {"action": "append", "default": [], "dest": "trusted_hosts"}),
    (("--cert",), {}),
    (("--client-cert",), {}),
    (("--cache-dir",), {}),
    (("--no-cache-dir",), {"action": "store_true"}),
    (("--use-feature",), {"action": "append", "default": [], "dest": "use_features"}),
    (("--use-deprecated",), {"action": "append", "default": []}),
)


def add_general_options(parser: argparse.ArgumentParser) -> None:
    """Give ``parser`` each general option it does not declare itself."""
    declared = parser._option_string_actions

    for strings, keywords in _GENERAL_OPTIONS:
        if any(string in declared for string in strings):
            continue

        parser.add_argument(*strings, **keywords)


def apply_general_options(options: argparse.Namespace) -> None:
    """Make the parsed general options the running command's."""
    from kpip.cli.logging_config import configure_logging

    configure_logging(int(options.verbose) - int(options.quiet))

    current = run_options.current
    current.cache_dir = options.cache_dir
    current.no_cache_dir = bool(options.no_cache_dir)
    current.cert = options.cert
    current.client_cert = options.client_cert
    current.proxy = options.proxy
    current.retries = getattr(options, "retries", None)
    current.resume_retries = getattr(options, "resume_retries", None)
    current.timeout = getattr(options, "timeout", None)
    current.trusted_hosts = tuple(
        getattr(options, "trusted_hosts", None) or getattr(options, "trusted_host", [])
    )
    current.no_input = bool(options.no_input)
    current.keyring_provider = options.keyring_provider
    current.exists_action = tuple(getattr(options, "exists_action", ()) or ())
    current.isolated = bool(options.isolated)
    current.debug = bool(getattr(options, "debug", False))
    current.check_build_dependencies = bool(
        getattr(options, "check_build_dependencies", False)
    )
    current.no_clean = bool(getattr(options, "no_clean", False))
