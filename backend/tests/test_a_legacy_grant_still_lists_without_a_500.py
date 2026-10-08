"""A grant already in Vault must never be re-judged by a rule written after it.

The `permissions` validator sat on `ClusterPermission` under a docstring asserting that stored
keys "are read back without passing through this model". They are not. `_key_info` builds
`APIKeyInfo(**metadata)` straight from raw Vault JSON, and `APIKeyInfo.cluster_permissions` is a
list of that same model, so the rule ran over every historical grant on every read.

`list_api_keys` builds every key inside one list comprehension wrapped in a bare
`except Exception` that answers 500. So a single stored key with `permissions: []`, or with a
level nobody uses any more, did not show up as the one broken key: it took the entire key list
down with it, leaving the operator with no key-management surface at all and no clue which key
caused it.

The fix was structural rather than a loosened rule: validators moved to
`ClusterPermissionRequest`, which every request model now uses, and the read model went back to
being lenient. These tests pin both halves, because the failure is invisible until the day
somebody's Vault happens to hold the wrong shape.
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
    """The defect in one sentence: three keys, one malformed, and all three vanish."""
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

    # note: an Admin, so which side manages a key never needs the database. Ownership is not read
    # either (no session), which leaves its counts null and the list itself untouched.
    admin = AuthenticatedUser(user_id="a1", email="admin@example.test", groups=["role_admin"])
    response = asyncio.run(mod.list_api_keys(user=admin, db=None))

    assert response.total == 3
    assert [key.id for key in response.keys] == ["oak_good_1", "oak_legacy", "oak_good_2"]


def test_a_stored_user_mapping_with_a_legacy_grant_still_builds():
    """`get_user_mappings_from_vault` runs inside the same comprehension as `_key_info`.

    So a malformed mapping is a second route to the same whole-list 500, and the mapping model
    has to stay lenient for the same reason the key model does.
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
    """A structural guard, because collapsing the two back together is a one-line refactor.

    It would also be an invisible one: the suite stays green until a real Vault holds a shape
    the newer rule dislikes, and then the failure is a 500 on a list nobody changed.
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
    """The widening door. Creation validated and the update did not, once before."""
    import inspect

    from app.routers.integrations import update_api_key_permissions

    annotation = inspect.signature(update_api_key_permissions).parameters["permissions"].annotation
    assert annotation == List[ClusterPermissionRequest]
