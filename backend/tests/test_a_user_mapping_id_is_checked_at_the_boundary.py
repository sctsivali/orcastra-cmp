"""
A user-mapping id is checked where it enters.

`external_user_id` becomes a Vault KV path segment in save_user_mapping_to_vault, and it was
unconstrained, so `../../../../<anything>` steered the write. The Vault layer now refuses a
traversal on its own (safe_vault_url), and the request model refuses the id before any work is
done. The stored-mapping model stays lenient, so a legacy id still lists.
"""

import uuid

import pytest
from pydantic import ValidationError

from app.routers.integrations import ExternalUserMapping, UserMappingRequest

REFUSED = [
    "../../../../clusters/cluster-a/certs",
    "a/b",
    "..",
    ".",
    "",
    "a b",
    "a?b",
    "a#b",
    "a%2e",
    "-x",
    "x-",
    "x.",
    "user\n",
    "x" * 129,
]

ACCEPTED = [
    "tenant@example.test",
    "ak-0f3c9a",
    "12345",
    "tenant_1|alt",
    "_x",
    "a",
    str(uuid.UUID("5b1e0a4c-0000-4000-8000-000000000001")),
    "x" * 128,
]


@pytest.mark.parametrize("value", REFUSED)
def test_an_id_that_could_steer_a_path_is_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        UserMappingRequest(external_user_id=value, external_email="tenant@example.test")


@pytest.mark.parametrize("value", ACCEPTED)
def test_ordinary_ids_are_accepted(value: str) -> None:
    request = UserMappingRequest(external_user_id=value, external_email="tenant@example.test")
    assert request.external_user_id == value


def test_a_stored_legacy_id_still_lists() -> None:
    mapping = ExternalUserMapping(external_user_id="legacy id", external_email="x@example.test")
    assert mapping.external_user_id == "legacy id"
