"""A reader for the header block of a METADATA or WHEEL file."""

from __future__ import annotations

_NORMALIZED_METADATA_KEYS = {
    name: name.lower()
    for name in (
        "Metadata-Version",
        "Name",
        "Version",
        "Requires-Python",
        "Requires-Dist",
        "Provides-Extra",
        "Summary",
    )
}


class LightMetadata:
    """A minimal, dict-backed stand-in for ``email.message.Message``'s read side."""

    __slots__ = ("_fields", "_payload")

    def __init__(self, fields: dict[str, list[str]], payload: str) -> None:
        self._fields = fields
        self._payload = payload

    def get(self, name: str, default: str | None = None) -> str | None:
        values = self._fields.get(name.lower())
        return values[0] if values else default

    def get_all(self, name: str, default: list[str] | None = None) -> list[str]:
        values = self._fields.get(name.lower())
        if values:
            return list(values)
        return list(default) if default is not None else []

    def get_payload(self) -> str:
        return self._payload


def parse_metadata_text(text: str) -> LightMetadata:
    """Parse RFC 822-style metadata text (METADATA, WHEEL, PKG-INFO)."""
    fields: dict[str, list[str]] = {}
    current_key: str | None = None
    position = 0
    text_length = len(text)
    while position < text_length:
        newline = text.find("\n", position)
        line_end = newline if newline != -1 else text_length
        line = text[position:line_end]
        if line.endswith("\r"):
            line = line[:-1]
        if not line:
            position = line_end + 1
            break
        if line[0] in " \t" and current_key is not None:
            fields[current_key][-1] += "\n" + line.strip()
        else:
            key, separator, value = line.partition(":")
            current_key = (
                _NORMALIZED_METADATA_KEYS.get(key) or key.strip().lower()
                if separator
                else None
            )
            if current_key is not None:
                values = fields.get(current_key)
                if values is None:
                    values = []
                    fields[current_key] = values
                values.append(value.strip())
        position = line_end + 1
    return LightMetadata(fields, text[position:])
