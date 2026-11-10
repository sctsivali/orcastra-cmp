"""LXD connections authenticate the server by pinning its certificate fingerprint.

LXD uses no certificate authority, so `verify=False` on the TLS layer is expected; its trust model
is that the client remembers the server's fingerprint, which is why `lxc remote add` prints one and
asks to accept it. Without a pin, anyone on the path between this control plane and a hypervisor
can present their own certificate, take the mTLS session, and read or forge instance configs, exec
streams and console traffic.

Trust on first use, then pin, as SSH and LXD itself do for systems with no CA. The limit: it
protects against an attacker who arrives after enrolment, not one already in place.
"""

import inspect

import pytest

from app.core.config import Settings
from app.services.lxd import connection as connection_module
from app.services.lxd_certs.pinning import (
    FINGERPRINT_FIELD,
    TRUST_FIELD,
    TRUST_TOFU,
    LXDCertificatePinError,
    decide_pin,
)

PIN_A = "a" * 64
PIN_B = "b" * 64


def _with_live(monkeypatch, fingerprint):
    monkeypatch.setattr(
        "app.services.lxd_certs.pinning.fetch_server_fingerprint",
        lambda endpoint, timeout=5: fingerprint,
    )
    # A self-signed LXD certificate never verifies against a public CA.
    monkeypatch.setattr(
        "app.services.lxd_certs.pinning.fetch_public_ca_fingerprint",
        lambda endpoint, timeout=5: None,
    )


def _pinned(fingerprint):
    return {FINGERPRINT_FIELD: fingerprint, TRUST_FIELD: TRUST_TOFU}


def test_a_first_connection_records_the_certificate(monkeypatch) -> None:
    _with_live(monkeypatch, PIN_A)
    decision = decide_pin("nuc2", "https://host:8443", None)
    assert decision.outcome == "pinned"
    assert decision.fingerprint == PIN_A
    assert decision.update[FINGERPRINT_FIELD] == PIN_A


def test_a_matching_certificate_is_accepted(monkeypatch) -> None:
    _with_live(monkeypatch, PIN_A)
    decision = decide_pin("nuc2", "https://host:8443", _pinned(PIN_A))
    assert decision.outcome == "match"
    assert decision.update is None, "a match must not rewrite the stored pin"


def test_a_substituted_certificate_is_refused(monkeypatch) -> None:
    """A different certificate on a pinned cluster is refused, not logged as a warning."""
    _with_live(monkeypatch, PIN_B)
    with pytest.raises(LXDCertificatePinError) as exc:
        decide_pin("nuc2", "https://host:8443", _pinned(PIN_A))
    assert "nuc2" in str(exc.value)
    assert "Re-pin" in str(exc.value), "the refusal must say how a legitimate rotation is accepted"


def test_an_unreachable_endpoint_is_not_reported_as_an_attack(monkeypatch) -> None:
    """A cluster that cannot handshake fails on its own terms a moment later.

    Turning an unreadable certificate into a pin error would report every offline cluster as a
    security incident each time something polled it.
    """
    _with_live(monkeypatch, None)
    decision = decide_pin("nuc2", "https://host:8443", _pinned(PIN_A))
    assert decision.outcome == "unavailable"
    assert decision.update is None


def test_the_pin_is_checked_before_the_client_certificate_is_sent() -> None:
    """Checking after the handshake would hand the client certificate over first."""
    source = inspect.getsource(connection_module.LXDConnectionManager.connect_with_vault_cert)
    assert "_enforce_certificate_pin" in source, "the connection path does not check the pin"
    assert source.index("_enforce_certificate_pin") < source.index("pylxd.Client("), (
        "the pin is checked after the client is built, so a substituted endpoint already "
        "received the client certificate"
    )


def test_a_vault_outage_does_not_look_like_a_certificate_attack() -> None:
    """One dependency being down must not present as an attack on another."""
    source = inspect.getsource(connection_module.LXDConnectionManager._enforce_certificate_pin)
    assert "connecting unpinned" in source
    assert "return" in source.split("Could not read the pin")[1][:200]


def test_pinning_is_on_by_default() -> None:
    """Pinning protects a fleet without anyone having to turn it on."""
    assert Settings.model_fields["lxd_pin_server_certificate"].default is True


def test_pinning_can_be_disabled_deliberately() -> None:
    """A setting turns pinning off, so an enrolment problem is not an outage with no lever."""
    source = inspect.getsource(connection_module.LXDConnectionManager._enforce_certificate_pin)
    assert "lxd_pin_server_certificate" in source


def test_a_rotation_can_be_accepted_without_disabling_the_control() -> None:
    """Without a re-pin action, the first legitimate rotation ends with pinning turned off."""
    from app.routers import cluster_registration

    assert hasattr(cluster_registration, "repin_cluster_certificate")
    source = inspect.getsource(cluster_registration.repin_cluster_certificate)
    assert "require_admin()" in source, "re-pinning is a trust decision, not a routine one"
    assert "fetch_server_fingerprint" in source, (
        "the endpoint must record what the cluster actually presents; accepting a "
        "caller-supplied fingerprint would let someone pin a cluster to a certificate they hold"
    )
    assert "remove_connection" in source, (
        "a client pooled before the re-pin still holds the old pin"
    )
