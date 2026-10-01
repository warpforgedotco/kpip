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
