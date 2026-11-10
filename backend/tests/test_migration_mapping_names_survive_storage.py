"""Mapping names are infrastructure names, not secrets, and must reach the worker unchanged.

Key-based secret redaction on the stored plan request would turn a network called `private` or
a profile called `cloud-init` into `[redacted]`: the plan previews fine, then execute rebuilds
the request from storage and fails on a network named `[redacted]`.
"""

from __future__ import annotations

from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.migrations.planning import stored_request_payload


def _request(**mappings: dict[str, str]) -> dict:
    return MigrationPlanCreateRequest.model_validate(
        {
            "scope": "instance",
            "intent": "move",
            "source": {"cluster_id": "a", "project": "web", "resource_name": "db"},
            "target": {"cluster_id": "b", "project": "web", "resource_name": "db"},
            "mappings": mappings,
        }
    ).model_dump(mode="json")


def test_mapping_names_with_secret_like_words_round_trip() -> None:
    payload = _request(
        networks={"private": "lan", "private-lan": "lxdbr1"},
        profiles={"cloud-init": "ci", "token-relay": "relay"},
        storage_pools={"root": "secret-ssd"},
    )

    stored = stored_request_payload(payload)

    assert stored["mappings"] == payload["mappings"]
    assert MigrationPlanCreateRequest.model_validate(stored).mappings.networks["private"] == "lan"


def test_a_dotted_profile_name_is_not_taken_for_a_token() -> None:
    name = "organization-base.webserver-tier.production-env1"
    payload = _request(profiles={name: name})

    assert stored_request_payload(payload)["mappings"]["profiles"] == {name: name}


def test_everything_outside_the_mappings_is_still_sanitized() -> None:
    payload = _request()
    payload["source"]["project"] = "x" * 3000

    stored = stored_request_payload(payload)

    assert len(stored["source"]["project"]) <= 2000
