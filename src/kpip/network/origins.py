"""Which origins an index, a find-links page or a redirect may come from.

pip reads a page of links only from a secure origin -- HTTPS, a local file,
the loopback interface -- or a host the user trusts with ``--trusted-host``;
any other plaintext page could be rewritten in transit to point at whatever
an attacker likes, so it is ignored with a warning rather than used. These
are pip's ``SECURE_ORIGINS`` and ``is_secure_origin``.

Kept apart from ``session`` so the index can ask without importing urllib3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import ipaddress
import logging
import urllib.parse

from kpip.core.errors import CommandError

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable


logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"http": 80, "https": 443}

# pip's SECURE_ORIGINS: any port for these schemes, any scheme on loopback.
_SECURE_SCHEMES = frozenset({"https", "file", "ssh"})

_LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def trusted_host_key(value: str) -> tuple[str, int | None]:
    """``--trusted-host`` as ``(host, port)``: a port only if one is given.

    pip marks "this host or host:port pair as trusted", so ``h:8080``
    trusts that port alone; cutting at the first ":" also trusted every
    other port on ``h``, and broke on an IPv6 literal.
    """
    parsed = urllib.parse.urlsplit("//" + value.strip())
    try:
        port = parsed.port
    except ValueError as error:
        # Not host-wide trust: an unusable port must not turn certificate
        # checks off for every port on the host.

        raise CommandError(
            f"Invalid --trusted-host {value.strip()!r}: {error}"
        ) from error
    return (parsed.hostname or value.strip()).lower(), port


def is_trusted_host(
    trusted: Collection[tuple[str, int | None]], url: urllib.parse.SplitResult
) -> bool:
    host = (url.hostname or "").lower()
    if (host, None) in trusted:
        return True
    try:
        port = url.port
    except ValueError:
        return False
    return (host, port or _DEFAULT_PORTS.get(url.scheme)) in trusted


def is_secure_origin(
    url: str, trusted: Collection[tuple[str, int | None]] = ()
) -> bool:
    """Whether ``url`` is somewhere pip would read links from.

    ``trusted`` holds ``trusted_host_key`` pairs. A VCS scheme such as
    ``git+https`` is judged by its transport, as pip judges it.
    """
    parsed = urllib.parse.urlsplit(url)

    if parsed.scheme.rsplit("+", 1)[-1].lower() in _SECURE_SCHEMES:
        return True

    host = (parsed.hostname or "").lower()

    # pip's host patterns let a URL with no host through -- a bare path
    # given as an index is a local directory, not somewhere on the network.
    if not host or host == "localhost":
        return True

    try:
        address = ipaddress.ip_address(host)

    except ValueError:
        pass

    else:
        if any(address in network for network in _LOOPBACK_NETWORKS):
            return True

    return bool(trusted) and is_trusted_host(trusted, parsed)


def secure_source(
    url: str, trusted_hosts: Iterable[str] = (), session: object = None
) -> bool:
    """Whether to read the index or find-links page at ``url``; warns, as
    pip does, when it is ignored.

    The hosts trusted are those given here, the command's ``--trusted-host``
    and any a requirements file added to ``session``. They are gathered only
    for a plaintext URL, so the usual HTTPS index never builds the session.
    """
    if is_secure_origin(url):
        return True

    from kpip.core import run_options

    trusted = {trusted_host_key(host) for host in trusted_hosts}
    trusted.update(trusted_host_key(host) for host in run_options.current.trusted_hosts)

    if session is not None:
        trusted.update(getattr(session, "trusted_hosts", None) or ())

    if is_secure_origin(url, trusted):
        return True

    host = urllib.parse.urlsplit(url).hostname

    logger.warning(
        "The repository located at %s is not a trusted or secure host and is "
        "being ignored. If this repository is available via HTTPS we recommend "
        "you use HTTPS instead, otherwise you may silence this warning and "
        "allow it anyway with '--trusted-host %s'.",
        host,
        host,
    )

    return False
