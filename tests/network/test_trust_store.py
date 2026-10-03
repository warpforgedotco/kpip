"""Certificates are checked against the system's trust store, as pip's are."""

from __future__ import annotations

import ssl

import pytest
from kpip._vendor import certifi, truststore
from kpip.network.session import NetworkSession


def test_the_systems_trust_store_by_default() -> None:
    context = NetworkSession().ssl_context_for(True, None)

    assert isinstance(context, truststore.SSLContext)


def test_a_named_bundle_is_trusted_beside_the_systems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[str] = []
    monkeypatch.setattr(
        truststore.SSLContext,
        "load_verify_locations",
        lambda self, cafile=None, **_: loaded.append(cafile),
    )

    context = NetworkSession().ssl_context_for("/etc/corporate-ca.pem", None)

    assert isinstance(context, truststore.SSLContext)
    assert loaded == ["/etc/corporate-ca.pem"]


def test_legacy_certs_checks_against_certifi_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    session = NetworkSession()
    session.legacy_certs = True

    context = session.ssl_context_for(True, None)

    assert type(context) is ssl.SSLContext
    certifi_count = len(
        ssl.create_default_context(cafile=certifi.where()).get_ca_certs()
    )
    assert len(context.get_ca_certs()) == certifi_count


def test_use_deprecated_legacy_certs_sets_it() -> None:
    import argparse

    from kpip.cli.general_options import add_general_options, apply_general_options
    from kpip.core import run_options

    parser = argparse.ArgumentParser()
    add_general_options(parser)
    apply_general_options(parser.parse_args(["--use-deprecated=legacy-certs"]))
    try:
        assert run_options.current.legacy_certs
    finally:
        run_options.reset()


def test_a_handshake_never_turns_verification_off_on_the_shared_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """truststore turns its context's verification off for each handshake;
    two at once left it off. Each handshake gets a context of its own, made
    with what was set on the shared one."""
    from kpip.network.session import _trust_store_context_class

    monkeypatch.setattr(_trust_store_context_class(), "shares_handshake_context", False)
    seen: list[tuple[object, object, object]] = []

    def wrap(self: object, sock: object, **_: object) -> object:
        seen.append((self, getattr(self, "kpip_settings", None), sock))
        return "wrapped"

    monkeypatch.setattr(truststore.SSLContext, "wrap_socket", wrap)
    monkeypatch.setattr(
        truststore.SSLContext, "load_verify_locations", lambda self, *a, **k: None
    )
    context = NetworkSession().ssl_context_for("/etc/corporate-ca.pem", None)
    context.set_alpn_protocols(["http/1.1"])

    first = context.wrap_socket("socket-1", server_hostname="pypi.org")
    second = context.wrap_socket("socket-2", server_hostname="pypi.org")

    assert first == second == "wrapped"
    handshake_contexts = {id(entry[0]) for entry in seen}
    assert id(context) not in handshake_contexts
    assert len(handshake_contexts) == 2
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_what_is_set_on_the_shared_context_reaches_each_handshake() -> None:
    """urllib3 and callers set properties and call setters on the shared
    context; a handshake made with a fresh one used to drop every setting
    but three method calls."""
    from kpip.network.session import trust_store_context

    context = trust_store_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.options |= ssl.OP_NO_COMPRESSION
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_ciphers("ECDHE+AESGCM")

    handshake = context._handshake_context()  # ty: ignore[unresolved-attribute]

    assert handshake is not context
    assert handshake.minimum_version == ssl.TLSVersion.TLSv1_3
    assert handshake.options & ssl.OP_NO_COMPRESSION
    assert not handshake.check_hostname
    assert handshake.verify_mode == ssl.CERT_NONE
    assert [cipher["name"] for cipher in handshake.get_ciphers()] == [
        cipher["name"] for cipher in context.get_ciphers()
    ]


def test_a_call_urllib3_repeats_is_recorded_once() -> None:
    from kpip.network.session import trust_store_context

    context = trust_store_context()
    for _ in range(3):
        context.set_alpn_protocols(["http/1.1"])

    assert context.kpip_calls == [  # ty: ignore[unresolved-attribute]
        ("set_alpn_protocols", (["http/1.1"],), {})
    ]


def test_a_setting_no_handshake_would_see_is_refused() -> None:
    from kpip.network.session import trust_store_context

    with pytest.raises(AttributeError, match="sni_callback"):
        trust_store_context().sni_callback = lambda *args: None


def test_with_openssl_handshakes_share_a_context_loaded_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With OpenSSL truststore never turns verification off, and reading the
    system's CAs into a context for every handshake was most of what a
    handshake cost: they share one, made again when a setting changes."""
    from kpip._vendor.truststore import _api as truststore_api
    from kpip.network.session import _trust_store_context_class, trust_store_context

    monkeypatch.setattr(_trust_store_context_class(), "shares_handshake_context", True)
    loads: list[object] = []
    configure = truststore_api._configure_context

    def counted(context: ssl.SSLContext) -> object:
        loads.append(context)
        return configure(context)

    monkeypatch.setattr(truststore_api, "_configure_context", counted)
    context = trust_store_context()
    context.set_alpn_protocols(["http/1.1"])

    first = context._handshake_context()  # ty: ignore[unresolved-attribute]
    second = context._handshake_context()  # ty: ignore[unresolved-attribute]

    assert first is second
    assert type(first) is not type(context)
    assert len(loads) == 1
    assert first.verify_mode == ssl.CERT_REQUIRED
    assert first.check_hostname

    context.minimum_version = ssl.TLSVersion.TLSv1_3
    third = context._handshake_context()  # ty: ignore[unresolved-attribute]

    assert third is not first
    assert third.minimum_version == ssl.TLSVersion.TLSv1_3
    assert len(loads) == 2
