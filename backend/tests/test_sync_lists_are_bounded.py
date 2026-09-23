"""The lists a sync may carry are bounded at the boundary.

Every pushed organization costs a SELECT and a commit, every member also costs Authentik calls,
and the refresh path fetched its list from a URL the integration chose. None of them had a
ceiling. One real OrcaHub push is its org plus buyer orgs and their members, far below these.
"""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers import integrations
from app.routers.integrations import (
    MAX_SYNC_MEMBERS,
    MAX_SYNC_ORGANIZATIONS,
    MAX_SYNC_POLICIES,
    MAX_SYNC_POLICY_RULES,
    SyncDataRequest,
    SyncPoliciesRequest,
    SyncPolicy,
)

ORG = {"id": "ext-1", "name": "Org", "slug": "org-1"}
MEMBER = {"email": "tenant@example.test", "organization_id": "ext-1", "role": "TENANT"}
POLICY = {"external_id": "p", "name": "p", "org_external_id": "ext-1"}
RULE = {"cluster_id": "cluster-a"}
ASSIGNMENT = {"user_email": "tenant@example.test"}


def test_the_caps_are_what_the_design_names():
    assert (MAX_SYNC_ORGANIZATIONS, MAX_SYNC_MEMBERS, MAX_SYNC_POLICIES, MAX_SYNC_POLICY_RULES) == (
        1000, 5000, 1000, 1000,
    )


def test_organizations_at_the_cap_pass_and_one_over_is_refused():
    assert len(SyncDataRequest(organizations=[ORG] * MAX_SYNC_ORGANIZATIONS).organizations) == 1000
    with pytest.raises(ValidationError):
        SyncDataRequest(organizations=[ORG] * (MAX_SYNC_ORGANIZATIONS + 1))


def test_members_over_the_cap_are_refused():
    assert len(SyncDataRequest(members=[MEMBER] * MAX_SYNC_MEMBERS).members) == MAX_SYNC_MEMBERS
    with pytest.raises(ValidationError):
        SyncDataRequest(members=[MEMBER] * (MAX_SYNC_MEMBERS + 1))


def test_policies_over_the_cap_are_refused():
    with pytest.raises(ValidationError):
        SyncPoliciesRequest(policies=[POLICY] * (MAX_SYNC_POLICIES + 1))


def test_rules_over_the_cap_are_refused():
    with pytest.raises(ValidationError):
        SyncPolicy(**POLICY, rules=[RULE] * (MAX_SYNC_POLICY_RULES + 1))


def test_assignments_over_the_cap_are_refused():
    with pytest.raises(ValidationError):
        SyncPolicy(**POLICY, assignments=[ASSIGNMENT] * (MAX_SYNC_MEMBERS + 1))


def test_a_policy_without_rules_still_constructs():
    policy = SyncPolicy(**POLICY)
    assert policy.rules == [] and policy.assignments == []


def test_a_refreshed_list_over_the_cap_is_a_502():
    with pytest.raises(HTTPException) as caught:
        integrations._parse_refreshed_organizations(
            {"organizations": [ORG] * (MAX_SYNC_ORGANIZATIONS + 1)}
        )
    assert caught.value.status_code == 502


@pytest.mark.parametrize("payload", [
    ["not", "a", "dict"],
    {"organizations": "nope"},
    {"organizations": [{"id": "x"}]},
    {"organizations": [None]},
    {"organizations": ["string"]},
])
def test_a_malformed_refresh_payload_is_a_502_not_a_500(payload):
    with pytest.raises(HTTPException) as caught:
        integrations._parse_refreshed_organizations(payload)
    assert caught.value.status_code == 502
    assert caught.value.detail == "External app returned an unusable organization list"


def test_a_usable_refresh_payload_parses():
    assert [o.slug for o in integrations._parse_refreshed_organizations({"organizations": [ORG]})] == [
        "org-1"
    ]
    assert integrations._parse_refreshed_organizations({}) == []


async def test_a_refresh_over_the_cap_upserts_nothing(monkeypatch):
    import json

    from app.core.safe_http import GuardedResponse

    upserts = []

    async def recorder(method, url, **kwargs):
        body = {"organizations": [ORG] * (MAX_SYNC_ORGANIZATIONS + 1)}
        return GuardedResponse(200, "application/json", json.dumps(body).encode())

    async def upsert(*args, **kwargs):
        upserts.append(args)
        return 0

    monkeypatch.setattr(integrations, "guarded_request", recorder)
    monkeypatch.setattr(integrations, "_upsert_synced_organizations", upsert)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(
        integrations,
        "get_api_key_from_vault",
        lambda _v, _k: ({"id": "oak_test", "app_url": "https://example.test"}, "h"),
    )
    with pytest.raises(HTTPException) as caught:
        await integrations.refresh_organizations(api_key_id="oak_test", db=None)
    assert caught.value.status_code == 502
    assert upserts == []
