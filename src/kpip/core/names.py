"""PEP 503 name normalization.

Split out of :mod:`kpip.core.packaging` because this three-line function is
needed on paths that need nothing else from that module -- notably
``cli.fast``, which the entrypoint imports on every command.  Reaching it
through ``packaging`` costs ``platform`` and ``subprocess`` as well, for no
benefit.
"""

from __future__ import annotations

from kpip.core.caches import memoized


@memoized(4096)
def canonicalize_name(name: str) -> str:
    if name.islower() and "_" not in name and "." not in name and "--" not in name:
        return name

    # Runs of "-", "_" and "." become one "-". Written out rather than as
    # ``re.sub``: importing ``re`` costs some 2.5 ms, and this module is on
    # the path of every command, most of which never meet a name that needs
    # rewriting.
    result: list[str] = []

    separator = False

    for character in name:
        if character in "-_.":
            if not separator:
                result.append("-")

                separator = True

            continue

        result.append(character)

        separator = False

    return "".join(result).lower()


def canonicalize_installed_name(value: str) -> str:
    """Normalize the *stem* of an installed metadata directory.

    Deliberately not :func:`canonicalize_name`. That one is the PEP 503 rule
    for a package name and turns a leading ``_`` into a leading ``-``; this
    one folds runs of ``-_.`` and drops them at either end, because what it
    is given is a directory stem like ``Foo_Bar-1.0`` whose separators are
    structure rather than part of a name.
    """
    result: list[str] = []

    separator = False

    for character in value:
        if character in "-_.":
            separator = bool(result)

            continue

        if separator:
            result.append("-")

            separator = False

        result.append(character.lower())

    return "".join(result)


def installed_name_might_match(
    filename: str,
    suffix: str,
    requested: set[str],
) -> bool:
    """Whether ``filename`` could be metadata for one of ``requested``.

    Conservative on purpose: a directory is named for the distribution it
    holds, so ``<name>`` or ``<name>-<version>`` is enough to rule most of
    them out without opening anything. It may say yes to a directory that
    turns out not to match -- callers still check the parsed name -- but it
    must never say no to one that does.
    """
    stem = canonicalize_installed_name(filename[: -len(suffix)])

    return any(stem == name or stem.startswith(f"{name}-") for name in requested)
