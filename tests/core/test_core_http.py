import pytest
from kpip.core.http_contracts import (
    HttpResponse,
    HttpStatusError,
    raise_for_status,
    response_text,
)
from kpip_test_support.transport_mocks import make_response


def response(status: int = 200, body: bytes = b"downloaded") -> HttpResponse:
    return make_response(
        status=status,
        reason="Network Error" if status >= 400 else "OK",
        url="https://example.com/file",
        headers={"Content-Length": str(len(body))},
        body=body,
    )


@pytest.mark.parametrize("status", [401, 501])
def test_raise_for_status_raises_exception(status: int) -> None:
    with pytest.raises(HttpStatusError):
        raise_for_status(response(status))


def test_raise_for_status_accepts_success() -> None:
    raise_for_status(response())


def test_response_text_falls_back_for_unknown_charset() -> None:
    value = make_response(
        status=200,
        reason="OK",
        url="https://example.com/file",
        headers={"Content-Type": "text/plain; charset=unknown-kpip-charset"},
        body=b"caf\xc3\xa9",
    )

    assert response_text(value) == "café"
