from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, call, patch

import pytest
from kpip.index.links import Link
from kpip.network.download import (
    Downloader,
    get_http_response_size,
    log_download,
    parse_content_disposition,
    sanitize_content_filename,
)
from kpip.network.exceptions import (
    ConnectionFailedError,
    ConnectionTimeoutError,
    IncompleteDownloadError,
    ProxyConnectionError,
    SSLMissingError,
)
from kpip.network.session import NetworkSession
from kpip_test_support.transport_mocks import BrokenStream, MockResponse
from kpip_test_support.server import Body, MockServer

if TYPE_CHECKING:
    from typeshed.wsgi import StartResponse, WSGIEnvironment


@pytest.mark.parametrize(
    "url, headers, from_cache, range_start, expected",
    [
        (
            "http://example.com/foo.tgz",
            {},
            False,
            None,
            "Downloading foo.tgz",
        ),
        (
            "http://example.com/foo.tgz",
            {"content-length": "2"},
            False,
            None,
            "Downloading foo.tgz (2 bytes)",
        ),
        (
            "http://example.com/foo.tgz",
            {"content-length": "2"},
            True,
            None,
            "Using cached foo.tgz (2 bytes)",
        ),
        (
            "https://files.pythonhosted.org/foo.tgz",
            {},
            False,
            None,
            "Downloading foo.tgz",
        ),
        (
            "https://files.pythonhosted.org/foo.tgz",
            {"content-length": "2"},
            False,
            None,
            "Downloading foo.tgz (2 bytes)",
        ),
        (
            "https://files.pythonhosted.org/foo.tgz",
            {"content-length": "2"},
            True,
            None,
            "Using cached foo.tgz",
        ),
        (
            "http://example.com/foo.tgz",
            {"content-length": "200"},
            False,
            100,
            "Resuming download foo.tgz (100 bytes/200 bytes)",
        ),
    ],
)
def test_log_download(
    caplog: pytest.LogCaptureFixture,
    url: str,
    headers: dict[str, str],
    from_cache: bool,
    range_start: int | None,
    expected: str,
) -> None:
    caplog.set_level(logging.INFO)
    resp = MockResponse(b"")
    resp.url = url
    resp.headers.update(headers)
    if from_cache:
        resp.from_cache = from_cache
    link = Link(url)
    total_length = get_http_response_size(resp)
    log_download(
        resp,
        link,
        total_length=total_length,
        range_start=range_start,
    )

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "INFO"
    assert expected in record.message


@pytest.mark.parametrize(
    "content_length, expected",
    [
        ("0", 0),
        ("36", 36),
        ("", None),
        ("not-a-number", None),
        ("-1", None),
    ],
)
def test_get_http_response_size(content_length: str, expected: int | None) -> None:
    resp = MockResponse(b"")
    resp.headers["content-length"] = content_length
    assert get_http_response_size(resp) == expected


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("dir/file", "file"),
        ("../file", "file"),
        ("../../file", "file"),
        ("../", ""),
        ("../..", ".."),
        ("/", ""),
    ],
)
def test_sanitize_content_filename(filename: str, expected: str) -> None:
    """Test inputs where the result is the same for Windows and non-Windows."""
    assert sanitize_content_filename(filename) == expected


@pytest.mark.parametrize(
    "filename, win_expected, non_win_expected",
    [
        ("dir\\file", "file", "dir\\file"),
        ("..\\file", "file", "..\\file"),
        ("..\\..\\file", "file", "..\\..\\file"),
        ("..\\", "", "..\\"),
        ("..\\..", "..", "..\\.."),
        ("\\", "", "\\"),
    ],
)
def test_sanitize_content_filename__platform_dependent(
    filename: str,
    win_expected: str,
    non_win_expected: str,
) -> None:
    """Test inputs where the result is different for Windows and non-Windows."""
    if sys.platform == "win32":
        expected = win_expected
    else:
        expected = non_win_expected
    assert sanitize_content_filename(filename) == expected


@pytest.mark.parametrize(
    "content_disposition, default_filename, expected",
    [
        ('attachment;filename="../file"', "df", "file"),
    ],
)
def test_parse_content_disposition(
    content_disposition: str,
    default_filename: str,
    expected: str,
) -> None:
    actual = parse_content_disposition(content_disposition, default_filename)
    assert actual == expected


@pytest.mark.parametrize(
    "resume_retries,mock_responses,expected_resume_args,expected_bytes",
    [
        (
            0,
            [({}, 200, b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89")],
            [],
            b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89",
        ),
        (
            0,
            [({"content-length": "36"}, 200, b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89")],
            [],
            b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89",
        ),
        (
            0,
            [({"content-length": "36"}, 200, b"0cfa7e9d-1868-4dd7-9fb3-")],
            [],
            None,
        ),
        (
            5,
            [
                ({"content-length": "36"}, 200, b"0cfa7e9d-1868-4dd7-9fb3-"),
                ({"content-length": "12"}, 206, b"f2561d5dfd89"),
            ],
            [(24, None)],
            b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89",
        ),
        (
            5,
            [
                ({"content-length": "36"}, 200, b"0cfa7e9d-1868-4dd7-9fb3-"),
                ({"content-length": "36"}, 200, b"0cfa7e9d-1868-"),
                (
                    {"content-length": "36"},
                    200,
                    b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89",
                ),
            ],
            [(24, None), (14, None)],
            b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89",
        ),
        (
            5,
            [
                ({"content-length": "36"}, 200, b"0cfa7e9d-1868-4dd7-9fb3-"),
                (
                    {"content-length": "40"},
                    200,
                    b"new-0cfa7e9d-1868-4dd7-9fb3-f2561d5d",
                ),
                ({"content-length": "4"}, 206, b"fd89"),
            ],
            [(24, None), (36, None)],
            b"new-0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89",
        ),
        (
            1,
            [
                ({"content-length": "36"}, 200, b"0cfa7e9d-1868-4dd7-9fb3-"),
                ({"content-length": "36"}, 200, b"0cfa7e9d-1868-"),
            ],
            [(24, None)],
            None,
        ),
        (
            5,
            [
                (
                    {
                        "content-length": "36",
                        "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT",
                    },
                    200,
                    b"0cfa7e9d-1868-4dd7-9fb3-",
                ),
                (
                    {
                        "content-length": "42",
                        "last-modified": "Wed, 21 Oct 2015 07:30:00 GMT",
                    },
                    200,
                    b"new-0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89",
                ),
                (
                    {
                        "content-length": "12",
                        "last-modified": "Wed, 21 Oct 2015 07:54:00 GMT",
                    },
                    200,
                    b"f2561d5dfd89",
                ),
            ],
            [
                (24, "Wed, 21 Oct 2015 07:28:00 GMT"),
                (40, "Wed, 21 Oct 2015 07:30:00 GMT"),
            ],
            b"f2561d5dfd89",
        ),
        (
            5,
            [
                (
                    {
                        "content-length": "36",
                        "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT",
                        "etag": '"33a64df551425fcc55e4d42a148795d9f25f89d4"',
                    },
                    200,
                    b"0cfa7e9d-1868-4dd7-9fb3-",
                ),
                (
                    {
                        "content-length": "12",
                        "last-modified": "Wed, 21 Oct 2015 07:54:00 GMT",
                        "etag": '"33a64df551425fcc55e4d42a148795d9f25f89d4"',
                    },
                    200,
                    b"f2561d5dfd89",
                ),
            ],
            [(24, '"33a64df551425fcc55e4d42a148795d9f25f89d4"')],
            b"f2561d5dfd89",
        ),
    ],
)
def test_downloader(
    resume_retries: int,
    mock_responses: list[tuple[dict[str, str], int, bytes]],
    expected_resume_args: list[tuple[int | None, str | None]],
    expected_bytes: bytes | None,
    tmp_path: Path,
) -> None:
    session = NetworkSession(resume_retries=resume_retries)
    link = Link("http://example.com/foo.tgz")
    downloader = Downloader(session)

    responses = []
    for headers, status, body in mock_responses:
        resp = MockResponse(body)
        resp.headers.update(headers)
        resp.status = status
        responses.append(resp)
    http_get_mock = MagicMock(side_effect=responses)

    with patch.object(Downloader, "http_get", http_get_mock):
        if expected_bytes is None:
            remove = MagicMock(return_value=None)
            with patch("os.remove", remove):
                with pytest.raises(IncompleteDownloadError):
                    downloader(link, str(tmp_path))
            remove.assert_called_once()
        else:
            filepath, _ = downloader(link, str(tmp_path))
            with open(filepath, "rb") as downloaded_file:
                downloaded_bytes = downloaded_file.read()
                assert downloaded_bytes == expected_bytes

    calls = [call(link)]
    for range_start, if_range in expected_resume_args:
        headers = {"Range": f"bytes={range_start}-"}
        if if_range:
            headers["If-Range"] = if_range
        calls.append(call(link, headers))

    http_get_mock.assert_has_calls(calls)


def test_downloader_resumes_on_protocol_error(tmp_path: Path) -> None:
    """A ProtocolError mid-stream should trigger resume logic, not crash."""
    session = NetworkSession(resume_retries=3)
    link = Link("http://example.com/foo.tgz")
    downloader = Downloader(session)

    broken_resp = MockResponse(b"0cfa7e9d-1868-4dd7-9fb3-")
    broken_resp.headers.update({"content-length": "36"})
    broken_resp.status = 200
    broken_resp._fp = BrokenStream(b"0cfa7e9d-1868-4dd7-9fb3-")

    resume_resp = MockResponse(b"f2561d5dfd89")
    resume_resp.headers.update({"content-length": "12"})
    resume_resp.status = 206

    http_get_mock = MagicMock(side_effect=[broken_resp, resume_resp])

    with patch.object(Downloader, "http_get", http_get_mock):
        filepath, _ = downloader(link, str(tmp_path))

    with open(filepath, "rb") as f:
        assert f.read() == b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89"


@pytest.mark.parametrize(
    "resume_error",
    [
        OSError("Connection broken"),
        TimeoutError("Read timed out"),
    ],
)
def test_downloader_retries_low_level_errors_during_resume(
    resume_error: Exception,
    tmp_path: Path,
) -> None:
    """Low-level errors raised while fetching a resume response are retried."""
    session = NetworkSession(resume_retries=5)
    link = Link("http://example.com/foo.tgz")
    downloader = Downloader(session)

    broken_resp = MockResponse(b"0cfa7e9d-1868-4dd7-9fb3-")
    broken_resp.headers.update({"content-length": "36"})
    broken_resp.status = 200
    broken_resp._fp = BrokenStream(b"0cfa7e9d-1868-4dd7-9fb3-")

    resume_resp = MockResponse(b"f2561d5dfd89")
    resume_resp.headers.update({"content-length": "12"})
    resume_resp.status = 206

    http_get_mock = MagicMock(side_effect=[broken_resp, resume_error, resume_resp])

    with patch.object(Downloader, "http_get", http_get_mock):
        filepath, _ = downloader(link, str(tmp_path))

    assert http_get_mock.call_count == 3
    with open(filepath, "rb") as f:
        assert f.read() == b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89"


@pytest.mark.parametrize(
    "resume_error",
    [
        ConnectionFailedError(
            "https://example.com/foo.tgz",
            "example.com",
            ConnectionError("Connection broken"),
        ),
        ConnectionTimeoutError(
            "https://example.com/foo.tgz",
            "example.com",
            kind="read",
            timeout=15,
        ),
        ProxyConnectionError(
            "https://example.com/foo.tgz",
            "https://proxy.example.com",
            OSError("Cannot connect to proxy"),
        ),
    ],
)
def test_downloader_retries_diagnostic_connection_errors_during_resume(
    resume_error: Exception,
    tmp_path: Path,
) -> None:
    """Diagnostic connection errors during resume should consume a resume retry."""
    session = NetworkSession(resume_retries=5)
    link = Link("http://example.com/foo.tgz")
    downloader = Downloader(session)

    broken_resp = MockResponse(b"0cfa7e9d-1868-4dd7-9fb3-")
    broken_resp.headers.update({"content-length": "36"})
    broken_resp.status = 200

    resume_resp = MockResponse(b"f2561d5dfd89")
    resume_resp.headers.update({"content-length": "12"})
    resume_resp.status = 206

    http_get_mock = MagicMock(side_effect=[broken_resp, resume_error, resume_resp])

    with patch.object(Downloader, "http_get", http_get_mock):
        filepath, _ = downloader(link, str(tmp_path))

    assert http_get_mock.call_count == 3
    with open(filepath, "rb") as f:
        assert f.read() == b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89"


def test_downloader_does_not_retry_on_ssl_missing_error(tmp_path: Path) -> None:
    """SSL errors during resume should fail immediately because retries can't help."""
    session = NetworkSession(resume_retries=5)
    link = Link("http://example.com/foo.tgz")
    downloader = Downloader(session)

    broken_resp = MockResponse(b"0cfa7e9d-1868-4dd7-9fb3-")
    broken_resp.headers.update({"content-length": "36"})
    broken_resp.status = 200
    resume_error = SSLMissingError("https://example.com/foo.tgz")

    http_get_mock = MagicMock(side_effect=[broken_resp, resume_error])

    with patch.object(Downloader, "http_get", http_get_mock):
        with pytest.raises(type(resume_error)):
            downloader(link, str(tmp_path))

    assert http_get_mock.call_count == 2


def test_downloader_resumes_on_truncated_http_stream(
    mock_server: MockServer,
    tmp_path: Path,
) -> None:
    """A truncated stream raises a real urllib3 ProtocolError that resume recovers."""
    body = b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89"

    def truncated(environ: WSGIEnvironment, start_response: StartResponse) -> Body:
        start_response("200 OK", [("Content-Length", str(len(body)))])
        return [body[:10]]

    def resumed(environ: WSGIEnvironment, start_response: StartResponse) -> Body:
        start = int(environ["HTTP_RANGE"].split("=", 1)[1].split("-", 1)[0])
        start_response(
            "206 Partial Content",
            [
                ("Content-Length", str(len(body) - start)),
                ("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"),
            ],
        )
        return [body[start:]]

    mock_server.set_responses([truncated, resumed])
    mock_server.start()
    url = f"http://{mock_server.host}:{mock_server.port}/foo.tgz"

    session = NetworkSession(resume_retries=3)
    downloader = Downloader(session)
    filepath, _ = downloader(Link(url), str(tmp_path))

    with open(filepath, "rb") as f:
        assert f.read() == body


def test_downloader_crashes_on_mismatched_resume_offset(tmp_path: Path) -> None:
    """A 206 whose Content-Range starts at a different offset than requested
    must fail, otherwise the misplaced bytes would corrupt the file.
    """
    body = b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89"
    session = NetworkSession(resume_retries=5)
    link = Link("http://example.com/foo.tgz")
    downloader = Downloader(session)

    first = MockResponse(body[:24])
    first.headers.update({"content-length": "36"})
    first.status = 200

    mismatched = MockResponse(b"XXXXXXXXXXXX")
    mismatched.headers.update(
        {"content-length": "12", "content-range": "bytes 0-11/36"},
    )
    mismatched.status = 206

    http_get_mock = MagicMock(side_effect=[first, mismatched])
    with patch.object(Downloader, "http_get", http_get_mock):
        with pytest.raises(IncompleteDownloadError):
            downloader(link, str(tmp_path))


def test_downloader_without_content_length(tmp_path: Path) -> None:
    """A response without a Content-Length header should be treated as an
    unknown size and still download fully.

    This guards against MockResponse inventing its own Content-Length, which
    would hide the unknown-size download path from the tests.
    """
    body = b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89"
    resp = MockResponse(body)
    resp.status = 200

    assert get_http_response_size(resp) is None

    session = NetworkSession(resume_retries=0)
    downloader = Downloader(session)
    link = Link("http://example.com/foo.tgz")
    with patch.object(Downloader, "http_get", MagicMock(return_value=resp)):
        filepath, _ = downloader(link, str(tmp_path))

    with open(filepath, "rb") as downloaded_file:
        assert downloaded_file.read() == body


def test_resumed_download_caching(tmp_path: Path) -> None:
    """Test that resumed downloads are cached properly for future use."""
    cache_dir = tmp_path / "cache"
    session = NetworkSession(cache=str(cache_dir), resume_retries=5)
    link = Link("https://example.com/foo.tgz")
    downloader = Downloader(session)

    incomplete_resp = MockResponse(b"0cfa7e9d-1868-4dd7-9fb3-")
    incomplete_resp.headers.update({"content-length": "36"})
    incomplete_resp.status = 200

    resume_resp = MockResponse(b"f2561d5dfd89")
    resume_resp.headers.update({"content-length": "12"})
    resume_resp.status = 206

    responses = [incomplete_resp, resume_resp]
    http_get_mock = MagicMock(side_effect=responses)

    with patch.object(Downloader, "http_get", http_get_mock):
        filepath, _ = downloader(link, str(tmp_path))

        with open(filepath, "rb") as downloaded_file:
            downloaded_bytes = downloaded_file.read()
            expected_bytes = b"0cfa7e9d-1868-4dd7-9fb3-f2561d5dfd89"
            assert downloaded_bytes == expected_bytes

        assert cache_dir.exists()
        cache_files = list(cache_dir.rglob("*"))
        assert len([f for f in cache_files if f.is_file()]) == 2


def test_error_response_is_drained_and_closed(monkeypatch) -> None:
    """A streamed error response must not keep its connection checked out."""
    from kpip.core.http_contracts import HttpStatusError
    from kpip_test_support.transport_mocks import make_response

    url = "https://example.invalid/demo.whl"
    resp = make_response(
        status=404,
        reason="Not Found",
        url=url,
        headers={},
        body=b"missing",
        stream=True,
    )
    events: list[str] = []
    monkeypatch.setattr(resp, "drain_conn", lambda: events.append("drain"))
    monkeypatch.setattr(resp, "close", lambda: events.append("close"))

    session = NetworkSession()
    monkeypatch.setattr(session, "get", lambda *args, **kwargs: resp)

    link = Link.from_url(url, source_url=None)

    with pytest.raises(HttpStatusError):
        Downloader(session).http_get(link)

    assert events == ["drain", "close"]
