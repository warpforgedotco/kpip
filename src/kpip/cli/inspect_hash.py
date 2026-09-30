"""Implementation of the ``kpip hash`` subcommand."""

from __future__ import annotations


def run_hash(args: list[str]) -> int:
    import hashlib
    import os

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
        print(
            f"{os.path.basename(filename)}: --hash={options.algorithm}:{digest.hexdigest()}",
        )
    return 0
