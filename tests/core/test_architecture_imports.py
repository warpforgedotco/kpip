from __future__ import annotations

import ast
from pathlib import Path


ALLOWED_IMPORTS = {
    "core": frozenset(),
    "host": frozenset({"core"}),
    "build": frozenset({"core", "host"}),
    "index": frozenset({"core", "build", "host"}),
    "network": frozenset({"core", "host", "build", "index"}),
    "vcs": frozenset({"core"}),
    "resolution": frozenset({"core", "index", "network", "vcs"}),
    "install": frozenset(
        {"core", "host", "build", "index", "network", "vcs", "resolution"},
    ),
    "cli": frozenset(
        {
            "core",
            "host",
            "build",
            "index",
            "network",
            "vcs",
            "resolution",
            "install",
        },
    ),
}

KNOWN_DEBT = frozenset(
    {
        ("build/build_backend.py", "install"),
        ("resolution/inputs.py", "install"),
        ("resolution/api.py", "install"),
        ("resolution/req_install.py", "build"),
    },
)


def test_first_party_imports_follow_architecture() -> None:
    package_root = Path(__file__).parents[2] / "src" / "kpip"
    violations: list[str] = []
    observed_debt: set[tuple[str, str]] = set()
    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root)
        owner = relative.parts[0]
        if owner not in ALLOWED_IMPORTS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                modules = (node.module or "",)
            elif isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            else:
                continue
            for module in modules:
                if not module.startswith("kpip."):
                    continue
                target = module.split(".", 2)[1]
                if target in {owner, "_vendor"} or target in ALLOWED_IMPORTS[owner]:
                    continue
                debt_key = (relative.as_posix(), target)
                if debt_key in KNOWN_DEBT:
                    observed_debt.add(debt_key)
                    continue
                violations.append(
                    f"{relative}:{node.lineno}: {owner} may not import {target}",
                )

    assert not violations, "\n" + "\n".join(violations)
    assert observed_debt == KNOWN_DEBT, (
        "remove stale architecture debt exceptions: "
        f"{sorted(KNOWN_DEBT - observed_debt)}"
    )


def test_no_module_shadows_a_standard_library_name() -> None:
    """No kpip module or package is named after one in the standard library.

    Absolute imports make this safe inside CPython, which is why it went
    unnoticed. It stops being safe the moment something flattens the
    namespace: compiling the package with Nuitka as a loose script made
    ``kpip/platform/`` answer to a bare ``import platform``, and
    ``kpip lock`` died on macOS with ``module 'platform' has no attribute
    'machine'``.

    The rule is cheap to keep and the failure it prevents is baffling to
    diagnose, so it is asserted rather than remembered.
    """
    import sys

    root = Path(__file__).resolve().parents[2] / "src" / "kpip"
    standard = sys.stdlib_module_names

    shadows = sorted(
        str(path.relative_to(root.parent))
        for path in root.rglob("*")
        if "_vendor" not in path.parts
        and "__pycache__" not in path.parts
        and (
            (
                path.is_dir()
                and (path / "__init__.py").is_file()
                and path.name in standard
            )
            or (
                path.suffix == ".py"
                and path.name != "__init__.py"
                and path.stem in standard
            )
        )
    )

    assert shadows == [], shadows
