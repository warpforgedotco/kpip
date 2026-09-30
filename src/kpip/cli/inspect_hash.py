"""Implementation of the ``kpip hash`` subcommand."""

from __future__ import annotations

from kpip.core.logger import get_logger

logger = get_logger(__name__)


def run_hash(args: list[str]) -> int:
    import hashlib

    from kpip.cli.parsers.inspection import create_hash_parser

    options = create_hash_parser().parse_args(args)
    for filename in options.files:
        digest = hashlib.new(options.algorithm)
        with open(filename, "rb") as file:
            while True:
                block = file.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        # As pip writes it: the path as given, then the option on a line of
        # its own, ready for a requirements file.
        logger.info(f"{filename}:\n--hash={options.algorithm}:{digest.hexdigest()}")
    return 0
