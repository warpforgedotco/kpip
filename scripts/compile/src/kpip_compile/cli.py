from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from kpip_compile.build import DEFAULT_OUTPUT_DIR, BuildOptions, build
from kpip_compile.pgo import PgoError
from kpip_compile.vendor import (
    APPLIES,
    NUITKA_BRANCH,
    NUITKA_REPOSITORY,
    PATCHES_DIR,
    STAMP_NAME,
    VENDOR_DIR,
    PatchError,
    VendorSpec,
    check_patches,
    default_spec,
    find_patches,
    resolve_commit,
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


def _check_patches(branch: str) -> int:
    """Print how each patch fares on ``branch`` as a Markdown table.

    Returns 1 when any patch does not apply, so a caller can tell (the table
    still lists them all), and 2 when the check itself could not run.
    """
    patches = find_patches()
    if not patches:
        # A table of nothing would pass without checking anything.
        print(f"error: no patches found in {PATCHES_DIR}", file=sys.stderr)
        return 2
    try:
        commit = resolve_commit(NUITKA_REPOSITORY, branch)
        spec = VendorSpec(repository=NUITKA_REPOSITORY, commit=commit, patches=patches)
        with tempfile.TemporaryDirectory() as work_dir:
            results = check_patches(spec, Path(work_dir) / "nuitka")
    except (subprocess.CalledProcessError, PatchError) as error:
        print(f"error: cannot check Nuitka {branch}: {error}", file=sys.stderr)
        return 2

    print(f"Nuitka `{branch}` at `{commit[:12]}`\n")
    print("| Patch | Status |")
    print("| --- | --- |")
    for patch, status in results:
        print(f"| `{patch.name}` | {status} |")
    return 0 if all(status == APPLIES for _patch, status in results) else 1


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

    check = commands.add_parser(
        "check-patches",
        help="Report how each patch fares on a Nuitka branch, without stopping "
        "at the first that fails.",
    )
    check.add_argument("--branch", default=NUITKA_BRANCH)

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
    build_parser.add_argument(
        "--pgo",
        action="store_true",
        help="Profile-guided optimization with Nuitka's --pgo-c: an instrumented "
        "build, a training run of real kpip commands (needs network), then the "
        "final build.",
    )
    build_parser.add_argument("nuitka_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "check-patches":
        return _check_patches(args.branch)

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
        pgo=args.pgo,
    )
    try:
        return build(options, nuitka_dir)
    except PgoError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
