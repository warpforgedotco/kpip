from __future__ import annotations


def valid_reference(value: str) -> bool:
    return bool(value) and all(
        part and all("a" <= character <= "z" for character in part)
        for part in value.split("-")
    )


class KpipError(Exception):
    """Base class for expected command failures."""


class DiagnosticKpipError(KpipError):
    reference: str | None = None

    def __init__(
        self,
        *,
        message: str,
        context: str | None = None,
        note_stmt: str | None = None,
        hint_stmt: str | None = None,
        reference: str | None = None,
    ) -> None:
        super().__init__(message)
        resolved_reference = reference or self.reference
        if resolved_reference is None:
            raise AssertionError("error reference not provided!")
        if not valid_reference(resolved_reference):
            raise AssertionError("error reference must be kebab-case!")
        self.reference = resolved_reference
        self.message = message
        self.context = context
        self.note_stmt = note_stmt
        self.hint_stmt = hint_stmt

    def render(self, *, ascii: bool = False, color: bool = False) -> str:
        return (
            self.render_ascii(color=color)
            if ascii
            else self.render_unicode(color=color)
        )

    def render_header(self, *, color: bool) -> str:
        reference = self.reference
        if reference is None:
            raise AssertionError("error reference not provided!")
        if color:
            return "\x1b[1;31merror\x1b[0m: \x1b[1m" + reference + "\x1b[0m"
        return f"error: {reference}"

    def render_trailers(self, *, color: bool) -> list[str]:
        lines: list[str] = []
        if self.note_stmt is not None:
            prefix = "\x1b[1;35mnote\x1b[0m" if color else "note"
            lines.append(f"{prefix}: {self.note_stmt}")
        if self.hint_stmt is not None:
            prefix = "\x1b[1;36mhint\x1b[0m" if color else "hint"
            lines.append(f"{prefix}: {self.hint_stmt}")
        return lines

    def render_ascii(self, *, color: bool) -> str:
        lines = [self.render_header(color=color), ""]
        lines.extend(self.message.splitlines())
        if self.context is not None:
            lines.append("")
            lines.extend(self.context.splitlines())
        trailers = self.render_trailers(color=color)
        if trailers:
            lines.append("")
            lines.extend(trailers)
        return "\n".join(lines) + "\n"

    def render_unicode(self, *, color: bool) -> str:
        lines = [self.render_header(color=color), ""]
        message_lines = self.message.splitlines()
        if message_lines:
            if self.context is None:
                first_prefix = "\x1b[31m×\x1b[0m" if color else "×"
                rest_prefix = "\x1b[31m \x1b[0m" if color else " "
                lines.append(f"{first_prefix} {message_lines[0]}")
                for line in message_lines[1:]:
                    lines.append(f"{rest_prefix} {line}")
            else:
                first_prefix = "\x1b[31m×\x1b[0m" if color else "×"
                mid_prefix = "\x1b[31m│\x1b[0m" if color else "│"
                ctx_prefix = "\x1b[31m╰─>\x1b[0m" if color else "╰─>"
                ctx_indent = "\x1b[31m   \x1b[0m" if color else "   "
                lines.append(f"{first_prefix} {message_lines[0]}")
                for line in message_lines[1:]:
                    lines.append(f"{mid_prefix} {line}")
                context_lines = self.context.splitlines()
                lines.append(f"{ctx_prefix} {context_lines[0]}")
                for line in context_lines[1:]:
                    lines.append(f"{ctx_indent} {line}")
        trailers = self.render_trailers(color=color)
        if trailers:
            lines.append("")
            lines.extend(trailers)
        return "\n".join(lines) + "\n"

    def __str__(self) -> str:
        return self.render()


class CommandError(KpipError):
    """The command line is invalid or unsupported."""


class ConfigurationError(CommandError):
    """Configuration input or lookup is invalid."""


class InstallationError(KpipError):
    """A package could not be installed."""


class InvalidWheelFilename(InstallationError):
    """The wheel filename is invalid."""


class UnsupportedWheel(InstallationError):
    """The wheel file is structurally unsupported."""


class BuildError(KpipError):
    """A source distribution could not be built."""


class DistributionNotFound(KpipError):
    """No matching distribution could be found."""


class ResolutionError(KpipError):
    """Requirements could not be resolved together.

    ``requires_python`` names each project whose every candidate the target
    Python was outside the ``Requires-Python`` of, with that specifier.
    ``edges`` are the requirements that could not be met together, each with
    the release that declared it -- ``(name, version)`` -- or None for one
    the user gave. ``constraints`` are the user's, by project, and
    ``located_versions`` the versions of the user's path and URL
    requirements, as built.
    """

    def __init__(
        self,
        message: str,
        requires_python: dict[str, str] | None = None,
        edges: list | None = None,
        constraints: dict | None = None,
        located_versions: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.requires_python = requires_python or {}
        self.edges = edges or []
        self.constraints = constraints or {}
        self.located_versions = located_versions or {}


class HashError(InstallationError):
    """A distribution hash is missing, unsupported, or invalid.

    ``order`` sorts the subclasses by how hard the problem is to act on,
    hardest first, so that a report covering several of them leads with the
    one the user has to solve before the others matter: being told to pin a
    version is noise while the file still names a repository that cannot be
    hashed at all. ``head`` is the sentence introducing a group of them.
    """

    order = 4
    head = ""


class HashMissing(HashError):
    """A required hash was not provided."""

    order = 2
    head = (
        "Hashes are required in --require-hashes mode, but they are missing "
        "from some requirements. Add lines like these to your requirements "
        "files to prevent tampering. (If you did not enable --require-hashes "
        "manually, note that it turns on automatically when any package has "
        "a hash.)"
    )


class HashMismatch(HashError):
    """A distribution hash does not match the allowed hashes."""

    order = 4
    head = (
        "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE. "
        "If you have updated the package versions, please update the hashes. "
        "Otherwise, examine the package contents carefully; someone may have "
        "tampered with them."
    )


class HashUnpinned(HashError):
    """A requirement is not pinned in require-hashes mode."""

    order = 3
    head = (
        "In --require-hashes mode, all requirements must have their versions "
        "pinned with ==. These do not:"
    )


class VcsHashUnsupported(HashError):
    """Version control requirements cannot be hash-verified."""

    order = 0
    head = (
        "Can't verify hashes for these requirements because we don't have a "
        "way to hash version control repositories:"
    )


class DirectoryUrlHashUnsupported(HashError):
    """Directory requirements cannot be hash-verified."""

    order = 1
    head = (
        "Can't verify hashes for these file:// requirements because they "
        "point to directories:"
    )
