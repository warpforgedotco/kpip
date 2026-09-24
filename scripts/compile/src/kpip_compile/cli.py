from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from kpip_compile.build import DEFAULT_OUTPUT_DIR, BuildOptions, build
from kpip_compile.vendor import (
    NUITKA_BRANCH,
    STAMP_NAME,
    VENDOR_DIR,
    PatchError,
    default_spec,
    vendor_nuitka,
)


def _vendor(*, force: bool) -> Path:
    try:
        spec = default_spec()
    except subprocess.CalledProcessError:
        # Offline: an earlier checkout still builds, just not the latest one.
        if not force and (VENDOR_DIR / STAMP_NAME).exists():
            print(
                f"warning: cannot resolve Nuitka {NUITKA_BRANCH}, "
                f"using the existing checkout in {VENDOR_DIR}",
                file=sys.stderr,
            )
            return VENDOR_DIR
        raise
    return vendor_nuitka(spec, VENDOR_DIR, force=force)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kpip-compile",
        description="Compile kpip with a vendored, patched Nuitka.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    vendor = commands.add_parser(
        "vendor", help="Fetch the latest Nuitka develop and apply the patches."
    )
    vendor.add_argument(
        "--force", action="store_true", help="Refetch even if the checkout is current."
    )

    build_parser = commands.add_parser(
        "build",
        help="Compile kpip, vendoring Nuitka first if needed.",
        description="Arguments after '--' go to Nuitka unchanged.",
    )
    build_parser.add_argument(
        "--python",
        default=sys.executable,
        help="Interpreter to compile with; the binary embeds its Python version.",
    )
    build_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    build_parser.add_argument(
        "--mode", choices=("onefile", "standalone"), default="onefile"
    )
    build_parser.add_argument(
        "--cache-mode",
        choices=("cached", "temporary"),
        default="cached",
        help="Onefile unpacking: reuse a cache directory, or unpack on every run.",
    )
    build_parser.add_argument("nuitka_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        nuitka_dir = _vendor(force=args.command == "vendor" and args.force)
    except PatchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.command == "vendor":
        print(nuitka_dir)
        return 0

    extra_args = tuple(args.nuitka_args)
    if extra_args[:1] == ("--",):
        extra_args = extra_args[1:]
    options = BuildOptions(
        python=args.python,
        output_dir=args.output_dir.resolve(),
        mode=args.mode,
        cache_mode=args.cache_mode,
        extra_args=extra_args,
    )
    return build(options, nuitka_dir)


if __name__ == "__main__":
    raise SystemExit(main())
