"""What installing from a pylock takes from it."""

from __future__ import annotations

from pathlib import Path

from kpip.cli.requirements import collect_requirements

LOCK = """\
lock-version = "1.0"
created-by = "test"

[[packages]]
name = "demo"
version = "1.0"
[[packages.wheels]]
name = "demo-1.0-py3-none-any.whl"
url = "https://files.invalid/demo-1.0-py3-none-any.whl"
hashes = {sha256 = "00"}

[[packages]]
name = "elsewhere"
version = "2.0"
marker = "sys_platform == 'no-such-platform'"
[[packages.wheels]]
name = "elsewhere-2.0-py3-none-any.whl"
url = "https://files.invalid/elsewhere-2.0-py3-none-any.whl"
hashes = {sha256 = "00"}
"""


def collect(tmp_path: Path, *requirements: str, constraints: bool = False):
    lock = tmp_path / "pylock.toml"
    lock.write_text(LOCK, encoding="utf-8")
    constraint_files = []
    if constraints:
        constraint = tmp_path / "constraints.txt"
        constraint.write_text("demo<2\n", encoding="utf-8")
        constraint_files.append(str(constraint))
    return collect_requirements(
        requirements=list(requirements),
        requirement_files=[str(lock)],
        constraint_files=constraint_files,
    )


def test_a_package_for_another_environment_is_left_out(tmp_path: Path) -> None:
    bundle = collect(tmp_path)

    assert list(bundle.locked_links) == ["demo"]
    assert bundle.requirements == ["demo==1.0"]


def test_a_lock_alone_resolves_nothing(tmp_path: Path) -> None:
    assert collect(tmp_path).only_locked


def test_anything_beside_the_lock_is_resolved(tmp_path: Path) -> None:
    assert not collect(tmp_path, "other").only_locked
    assert not collect(tmp_path, constraints=True).only_locked
