"""A cluster behind a publicly trusted certificate keeps its pin across renewals.

A cluster can answer on a DNS name over 443 with a Let's Encrypt certificate, which is replaced
roughly every 60 days. A plain leaf pin would refuse it at every renewal until an Administrator
re-pinned it, so a renewal is accepted when the cluster was first pinned as public-CA and the new
certificate is publicly trusted for the same hostname. A self-signed LXD certificate keeps the
strict rule: any change is refused.

A portless endpoint is probed on 443, the port the client connects on. Probing 8443 instead would
record no pin, and every Migration Center plan would be refused with "Both clusters need pinned
certificate fingerprints".
"""

import logging

import pytest

from app.services.lxd_certs import pinning
from app.services.lxd_certs.pinning import (
    FINGERPRINT_FIELD,
    MAX_PREVIOUS,
    PINNED_AT_FIELD,
    PINNED_HOST_FIELD,
    PREVIOUS_FIELD,
    TRUST_FIELD,
    TRUST_PUBLIC_CA,
    TRUST_TOFU,
    LXDCertificatePinError,
    PinState,
    decide_pin,
    endpoint_host_port,
    repin_fields,
)

OLD = "a" * 64
NEW = "b" * 64
OTHER = "c" * 64
ENDPOINT = "https://vdc.example.org"


def _live(monkeypatch, presented, publicly_trusted=None):
    monkeypatch.setattr(pinning, "fetch_server_fingerprint", lambda endpoint, timeout=5: presented)
    monkeypatch.setattr(
        pinning, "fetch_public_ca_fingerprint", lambda endpoint, timeout=5: publicly_trusted
    )


def _public_pin(fingerprint=OLD, host="vdc.example.org:443", previous=None):
    return {
        "endpoint": ENDPOINT,
        FINGERPRINT_FIELD: fingerprint,
        PINNED_AT_FIELD: "2026-09-01T00:00:00+00:00",
        TRUST_FIELD: TRUST_PUBLIC_CA,
        PINNED_HOST_FIELD: host,
        PREVIOUS_FIELD: previous or [],
    }


def test_a_portless_https_endpoint_is_read_on_443() -> None:
    assert endpoint_host_port("https://vdc.example.org") == ("vdc.example.org", 443)


def test_an_explicit_port_is_honoured() -> None:
    assert endpoint_host_port("https://10.0.0.5:8443") == ("10.0.0.5", 8443)
    assert endpoint_host_port("10.0.0.5:9443") == ("10.0.0.5", 9443)


def test_a_bare_host_is_read_as_https() -> None:
    assert endpoint_host_port("vdc.example.org") == ("vdc.example.org", 443)


def test_a_plain_http_endpoint_is_never_pinned(monkeypatch) -> None:
    _live(monkeypatch, OLD, OLD)
    decision = decide_pin("lab", "http://vdc.example.org", {})
    assert decision.outcome == "unavailable"
    assert decision.update is None


def test_a_publicly_trusted_certificate_is_pinned_as_public_ca(monkeypatch) -> None:
    _live(monkeypatch, OLD, OLD)
    decision = decide_pin("hetzner", ENDPOINT, {})
    assert decision.outcome == "pinned"
    assert decision.fingerprint == OLD
    assert decision.update[TRUST_FIELD] == TRUST_PUBLIC_CA
    assert decision.update[PINNED_HOST_FIELD] == "vdc.example.org:443"
    assert decision.update[PREVIOUS_FIELD] == []


def test_a_self_signed_certificate_is_pinned_on_first_use(monkeypatch) -> None:
    _live(monkeypatch, OLD, None)
    decision = decide_pin("nuc2", "https://10.0.0.5:8443", {})
    assert decision.outcome == "pinned"
    assert decision.update[TRUST_FIELD] == TRUST_TOFU
    assert decision.update[PINNED_HOST_FIELD] is None, "a self-signed pin promises no hostname"


def test_a_renewal_is_accepted_and_the_old_pin_is_kept_in_the_lineage(monkeypatch, caplog) -> None:
    _live(monkeypatch, NEW, NEW)
    with caplog.at_level(logging.WARNING, logger=pinning.logger.name):
        decision = decide_pin("hetzner", ENDPOINT, _public_pin())
    assert decision.outcome == "renewed"
    assert decision.fingerprint == NEW
    assert decision.update[FINGERPRINT_FIELD] == NEW
    lineage = decision.update[PREVIOUS_FIELD]
    assert lineage[0]["fingerprint"] == OLD
    assert lineage[0]["pinned_at"] == "2026-09-01T00:00:00+00:00"
    assert lineage[0]["replaced_at"] == decision.update[PINNED_AT_FIELD]
    assert any("renewed" in record.getMessage() for record in caplog.records)


def test_the_lineage_is_bounded(monkeypatch) -> None:
    previous = [
        {"fingerprint": f"{index:x}" * 64, "pinned_at": None, "replaced_at": None}
        for index in range(1, MAX_PREVIOUS + 1)
    ]
    _live(monkeypatch, NEW, NEW)
    decision = decide_pin("hetzner", ENDPOINT, _public_pin(previous=previous))
    lineage = decision.update[PREVIOUS_FIELD]
    assert len(lineage) == MAX_PREVIOUS
    assert lineage[0]["fingerprint"] == OLD, "newest first"


def test_a_renewed_pin_matches_on_the_next_connection_without_a_write(monkeypatch) -> None:
    _live(monkeypatch, NEW, NEW)
    renewed = {**_public_pin(), **decide_pin("hetzner", ENDPOINT, _public_pin()).update}
    again = decide_pin("hetzner", ENDPOINT, renewed)
    assert again.outcome == "match"
    assert again.update is None


def test_a_public_ca_cluster_presenting_a_self_signed_certificate_is_refused(monkeypatch) -> None:
    """A downgrade is the shape an interception takes, so it is never read as a renewal."""
    _live(monkeypatch, NEW, None)
    with pytest.raises(LXDCertificatePinError):
        decide_pin("hetzner", ENDPOINT, _public_pin())


def test_a_self_signed_cluster_presenting_a_public_certificate_is_refused(monkeypatch) -> None:
    """The renewal rule is a promise made at enrolment; it cannot be acquired later."""
    tofu = {FINGERPRINT_FIELD: OLD, TRUST_FIELD: TRUST_TOFU}
    _live(monkeypatch, NEW, NEW)
    with pytest.raises(LXDCertificatePinError):
        decide_pin("lab", ENDPOINT, tofu)


def test_a_changed_self_signed_certificate_is_still_refused(monkeypatch) -> None:
    tofu = {FINGERPRINT_FIELD: OLD, TRUST_FIELD: TRUST_TOFU}
    _live(monkeypatch, NEW, None)
    with pytest.raises(LXDCertificatePinError):
        decide_pin("lab", "https://10.0.0.5:8443", tofu)


def test_a_certificate_on_another_port_of_the_same_host_is_refused(monkeypatch) -> None:
    """Another TLS service on the same name has its own valid certificate and is not the API."""
    _live(monkeypatch, NEW, NEW)
    with pytest.raises(LXDCertificatePinError):
        decide_pin("hetzner", "https://vdc.example.org:8443", _public_pin())


def test_a_certificate_for_another_host_is_refused(monkeypatch) -> None:
    """The endpoint can be edited while the pin survives, so the host is part of the promise."""
    _live(monkeypatch, NEW, NEW)
    with pytest.raises(LXDCertificatePinError):
        decide_pin("hetzner", "https://other.example.org", _public_pin())


def test_a_renewal_presented_by_a_different_socket_is_refused(monkeypatch) -> None:
    """Both handshakes have to have seen the same certificate, or the check proves nothing."""
    _live(monkeypatch, NEW, OTHER)
    with pytest.raises(LXDCertificatePinError):
        decide_pin("hetzner", ENDPOINT, _public_pin())


def test_an_ip_endpoint_is_never_classified_public_ca(monkeypatch) -> None:
    """A public CA does not vouch for an address on a private network, so TOFU is the only rule."""
    monkeypatch.setattr(pinning, "_verified_leaf_fingerprint", lambda host, port, timeout: OLD)
    assert pinning.fetch_public_ca_fingerprint("https://10.0.0.5:8443") is None
    assert pinning.fetch_public_ca_fingerprint("https://[2001:db8::1]:8443") is None
    assert pinning.fetch_public_ca_fingerprint("http://vdc.example.org") is None
    assert pinning.fetch_public_ca_fingerprint("https://vdc.example.org") == OLD


def test_a_legacy_pin_is_classified_once(monkeypatch) -> None:
    legacy = {FINGERPRINT_FIELD: OLD, PINNED_AT_FIELD: "2026-08-29T00:00:00+00:00"}
    _live(monkeypatch, OLD, OLD)
    decision = decide_pin("hetzner", ENDPOINT, legacy)
    assert decision.outcome == "match"
    assert decision.update == {
        TRUST_FIELD: TRUST_PUBLIC_CA,
        PINNED_HOST_FIELD: "vdc.example.org:443",
        PREVIOUS_FIELD: [],
    }


def test_a_legacy_self_signed_pin_is_classified_as_tofu(monkeypatch) -> None:
    legacy = {FINGERPRINT_FIELD: OLD}
    _live(monkeypatch, OLD, None)
    decision = decide_pin("nuc2", "https://10.0.0.5:8443", legacy)
    assert decision.update == {TRUST_FIELD: TRUST_TOFU}


def test_an_unreadable_certificate_keeps_the_stored_pin_and_warns_once(monkeypatch, caplog) -> None:
    _live(monkeypatch, None, None)
    pinning._reset_unavailable_warnings()
    with caplog.at_level(logging.WARNING, logger=pinning.logger.name):
        first = decide_pin("hetzner", ENDPOINT, _public_pin())
        decide_pin("hetzner", ENDPOINT, _public_pin())
    assert first.outcome == "unavailable"
    assert first.fingerprint == OLD, "the real socket must still be held to the stored pin"
    assert first.update is None
    warnings = [r for r in caplog.records if "vdc.example.org:443" in r.getMessage()]
    assert len(warnings) == 1, "a cluster that stays unreadable is logged once, not every poll"


def test_the_unavailable_warning_is_rearmed_after_a_success(monkeypatch, caplog) -> None:
    pinning._reset_unavailable_warnings()
    with caplog.at_level(logging.WARNING, logger=pinning.logger.name):
        _live(monkeypatch, None, None)
        decide_pin("hetzner", ENDPOINT, _public_pin())
        _live(monkeypatch, OLD, OLD)
        decide_pin("hetzner", ENDPOINT, _public_pin())
        _live(monkeypatch, None, None)
        decide_pin("hetzner", ENDPOINT, _public_pin())
    warnings = [r for r in caplog.records if "vdc.example.org:443" in r.getMessage()]
    assert len(warnings) == 2


def test_pin_state_accepts_the_lineage_only_for_public_ca() -> None:
    lineage = [{"fingerprint": OLD, "pinned_at": None, "replaced_at": None}]
    public = PinState.from_metadata(_public_pin(fingerprint=NEW, previous=lineage))
    assert public.accepts(NEW) and public.accepts(OLD)
    assert not public.accepts(OTHER)
    assert not public.accepts(None)

    tofu = PinState.from_metadata(
        {FINGERPRINT_FIELD: NEW, TRUST_FIELD: TRUST_TOFU, PREVIOUS_FIELD: lineage}
    )
    assert tofu.accepts(NEW)
    assert not tofu.accepts(OLD), "a self-signed pin has no lineage to honour"


def test_pin_state_ignores_a_malformed_lineage() -> None:
    state = PinState.from_metadata(
        _public_pin(previous=[{"fingerprint": 5}, "x", {"fingerprint": "not-hex"}])
    )
    assert state.previous == ()


def test_no_pin_means_no_state() -> None:
    assert PinState.from_metadata({}) is None
    assert PinState.from_metadata(None) is None


def test_repin_resets_the_lineage(monkeypatch) -> None:
    monkeypatch.setattr(pinning, "fetch_public_ca_fingerprint", lambda endpoint, timeout=5: NEW)
    fields = repin_fields(ENDPOINT, NEW)
    assert fields[FINGERPRINT_FIELD] == NEW
    assert fields[TRUST_FIELD] == TRUST_PUBLIC_CA
    assert fields[PREVIOUS_FIELD] == []


class _Vault:
    def __init__(self, stored, *, lose_cas_to=None):
        self.stored = dict(stored)
        self.version = 3
        self.writes = []
        self.lose_cas_to = lose_cas_to

    def vault_read(self, path, use_cache=True):
        assert use_cache is False, "a pin decision is read uncached so a fresh re-pin is not lost"
        return dict(self.stored)

    def vault_read_versioned(self, path):
        return dict(self.stored), self.version

    def vault_kv_put(self, path, data, *, cas=None):
        from app.core.vault_client import VaultCASConflict

        if self.lose_cas_to is not None:
            # Another writer lands between this read and this write.
            self.stored = dict(self.lose_cas_to)
            self.version += 1
            self.lose_cas_to = None
        if cas is not None and cas != self.version:
            raise VaultCASConflict("lost", status_code=400)
        self.writes.append(data)
        self.stored = dict(data)
        self.version += 1


def _manager_with_vault(monkeypatch, vault):
    from app.services.lxd import connection as connection_module

    monkeypatch.setattr("app.core.vault_client.get_vault_client", lambda *args, **kwargs: vault)
    audits = []
    monkeypatch.setattr(
        "app.services.activity_logger.get_activity_logger",
        lambda: type("A", (), {"log_activity": lambda self, **kw: audits.append(kw)})(),
    )
    return connection_module.LXDConnectionManager(), audits


def test_a_renewal_is_written_with_the_rest_of_the_metadata_and_audited(monkeypatch) -> None:
    vault = _Vault({**_public_pin(), "display_name": "Hetzner"})
    manager, audits = _manager_with_vault(monkeypatch, vault)
    manager._write_cluster_pin(
        "hetzner", compared=OLD, update={FINGERPRINT_FIELD: NEW}, outcome="renewed",
        endpoint=ENDPOINT,
    )
    assert vault.writes[0][FINGERPRINT_FIELD] == NEW
    assert vault.writes[0]["display_name"] == "Hetzner", "a pin write must not drop other fields"
    assert audits[0]["action"] == "cluster.server_certificate.renew"
    assert audits[0]["details"]["previous_fingerprint"] == OLD[:16]


def test_a_concurrent_repin_is_not_overwritten(monkeypatch) -> None:
    vault = _Vault(_public_pin(fingerprint=OTHER))
    manager, audits = _manager_with_vault(monkeypatch, vault)
    manager._write_cluster_pin(
        "hetzner", compared=OLD, update={FINGERPRINT_FIELD: NEW}, outcome="renewed",
        endpoint=ENDPOINT,
    )
    assert vault.writes == []
    assert audits == []


def test_a_renewal_another_worker_already_recorded_is_not_written_twice(monkeypatch) -> None:
    vault = _Vault(_public_pin(fingerprint=NEW))
    manager, audits = _manager_with_vault(monkeypatch, vault)
    manager._write_cluster_pin(
        "hetzner", compared=OLD, update={FINGERPRINT_FIELD: NEW}, outcome="renewed",
        endpoint=ENDPOINT,
    )
    assert vault.writes == []
    assert audits == [], "only the worker that changed Vault audits the renewal"


def test_a_pooled_client_bound_to_a_superseded_pin_is_replaced(monkeypatch) -> None:
    """The reconnect verifies the live certificate before any credential is sent."""
    import requests

    from app.services.lxd import connection as connection_module

    session = requests.Session()
    session.mount("https://", connection_module.FingerprintHTTPAdapter(server_fingerprint=OLD))
    stale = type("Client", (), {})()
    stale.api = type("Api", (), {"_api_endpoint": "https://vdc.example.org", "session": session})()

    manager = connection_module.LXDConnectionManager()
    manager._clients["hetzner"] = stale
    monkeypatch.setattr(connection_module.health, "is_tripped", lambda cluster_id: False)
    monkeypatch.setattr(
        "app.core.org_scope.get_current_allowed_clusters", lambda: None
    )
    reconnects = []
    monkeypatch.setattr(
        manager,
        "_auto_reconnect",
        lambda cluster_id, **kwargs: reconnects.append(kwargs) or "fresh",
    )
    assert manager.get_client("hetzner", expected_server_fingerprint=NEW) == "fresh"
    assert "hetzner" not in manager._clients
    assert reconnects == [{"expected_server_fingerprint": NEW}]


def _settings_with_vault(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(
            lxd_pin_server_certificate=True, vault_enabled=True, vault_token="t",
            vault_addr="http://vault", vault_clusters_path="secret/data/clusters",
        ),
    )


def test_a_write_that_loses_a_check_and_set_race_is_not_recorded(monkeypatch) -> None:
    vault = _Vault(_public_pin(), lose_cas_to=_public_pin(fingerprint=OTHER))
    manager, audits = _manager_with_vault(monkeypatch, vault)
    stored = manager._write_cluster_pin(
        "hetzner", compared=OLD, update={FINGERPRINT_FIELD: NEW}, outcome="renewed",
        endpoint=ENDPOINT,
    )
    assert stored == OTHER, "the caller learns which pin won"
    assert vault.writes == []
    assert audits == []


def test_a_connection_that_lost_the_pin_race_is_refused(monkeypatch) -> None:
    """Worker 1 pins the real certificate; worker 2 saw another one and must not connect."""
    _settings_with_vault(monkeypatch)
    _live(monkeypatch, NEW, None)
    vault = _Vault({"endpoint": ENDPOINT}, lose_cas_to={FINGERPRINT_FIELD: OTHER, TRUST_FIELD: TRUST_TOFU})
    manager, _audits = _manager_with_vault(monkeypatch, vault)
    with pytest.raises(LXDCertificatePinError):
        manager._enforce_certificate_pin("hetzner", "https://10.0.0.5:8443")


def test_a_vault_outage_holds_the_socket_to_the_last_pin_seen(monkeypatch) -> None:
    _settings_with_vault(monkeypatch)
    _live(monkeypatch, OLD, None)
    vault = _Vault({FINGERPRINT_FIELD: OLD, TRUST_FIELD: TRUST_TOFU})
    manager, _audits = _manager_with_vault(monkeypatch, vault)
    assert manager._enforce_certificate_pin("lab", "https://10.0.0.5:8443") == OLD

    def unreachable(*args, **kwargs):
        raise RuntimeError("vault down")

    monkeypatch.setattr(vault, "vault_read", unreachable)
    assert manager._enforce_certificate_pin("lab", "https://10.0.0.5:8443") == OLD


def test_a_refresh_is_reused_briefly_and_skips_a_tripped_cluster(monkeypatch) -> None:
    from app.services.lxd import pin_enforcement

    _settings_with_vault(monkeypatch)
    handshakes = []

    def presented(endpoint, timeout=5):
        handshakes.append(endpoint)
        return OLD

    monkeypatch.setattr(pinning, "fetch_server_fingerprint", presented)
    monkeypatch.setattr(pinning, "fetch_public_ca_fingerprint", lambda endpoint, timeout=5: None)
    vault = _Vault({"endpoint": "https://10.0.0.5:8443", FINGERPRINT_FIELD: OLD, TRUST_FIELD: TRUST_TOFU})
    manager, _audits = _manager_with_vault(monkeypatch, vault)
    monkeypatch.setattr(pin_enforcement.health, "is_tripped", lambda cluster_id: cluster_id == "down")

    assert manager.refresh_pin("lab") == OLD
    assert manager.refresh_pin("lab") == OLD
    assert len(handshakes) == 1, "a refresh within the reuse window answers from memory"
    assert manager.refresh_pin("down") is None
    assert len(handshakes) == 1, "a cluster the breaker holds as down is not handshaken"


def test_a_refresh_replaces_a_pooled_client_that_holds_no_pin(monkeypatch) -> None:
    import requests

    from app.services.lxd import connection as connection_module

    _settings_with_vault(monkeypatch)
    _live(monkeypatch, OLD, None)
    vault = _Vault({"endpoint": "https://10.0.0.5:8443", FINGERPRINT_FIELD: OLD, TRUST_FIELD: TRUST_TOFU})
    manager, _audits = _manager_with_vault(monkeypatch, vault)
    session = requests.Session()
    session.mount("https://", connection_module.FingerprintHTTPAdapter(server_fingerprint=None))
    unpinned = type("Client", (), {})()
    unpinned.api = type("Api", (), {"_api_endpoint": "https://10.0.0.5:8443", "session": session})()
    manager._clients["lab"] = unpinned
    monkeypatch.setattr(connection_module.health, "is_tripped", lambda cluster_id: False)
    assert manager.refresh_pin("lab") == OLD
    assert "lab" not in manager._clients

