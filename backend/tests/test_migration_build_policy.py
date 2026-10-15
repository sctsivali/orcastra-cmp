"""Which daemon builds may take part in a migration: deny, then allow, then the LXD series policy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.services.migrations.build_policy import (
    build_token,
    build_verdict,
    parse_version,
    token_set,
)


def _settings(deny: Any = "", allow: Any = "", approved: Any = "") -> SimpleNamespace:
    return SimpleNamespace(
        migration_build_deny=deny,
        migration_build_allow=allow,
        migration_approved_builds=approved,
    )


def _lxd(version: Optional[str]) -> dict[str, Any]:
    return {"engine": "lxd", "version": version}


@pytest.mark.parametrize(
    ("version", "approved", "basis"),
    [
        ("5.0.8", True, "policy"),
        ("5.0.10", True, "policy"),
        ("5.0.7", False, "below_patched_minimum"),
        ("5.0", False, "below_patched_minimum"),
        ("5.21.6", True, "policy"),
        ("5.21.12", True, "policy"),
        ("5.21.5", False, "below_patched_minimum"),
        ("6.10", True, "policy"),
        ("6.10.0", True, "policy"),
        ("6.11", True, "policy"),
        ("6.9.3", False, "below_patched_minimum"),
        ("6.1", False, "below_patched_minimum"),
        ("6.0", False, "below_patched_minimum"),
        ("4.0.9", False, "unsupported_series"),
        ("5.1", False, "unsupported_series"),
        ("5.20.2", False, "unsupported_series"),
        ("7.0", False, "newer_series"),
    ],
)
def test_lxd_builds_are_judged_by_the_patched_minimum_of_their_series(
    version: str, approved: bool, basis: str
) -> None:
    verdict = build_verdict(_lxd(version), _settings())

    assert (verdict.approved, verdict.basis) == (approved, basis)


def test_a_refused_build_names_the_first_patched_build_of_its_series() -> None:
    verdict = build_verdict(_lxd("5.21.5"), _settings())

    assert "5.21.6" in verdict.reason


def test_a_series_newer_than_the_policy_is_refused_until_allowlisted() -> None:
    verdict = build_verdict(_lxd("7.2.1"), _settings())

    assert verdict.approved is False
    assert verdict.basis == "newer_series"
    assert "MIGRATION_BUILD_ALLOW" in verdict.reason
    assert "GHSA-5g5r-wh97-qcq2" in verdict.reason


def test_a_newer_series_passes_once_an_operator_allowlists_it() -> None:
    verdict = build_verdict(_lxd("7.2.1"), _settings(allow="lxd:7.2.1"))

    assert verdict.approved is True
    assert verdict.basis == "allow"


def test_the_deny_list_wins_over_the_allow_list() -> None:
    verdict = build_verdict(_lxd("6.10"), _settings(deny="lxd:6.10", allow="lxd:6.10"))

    assert (verdict.approved, verdict.basis) == (False, "deny")


def test_the_deny_list_refuses_a_build_the_series_policy_would_admit() -> None:
    verdict = build_verdict(_lxd("6.11"), _settings(deny="lxd:6.11"))

    assert (verdict.approved, verdict.basis) == (False, "deny")


def test_the_allow_list_admits_a_build_the_series_policy_would_refuse() -> None:
    verdict = build_verdict(_lxd("5.0.2"), _settings(allow="lxd:5.0.2"))

    assert (verdict.approved, verdict.basis) == (True, "allow")


def test_the_legacy_approved_builds_setting_is_read_as_the_allow_list() -> None:
    verdict = build_verdict(_lxd("5.0.2"), _settings(approved="lxd:5.0.2"))

    assert (verdict.approved, verdict.basis) == (True, "allow")


def test_an_incus_build_is_refused_unless_it_is_allow_listed() -> None:
    identity = {"engine": "incus", "version": "6.0.1"}

    refused = build_verdict(identity, _settings())
    admitted = build_verdict(identity, _settings(allow="incus:6.0.1"))

    assert (refused.approved, refused.basis) == (False, "not_allowlisted")
    assert "MIGRATION_BUILD_ALLOW" in refused.reason
    assert (admitted.approved, admitted.basis) == (True, "allow")


def test_list_tokens_match_without_regard_to_case_or_padding() -> None:
    identity = {"engine": "Incus", "version": "6.0.1"}

    from_text = build_verdict(identity, _settings(allow=" other:1.0 ,  INCUS:6.0.1 "))
    from_list = build_verdict(identity, _settings(allow=["INCUS:6.0.1"]))

    assert from_text.approved is True
    assert from_list.approved is True


@pytest.mark.parametrize("version", [None, "", "git-4f2a", "v6.10", "six"])
def test_an_unreadable_lxd_version_is_refused(version: Optional[str]) -> None:
    verdict = build_verdict(_lxd(version), _settings())

    assert (verdict.approved, verdict.basis) == (False, "unreadable_version")


def test_a_build_without_an_engine_is_never_judged_as_lxd() -> None:
    verdict = build_verdict({"version": "6.10"}, _settings())

    assert (verdict.approved, verdict.basis) == (False, "not_allowlisted")


def test_settings_without_build_lists_fall_back_to_the_series_policy() -> None:
    assert build_verdict(_lxd("6.10"), object()).basis == "policy"
    assert build_verdict(_lxd("6.9"), object()).basis == "below_patched_minimum"


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("5.21", (5, 21, 0)),
        ("6.10.1-rc1", (6, 10, 1)),
        (" 5.0.8 ", (5, 0, 8)),
        ("6", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_version_reads_major_minor_and_optional_patch(
    raw: Optional[str], parsed: Optional[tuple[int, int, int]]
) -> None:
    assert parse_version(raw) == parsed


def test_build_token_and_token_set_normalise_to_lowercase() -> None:
    assert build_token({"engine": "LXD", "version": "6.10"}) == "lxd:6.10"
    assert token_set("A:1, ,b:2", None, ["C:3"]) == {"a:1", "b:2", "c:3"}
