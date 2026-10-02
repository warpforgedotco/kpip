"""The command's index options, handed to the kpip filling a build environment.

Apart from ``kpip.build.build_backend`` so that every requirement command,
which exports them, does not import the build machinery to do it: a command
whose requirements are all wheels never builds.
"""

from __future__ import annotations

import json
import os


BUILD_INDEX_OPTIONS_VARIABLE = "KPIP_BUILD_INDEX_OPTIONS"
"""The command's index options, for the kpip filling a build environment."""


def export_build_index_options(
    *,
    index_url: str | None,
    extra_index_urls: list[str],
    trusted_hosts: list[str],
    cert: str | None,
    client_cert: str | None,
    pre: bool,
) -> None:
    """Hand the command's index options to the kpip filling a build
    environment.

    pip installs a build environment's requirements from the indexes, trusted
    hosts and certificates the command was given, from the command line or a
    requirements file, and with its ``--pre``. Left to its own configuration,
    the kpip doing it would fetch them from pypi.org: a private
    ``setuptools`` would come from the public index, and an air-gapped
    machine could not build at all. Kept in the environment, as
    ``KPIP_FIND_LINKS`` is, for the builds this command starts.
    """
    arguments = [
        *(["--index-url", index_url] if index_url else []),
        *(
            argument
            for url in extra_index_urls
            for argument in ("--extra-index-url", url)
        ),
        *(argument for host in trusted_hosts for argument in ("--trusted-host", host)),
        # The build environment is filled from the project's directory.
        *(["--cert", os.path.abspath(cert)] if cert else []),
        *(["--client-cert", os.path.abspath(client_cert)] if client_cert else []),
        *(["--pre"] if pre else []),
    ]

    if arguments:
        os.environ[BUILD_INDEX_OPTIONS_VARIABLE] = json.dumps(arguments)

    else:
        os.environ.pop(BUILD_INDEX_OPTIONS_VARIABLE, None)


def _build_index_arguments(environment: dict[str, str]) -> list[str]:
    """The index options ``export_build_index_options`` left in
    ``environment``."""
    value = environment.get(BUILD_INDEX_OPTIONS_VARIABLE)

    if not value:
        return []

    arguments = json.loads(value)

    return [argument for argument in arguments if isinstance(argument, str)]
