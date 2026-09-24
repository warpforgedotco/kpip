"""Shared serialization and output for the lock commands.

Imported by both ``cli.lock`` and the ``cli.fast.lock`` fast path, so this
module deliberately imports nothing.
"""

from __future__ import annotations

LOCK_HEADER = ('created-by = "kpip"', 'lock-version = "1.0"', "")


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_lock_output(output: str, rendered: str) -> None:
    """Write a rendered lock to ``output``, or to stdout for ``-``."""

    if output == "-":
        print(rendered, end="")

    else:
        with open(output, "w", encoding="utf-8") as output_file:
            output_file.write(rendered)


def read_previous_lock(output: str, upgrade: bool) -> bytes | None:
    """The lock last written to ``output``, which this one starts from.

    None when there is none to start from: ``--upgrade`` asked to start
    afresh, the lock goes to stdout, or nothing is there yet.
    """

    if upgrade or output == "-":
        return None

    try:
        with open(output, "rb") as file:
            return file.read()
    except OSError:
        return None


def lock_left_behind(output: str, upgrade: bool, rendered: str) -> bytes | None:
    """What :func:`read_previous_lock` finds once ``rendered`` is written."""

    if upgrade or output == "-":
        return None

    return rendered.encode("utf-8")


def previous_lock_digest(previous: bytes | None, upgrade_packages: list[str]) -> str:
    """What a cached lock keys on for the lock it started from.

    A lock is only a function of its inputs given the pins it prefers, so a
    replayed or cached answer must have started from the same ones.
    """

    if previous is None:
        return ""

    from kpip.network.freshness import sha224_hexdigest

    return sha224_hexdigest(previous) + "\0" + "\0".join(sorted(upgrade_packages))


def lock_preferences(
    previous: bytes | None, upgrade_packages: list[str]
) -> dict[str, str]:
    """The version each package had in ``previous``, by canonical name.

    Packages named by ``--upgrade-package`` are left out, so they resolve as
    if there were no previous lock. A lock that cannot be read is no reason
    to fail: this one is resolved from scratch instead.
    """

    if previous is None:
        return {}

    from kpip.core.names import canonicalize_name
    from kpip.resolution.files.pylock import tomllib

    upgraded = {canonicalize_name(name) for name in upgrade_packages}

    try:
        lock = tomllib.loads(previous.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}

    packages = lock.get("packages")
    preferences: dict[str, str] = {}

    for package in packages if isinstance(packages, list) else ():
        if not isinstance(package, dict):
            continue

        name = package.get("name")
        version = package.get("version")

        if isinstance(name, str) and isinstance(version, str):
            canonical = canonicalize_name(name)

            if canonical not in upgraded:
                preferences[canonical] = version

    return preferences


def render_wheel_lock(packages: list[tuple[str, str, str, str, str]]) -> str:
    lines = list(LOCK_HEADER)
    for name, version, wheel_name, wheel_url, digest in packages:
        lines.extend(
            (
                "[[packages]]",
                f"name = {toml_string(name)}",
                f"version = {toml_string(version)}",
                "[[packages.wheels]]",
                f"name = {toml_string(wheel_name)}",
                f"url = {toml_string(wheel_url)}",
                "[packages.wheels.hashes]",
                f"sha256 = {toml_string(digest)}",
                "",
            ),
        )
    return "\n".join(lines)
