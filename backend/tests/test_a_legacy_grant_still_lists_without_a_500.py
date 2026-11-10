"""A grant already in Vault must never be re-judged by a rule written after it.

`_key_info` builds `APIKeyInfo(**metadata)` from raw Vault JSON, and
`APIKeyInfo.cluster_permissions` is a list of `ClusterPermission`, so a validator on that model
runs over every stored grant on every read. `list_api_keys` builds every key inside one list
comprehension under an `except Exception` that answers 500, so a single stored key with
`permissions: []` or a retired level would take the whole key list down without naming the key.

The validators live on `ClusterPermissionRequest`, which every request model uses, and the read
model stays lenient. These tests pin both halves.
"""

import asyncio
from typing import List

import pytest

from app.routers.integrations import (
    APIKeyInfo,
    APIKeyValidateResponse,
    ClusterPermission,
    ClusterPermissionRequest,
    ExternalUserMapping,
    _key_info,
)

CLUSTER = "nuc2"


def _metadata(key_id="oak_1", **grant):
    base = {"cluster_id": CLUSTER, "permissions": ["read"], "projects": ["default"]}
    base.update(grant)
    return {
        "id": key_id,
        "name": f"key {key_id}",
        "app_name": "QA",
        "created_at": "2026-01-01T00:00:00",
        "is_active": True,
        "cluster_permissions": [base],
    }


LEGACY_SHAPES = [
    ("a grant that grants nothing", {"permissions": []}),
    ("a permission level nobody uses any more", {"permissions": ["owner"]}),
    ("an untrimmed project name", {"projects": ["  Tenant A  "]}),
    ("a non-ascii project name", {"projects": ["defaułt"]}),
    ("an empty project list", {"projects": []}),
    ("a project list that is not a list of names", {"projects": ["a", "a", ""]}),
]


@pytest.mark.parametrize("label,grant", LEGACY_SHAPES, ids=[s[0] for s in LEGACY_SHAPES])
def test_a_stored_grant_still_lists(label, grant):
    info = _key_info(_metadata(**grant))
    assert info.cluster_permissions[0].cluster_id == CLUSTER


def test_the_read_model_does_not_normalise_what_it_reads():
    """Reading must not rewrite. The stored value is the grant the runtime enforces."""
    info = _key_info(_metadata(projects=["  Tenant A  ", "  Tenant A  "]))
    assert info.cluster_permissions[0].projects == ["  Tenant A  ", "  Tenant A  "]


def test_one_bad_key_does_not_hide_every_other_key(monkeypatch):
    """Three keys, one malformed: all three still list."""
    import app.routers.integrations as mod

    records = [
        _metadata("oak_good_1"),
        _metadata("oak_legacy", permissions=[]),
        _metadata("oak_good_2"),
    ]

    class _Cache:
        def get(self, *a, **kw):
            return None

        def set(self, *a, **kw):
            return None

    monkeypatch.setattr(mod, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(mod, "list_api_keys_from_vault", lambda client: records)
    monkeypatch.setattr(mod, "get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(mod, "get_scope_cache_key", lambda key: key)
    monkeypatch.setattr(mod, "get_current_allowed_clusters", lambda: None)
    monkeypatch.setattr(mod, "get_user_mappings_from_vault", lambda client, key_id: [])

    from app.core.auth import AuthenticatedUser

    # An Admin, so which side manages a key never needs the database. Ownership is not read
    # either (no session), which leaves its counts null and the list itself untouched.
    admin = AuthenticatedUser(user_id="a1", email="admin@example.test", groups=["role_admin"])
    response = asyncio.run(mod.list_api_keys(user=admin, db=None))

    assert response.total == 3
    assert [key.id for key in response.keys] == ["oak_good_1", "oak_legacy", "oak_good_2"]


def test_a_stored_user_mapping_with_a_legacy_grant_still_builds():
    """`get_user_mappings_from_vault` runs inside the same `try` as `_key_info`.

    A malformed mapping is a second route to the same whole-list 500, so the mapping model stays
    lenient for the same reason the key model does.
    """
    mapping = ExternalUserMapping(
        external_user_id="u1",
        external_email="u1@example.com",
        permissions=[{"cluster_id": CLUSTER, "permissions": [], "projects": []}],
    )
    assert mapping.permissions[0].permissions == []


def test_validate_and_whoami_tolerate_a_legacy_grant():
    """Both build ClusterPermission directly from a stored dict rather than from a request."""
    grant = ClusterPermission(cluster_id=CLUSTER, permissions=["owner"], projects=[])
    response = APIKeyValidateResponse(valid=True, cluster_permissions=[grant])
    assert response.cluster_permissions[0].permissions == ["owner"]


def test_the_response_model_is_not_the_request_model():
    """A structural guard: merging the two models back is a one-line refactor.

    The rest of the suite would stay green until a stored grant fails the newer rule, and the
    key list would answer 500.
    """
    assert APIKeyInfo.model_fields["cluster_permissions"].annotation == List[ClusterPermission]
    assert (
        ExternalUserMapping.model_fields["permissions"].annotation == List[ClusterPermission]
    )
    assert (
        APIKeyValidateResponse.model_fields["cluster_permissions"].annotation
        == List[ClusterPermission]
    )


def test_the_request_models_all_use_the_strict_grant():
    from app.routers.integrations import APIKeyCreateRequest, UserMappingRequest

    assert (
        APIKeyCreateRequest.model_fields["cluster_permissions"].annotation
        == List[ClusterPermissionRequest]
    )
    assert (
        UserMappingRequest.model_fields["cluster_permissions"].annotation
        == List[ClusterPermissionRequest]
    )


def test_the_update_endpoint_takes_the_strict_grant():
    """An update can widen a grant, so it validates like creation does."""
    import inspect

    from app.routers.integrations import update_api_key_permissions

    annotation = inspect.signature(update_api_key_permissions).parameters["permissions"].annotation
    assert annotation == List[ClusterPermissionRequest]
