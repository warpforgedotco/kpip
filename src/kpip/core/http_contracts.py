"""Dependency-free structural contracts for HTTP clients and responses."""

from __future__ import annotations

from typing import Any, Protocol

from kpip.core.errors import KpipError


class HttpStatusError(KpipError):
    """An unsuccessful HTTP response."""

    def __init__(self, error_msg: str, response: HttpResponse) -> None:
        self.response = response
        super().__init__(error_msg)


class HttpResponse(Protocol):
    status: int
    reason: str
    url: str
    headers: Any

    @property
    def data(self) -> bytes: ...

    def read(self, amt: int | None = None) -> bytes: ...

    def stream(self, amt: int) -> Any: ...

    def drain_conn(self) -> None: ...

    def close(self) -> None: ...


class HttpSession(Protocol):
    auth: Any
    cache: Any

    def get(self, url: str, **kwargs: Any) -> HttpResponse: ...

    def head(self, url: str, **kwargs: Any) -> HttpResponse: ...


def raise_for_status(response: HttpResponse) -> None:
    if response.status < 400:
        return
    kind = "Client" if response.status < 500 else "Server"
    raise HttpStatusError(
        f"{response.status} {kind} Error: {response.reason} for url: {response.url}",
        response,
    )


def response_text(response: HttpResponse) -> str:
    content_type = response.headers.get("Content-Type", "")
    charset = "utf-8"
    for value in content_type.split(";")[1:]:
        key, separator, encoding = value.strip().partition("=")
        if separator and key.lower() == "charset":
            charset = encoding.strip().strip('"') or charset
            break
    data = response.data
    try:
        return data.decode(charset, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")
