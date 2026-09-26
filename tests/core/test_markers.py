"""PEP 508 marker parsing and evaluation, against the reference library.

The evaluator this replaced split the marker text on ``or`` and then on
``and``, which cannot see grouping: ``(a or b) and c`` split into ``(a`` and
``b) and c``. These tests pin the cases that broke, and a differential
against ``packaging`` covers the rest of the grammar.
"""

from __future__ import annotations

import packaging.markers
import pytest
from kpip.core.markers import (
    InvalidMarker,
    UndefinedEnvironmentName,
    canonical_marker,
    evaluate_marker,
    format_marker,
    marker_matches,
    parse_marker,
)
from kpip.core.packaging import marker_applies
from packaging.markers import Marker

LINUX = {
    "implementation_name": "cpython",
    "implementation_version": "3.11.9",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "6.1.0",
    "platform_system": "Linux",
    "platform_version": "#1 SMP",
    "python_full_version": "3.11.9",
    "python_version": "3.11",
    "sys_platform": "linux",
    "extra": "",
}

ENVIRONMENTS = {
    "linux-x86_64-py3.11": LINUX,
    "linux-aarch64-py3.9": {
        **LINUX,
        "platform_machine": "aarch64",
        "python_version": "3.9",
        "python_full_version": "3.9.18",
        "implementation_version": "3.9.18",
    },
    "win32-AMD64-py3.12": {
        **LINUX,
        "os_name": "nt",
        "platform_machine": "AMD64",
        "platform_system": "Windows",
        "sys_platform": "win32",
        "python_version": "3.12",
        "python_full_version": "3.12.4",
        "implementation_version": "3.12.4",
    },
    "darwin-arm64-py3.13": {
        **LINUX,
        "platform_machine": "arm64",
        "platform_system": "Darwin",
        "sys_platform": "darwin",
        "python_version": "3.13",
        "python_full_version": "3.13.2",
        "implementation_version": "3.13.2",
    },
    "linux-x86_64-pypy": {
        **LINUX,
        "implementation_name": "pypy",
        "implementation_version": "7.3.16",
        "platform_python_implementation": "PyPy",
    },
}

MARKERS = [
    # Grouping: the whole reason this module exists.
    '(sys_platform == "linux" or sys_platform == "darwin") and python_version >= "3.8"',
    'python_version >= "3.8" and (sys_platform == "linux" or sys_platform == "darwin")',
    '(python_version < "3.13" and platform_system == "Linux") and extra == "test"',
    'extra == "dev" and (python_version >= "3.7" and python_version < "3.11")',
    'platform_machine == "aarch64" or (platform_machine == "ppc64le" or '
    '(platform_machine == "x86_64" or platform_machine == "AMD64"))',
    '(sys_platform == "win32" or sys_platform == "emscripten") and extra == "all"',
    'os_name == "posix" or (os_name == "nt" and python_version >= "3.9")',
    '((os_name == "posix"))',
    # Precedence without parentheses: `and` binds tighter than `or`.
    'sys_platform == "nope" and python_version >= "3.0" or os_name == "posix"',
    'os_name == "posix" or sys_platform == "nope" and python_version >= "99"',
    # `in` / `not in` are Python containment on the literal.
    'platform_machine in "x86_64 aarch64"',
    'platform_machine not in "x86_64 aarch64"',
    'sys_platform in "linux darwin"',
    'python_version in "3.9 3.10 3.11"',
    # Either side may be the variable.
    '"linux" == sys_platform',
    '"3.8" <= python_version',
    '"x86_64" != platform_machine',
    '"arm" in platform_machine',
    '"win" not in sys_platform',
    # Legacy dotted spellings and the implementation alias.
    'os.name == "posix"',
    'sys.platform == "linux"',
    'platform.machine == "x86_64"',
    'platform.python_implementation == "CPython"',
    'python_implementation == "CPython"',
    # Version-valued variables use PEP 440; everything else compares as text.
    'python_full_version == "3.11"',
    'python_full_version >= "3.10"',
    'python_version > "3.9"',
    'implementation_version >= "7.0"',
    'platform_release > "5.0"',
    'sys_platform > "a"',
    'sys_platform >= "linux"',
    'platform_machine <= "x86_64"',
    # Quoting variants.
    "sys_platform == 'linux'",
    'os_name=="posix"',
    "os_name  ==  'posix'",
    # Extras, including PEP 685 normalisation of the literal.
    'extra == "test"',
    'extra != "test"',
    'extra == "Test.Dev"',
    'extra in "gpu,docs"',
]

EXTRA_SETS = [set(), {"test"}, {"dev"}, {"test", "docs"}, {"test-dev"}, {"gpu"}]


def _reference(marker: str, environment: dict[str, str], extras: set[str]) -> bool:
    """pip's rule: evaluate once per requested extra, then OR."""
    parsed = Marker(marker)
    if not extras:
        return parsed.evaluate({**environment, "extra": ""})
    return any(parsed.evaluate({**environment, "extra": e}) for e in extras)


@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize("environment_name", sorted(ENVIRONMENTS))
def test_matches_packaging(marker: str, environment_name: str) -> None:
    environment = ENVIRONMENTS[environment_name]
    for extras in EXTRA_SETS:
        expected = _reference(marker, environment, extras)
        contexts = [{**environment, "extra": e} for e in extras] or [
            {**environment, "extra": ""}
        ]
        tree = parse_marker(marker)
        assert (
            any(evaluate_marker(tree, context) for context in contexts) is expected
        ), (
            marker,
            environment_name,
            sorted(extras),
        )


def test_grouped_marker_keeps_its_guard() -> None:
    """The failure that motivated the parser: a group swallowed its guard.

    Splitting on `or` left `platform_machine == "AMD64"` as a bare clause, so
    the requirement applied on every AMD64 machine whether or not the
    `huggingface` extra had been asked for.
    """
    marker = (
        '(platform_machine == "x86_64" or platform_machine == "amd64" '
        'or platform_machine == "AMD64" or platform_machine == "arm64" '
        'or platform_machine == "aarch64") and extra == "huggingface"'
    )
    assert marker_applies(marker, extras=()) is False
    assert marker_applies(marker, extras=("test",)) is False
    assert marker_applies(marker, extras=("huggingface",)) is True


def test_extras_are_or_ed_not_intersected() -> None:
    """`extra` holds one value, so the marker runs once per requested extra."""
    assert marker_applies('extra != "gpu"', extras=("gpu",)) is False
    assert marker_applies('extra != "gpu"', extras=("gpu", "docs")) is True
    assert marker_applies('extra == "gpu"', extras=("gpu", "docs")) is True
    assert marker_applies('extra == "gpu"', extras=()) is False


def test_arbitrary_equality_compares_text() -> None:
    """``===`` is string equality, so a value that is not a version still
    matches -- and a failed clause no longer sinks the ``or`` around it."""
    environment = {"platform_release": "5.15.0-1034-Azure", "os_name": "posix"}
    tree = parse_marker('platform_release === "5.15.0-1034-azure"')
    assert evaluate_marker(tree, environment) is True
    tree = parse_marker('platform_release === "6.0" or os_name == "posix"')
    assert evaluate_marker(tree, environment) is True


def test_extra_literal_is_normalised() -> None:
    assert marker_applies('extra == "Test.Dev"', extras=("test-dev",)) is True
    assert marker_applies('extra == "test_dev"', extras=("test-dev",)) is True


@pytest.mark.parametrize(
    "marker",
    [
        "",
        "python_version",
        "python_version >=",
        '>= "3.8"',
        'python_version >= "3.8" and',
        '(python_version >= "3.8"',
        'python_version >= "3.8")',
        'nonexistent_variable == "x"',
        "python_version >= 3.8",
        'python_version ~ "3.8"',
    ],
)
def test_rejects_what_packaging_rejects(marker: str) -> None:
    with pytest.raises(packaging.markers.InvalidMarker):
        Marker(marker)
    with pytest.raises(InvalidMarker):
        parse_marker(marker)


def test_rejects_a_comparison_between_two_literals() -> None:
    """`"3.8" >= "3.9"` fits the grammar but names no variable.

    packaging accepts it at parse time and fails when evaluated, because it
    looks the right-hand literal up in the environment. Rejecting it up front
    reaches the same outcome one step earlier.
    """
    with pytest.raises(InvalidMarker):
        parse_marker('"3.8" >= "3.9"')
    assert marker_applies('"3.8" >= "3.9"') is False


def test_unparseable_marker_does_not_abort_a_resolve() -> None:
    """One bad Requires-Dist line must not take down the whole install."""
    assert marker_applies("this is not a marker") is False


def test_undefined_variable_raises_at_the_evaluator() -> None:
    """The evaluator is strict; marker_applies() is what tolerates it."""
    tree = parse_marker('python_version >= "3.8"')
    assert marker_matches('python_version >= "3.8"', LINUX) is True
    with pytest.raises(UndefinedEnvironmentName):
        evaluate_marker(tree, {})


@pytest.mark.parametrize(
    "marker, expected",
    [
        ("python_version<'3.7'", 'python_version < "3.7"'),
        ('  python_version   >=   "3.8"  ', 'python_version >= "3.8"'),
        (
            "(python_version < '3.12') and extra == 'test'",
            'python_version < "3.12" and extra == "test"',
        ),
        (
            'os_name == "posix" and (sys_platform == "linux" or sys_platform == "win32")',
            'os_name == "posix" and (sys_platform == "linux" or sys_platform == "win32")',
        ),
        # `and` binds tighter, so its group is redundant and is not restored.
        (
            '(os_name == "posix" and sys_platform == "linux") or os_name == "nt"',
            'os_name == "posix" and sys_platform == "linux" or os_name == "nt"',
        ),
    ],
)
def test_canonical_spelling(marker: str, expected: str) -> None:
    """Two spellings of one marker canonicalise the same, so requirements dedupe."""
    assert canonical_marker(marker) == expected
    assert format_marker(parse_marker(expected)) == expected


def test_canonical_spelling_tolerates_junk() -> None:
    """Callers that only display or compare a marker must not fail on one."""
    assert canonical_marker("  not  a   marker  ") == "not a marker"


def test_parse_is_interned() -> None:
    assert parse_marker('python_version >= "3.8"') is parse_marker(
        'python_version >= "3.8"',
    )
