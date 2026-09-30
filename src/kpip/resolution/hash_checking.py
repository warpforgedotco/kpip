from __future__ import annotations


from typing import TYPE_CHECKING
from kpip.core.errors import (
    DirectoryUrlHashUnsupported,
    HashError,
    HashMissing,
    HashUnpinned,
    VcsHashUnsupported,
)
from kpip.core.packaging import parse_requirement

if TYPE_CHECKING:
    from collections.abc import Container, Iterable
    from typing import Any
    from kpip.resolution.req_install import InstallRequirement


class HashErrors(HashError):
    """Every hash-mode failure in one report, grouped by kind.

    Each failure keeps its own exception type, so a caller can still ask what
    went wrong; the grouping only decides how they read together. Kinds are
    ordered by :attr:`HashError.order` -- hardest to act on first.
    """

    def __init__(self) -> None:
        super().__init__()
        self.errors: list[tuple[HashError, str]] = []

    def append(self, error: HashError, subject: str) -> None:
        self.errors.append((error, subject))

    def __bool__(self) -> bool:
        return bool(self.errors)

    def __str__(self) -> str:
        lines: list[str] = []
        previous: type[HashError] | None = None
        for error, subject in sorted(
            self.errors,
            key=lambda item: (item[0].order, item[1]),
        ):
            kind = type(error)
            if kind is not previous:
                lines.append(kind.head)
                previous = kind
            lines.append(f"    {subject}")
        return "\n".join(lines)


def describe(requirement: InstallRequirement) -> str:
    """How a requirement is named back to the user in an error."""
    text = str(requirement.req) if requirement.req is not None else ""
    if not text and requirement.link is not None:
        text = requirement.link.url
    source = getattr(requirement, "comes_from", None)
    if isinstance(source, str) and source:
        return f"{text} (from {source})"
    return text or "<unnamed requirement>"


def check_requirement(
    requirement: InstallRequirement,
    errors: HashErrors,
    pinned_by_constraint: Container[str] = (),
) -> None:
    """Record every rule ``requirement`` breaks.

    A requirement that cannot be hashed at all is reported only for that, and
    not also for the pin and digest it was never going to be able to carry.

    ``pinned_by_constraint`` names projects some constraint pins with ``==``.
    A bare ``base`` in requirements.txt against ``base==0.1.0`` in
    constraints.txt does resolve to one release, so the pin rule is satisfied
    even though the requirement itself carries no specifier.
    """
    link = requirement.link
    subject = describe(requirement)

    if link is not None and link.is_vcs:
        errors.append(VcsHashUnsupported(subject), subject)
        return

    if requirement.editable or (link is not None and link.is_existing_dir):
        errors.append(DirectoryUrlHashUnsupported(subject), subject)
        return

    # A direct URL already names one artifact, so there is nothing for a pin
    # to add; pip makes the same exemption.
    if (
        not requirement.is_direct
        and (requirement.req is None or not requirement.is_pinned)
        and (
            requirement.req is None
            or requirement.req.canonical_name not in pinned_by_constraint
        )
    ):
        errors.append(HashUnpinned(subject), subject)

    if not requirement.hashes(trust_internet=False):
        errors.append(HashMissing(subject), subject)


def constraint_pinned_names(constraints: Iterable[str]) -> set[str]:
    """Canonical names some constraint pins to a single release."""

    pinned: set[str] = set()
    for raw in constraints:
        try:
            requirement = parse_requirement(raw)
        except ValueError:
            continue
        if requirement.specifier.is_pinned:
            pinned.add(requirement.canonical_name)
    return pinned


def enforce_hash_checking(
    requirements: Iterable[InstallRequirement],
    *,
    constraints: Iterable[str] = (),
) -> None:
    """Raise unless every requirement satisfies hash-checking mode.

    Call this once the full requirement set is known and before anything is
    fetched: the point of the mode is that an install which cannot be verified
    never starts, not that it is torn down halfway.
    """
    pinned_by_constraint = constraint_pinned_names(constraints)
    errors = HashErrors()
    for requirement in requirements:
        check_requirement(requirement, errors, pinned_by_constraint)
    if errors:
        raise errors


def enforce_dependency_hashes(
    candidates: Iterable[Any],
    hashed_names: Container[str],
    checked_names: Container[str],
) -> None:
    """Raise unless every resolved candidate carries a hash the user gave.

    :func:`enforce_hash_checking` can only see the requirements the user
    wrote; the dependencies they pull in are known once resolution is done.
    Hash-checking mode covers those too -- a dependency nobody hashed would
    otherwise install unverified -- so each must be hashed by a requirement
    or a constraint. ``checked_names`` are the user's own requirements,
    already held to the rules above.

    Call this before anything is fetched. Where the index published a digest
    it is shown, as a line to add.
    """
    errors = HashErrors()
    for candidate in candidates:
        name = candidate.canonical_name
        if name in hashed_names or name in checked_names:
            continue
        subject = f"{candidate.name}=={candidate.version}"
        digest = (getattr(candidate, "source_hashes", None) or {}).get("sha256")
        if digest:
            subject += f" --hash=sha256:{digest}"
        errors.append(HashMissing(subject), subject)
    if errors:
        raise errors
