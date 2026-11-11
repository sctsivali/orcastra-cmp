"""A cluster's client certificate lasts as long as the operator chose (services/lxd_certs/lifetime.py).

Facts this encodes, proven on a Vault 1.17.3 dev server (the dev Vault's version): a ttl above the
role's max_ttl is capped with only a warning; a not_after beyond max_ttl but inside the CA is issued
as asked; a not_after beyond the CA is refused (issuer behaviour `err`); ttl with not_after together
is refused; a not_after in the past is issued, already expired.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import get_settings
from app.models.schemas import CertificateLifetime as L
from app.services.lxd_certs import lifetime
from tests._cert_rotation_fakes import FakeVault

NOW = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "vault_cert_ttl", "720h")
    monkeypatch.setattr(s, "cluster_cert_allow_longest", True)
    return s


def _vault(ca_days=None) -> FakeVault:
    vault = FakeVault()
    if ca_days is not None:
        vault.set_ca(ca_days)
    return vault


@pytest.mark.parametrize("text,expected", [
    ("720h", timedelta(days=30)), ("30d", timedelta(days=30)), ("1h30m", timedelta(minutes=90)),
    ("2592000", timedelta(days=30)), ("90m", timedelta(minutes=90)),
])
def test_vault_durations_are_read(text, expected) -> None:
    assert lifetime.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", None, "abc", "h", "720x", "0h", "1h 30m", "-5h", "5w"])
def test_anything_else_is_not_a_duration(text) -> None:
    assert lifetime.parse_duration(text) is None


def test_a_bare_number_is_seconds_as_vault_reads_it() -> None:
    assert lifetime.parse_duration("720") == timedelta(seconds=720)


def test_the_default_asks_vault_exactly_what_it_always_did_and_reads_no_ca(settings) -> None:
    vault = _vault()  # no CA stored: reading it would raise
    plan = lifetime.plan(L.DEFAULT, vault, now=NOW)
    assert plan.params == {"ttl": "720h"}
    assert plan.requested_until == NOW + timedelta(days=30)


def test_a_period_asks_for_its_ttl(settings) -> None:
    plan = lifetime.plan(L.ONE_YEAR, _vault(ca_days=1800), now=NOW)
    assert plan.params == {"ttl": "8760h"}
    assert plan.requested_until == NOW + timedelta(days=365)


def test_a_period_past_the_ca_is_refused_before_vault_is_asked(settings) -> None:
    vault = _vault(ca_days=100)
    with pytest.raises(lifetime.LifetimeRefused) as refused:
        lifetime.plan(L.SIX_MONTHS, vault)
    assert refused.value.status_code == 422
    assert "certificate authority expires on" in refused.value.detail
    assert vault.issue_params == []


def test_a_period_with_an_unreadable_ca_is_left_to_vault(settings) -> None:
    assert lifetime.plan(L.THREE_MONTHS, _vault(), now=NOW).params == {"ttl": "2160h"}


def test_longest_ends_an_hour_before_the_ca(settings) -> None:
    vault = _vault(ca_days=1825)
    ca_end = lifetime.ca_not_after(vault)
    plan = lifetime.plan(L.LONGEST, vault)
    assert plan.params == {"not_after": (ca_end - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert "ttl" not in plan.params, "Vault refuses ttl and not_after together"


def test_longest_needs_the_ca(settings) -> None:
    with pytest.raises(lifetime.LifetimeRefused) as refused:
        lifetime.plan(L.LONGEST, _vault())
    assert refused.value.status_code == 503


def test_longest_with_a_ca_about_to_expire_is_refused(settings) -> None:
    with pytest.raises(lifetime.LifetimeRefused) as refused:
        lifetime.plan(L.LONGEST, _vault(ca_days=0))
    assert refused.value.status_code == 409


def test_a_deployment_can_turn_longest_off(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "cluster_cert_allow_longest", False)
    with pytest.raises(lifetime.LifetimeRefused) as refused:
        lifetime.plan(L.LONGEST, _vault(ca_days=1825))
    assert refused.value.status_code == 422
    assert all(o.value is not L.LONGEST for o in lifetime.options(_vault(ca_days=1825)).options)


def test_the_issued_expiry_is_read_from_the_certificate(settings) -> None:
    vault = _vault(ca_days=1825)
    issued, outcome = lifetime.issue(vault, "c1.orcastra.io", lifetime.plan(L.ONE_YEAR, vault))
    assert issued["cert"].startswith("-----BEGIN CERTIFICATE-----")
    assert outcome.lifetime is L.ONE_YEAR
    assert abs(outcome.expires_at - (datetime.now(timezone.utc) + timedelta(days=365))) < timedelta(minutes=1)
    assert outcome.shortened is False


def test_a_ttl_vault_capped_is_reported_as_shortened(settings) -> None:
    vault = _vault(ca_days=1825)
    vault.max_ttl_hours = 720  # a role whose max_ttl is 30 days
    _, outcome = lifetime.issue(vault, "c1.orcastra.io", lifetime.plan(L.ONE_YEAR, vault))
    assert outcome.shortened is True
    assert outcome.expires_at < datetime.now(timezone.utc) + timedelta(days=31)


def test_longest_is_not_capped_by_the_role(settings) -> None:
    vault = _vault(ca_days=1825)
    vault.max_ttl_hours = 720
    _, outcome = lifetime.issue(vault, "c1.orcastra.io", lifetime.plan(L.LONGEST, vault))
    assert outcome.shortened is False
    assert outcome.expires_at > datetime.now(timezone.utc) + timedelta(days=1800)


@pytest.mark.parametrize("choice,stored,expected", [
    (L.ONE_YEAR, "720h", L.ONE_YEAR),
    (None, "longest", L.LONGEST),
    (None, "8760h", L.ONE_YEAR),
    (None, None, L.DEFAULT),
    (None, "forever", L.DEFAULT),
])
def test_a_choice_now_wins_then_the_stored_one(choice, stored, expected) -> None:
    assert lifetime.resolve(choice, stored) is expected


def test_resolve_falls_back_to_what_the_caller_names() -> None:
    assert lifetime.resolve(None, None, L.SIX_MONTHS) is L.SIX_MONTHS


def test_the_picker_starts_with_the_default_and_names_its_length(settings) -> None:
    offered = lifetime.options(_vault(ca_days=1825), now=NOW).options
    assert offered[0].value is L.DEFAULT and offered[0].label == "Default (30 days)"
    values = [o.value for o in offered]
    assert L.ONE_MONTH in values, "a stored 30-day choice stays selectable beside the default"
    assert values[-1] is L.LONGEST and offered[-1].available
    assert all(o.available for o in offered)


def test_periods_past_the_ca_are_offered_but_unavailable(settings) -> None:
    offered = {o.value: o for o in lifetime.options(_vault(ca_days=100), now=datetime.now(timezone.utc)).options}
    assert offered[L.TWO_MONTHS].available
    assert not offered[L.SIX_MONTHS].available and "expires on" in offered[L.SIX_MONTHS].reason
    assert not offered[L.ONE_YEAR].available


def test_an_unreadable_ca_leaves_periods_and_explains_longest(settings) -> None:
    offered = {o.value: o for o in lifetime.options(_vault()).options}
    assert offered[L.ONE_YEAR].available
    assert not offered[L.LONGEST].available and "could not be read" in offered[L.LONGEST].reason


def test_a_non_default_deployment_ttl_is_labelled_by_its_length(settings, monkeypatch) -> None:
    monkeypatch.setattr(settings, "vault_cert_ttl", "8760h")
    offered = lifetime.options(_vault(ca_days=1825)).options
    assert offered[0].label == "Default (1 year)"
    assert L.ONE_YEAR in [o.value for o in offered]
