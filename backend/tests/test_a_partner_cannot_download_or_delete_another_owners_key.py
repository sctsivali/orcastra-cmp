"""A My Keys record that names an owner is that owner's alone, on every route that reads it.

`list_keys` already filtered by `owner_email`, but `_authorize_key`, which guards the PFX
download and the delete, checked only that the caller could reach the key's cluster. On a cluster
two partners share, partner B could name partner A's key id and receive A's private key, which may
carry an `admin` LXD identity, or delete it. Answering 404 keeps "not yours" indistinguishable from
"does not exist", the same shape the record-without-a-node branch already uses.
"""

from typing import Any, Dict, Optional

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.routers import my_keys

SHARED = "cluster-shared"
OWNER = "partner-a@example.test"
OTHER = "partner-b@example.test"


def _user(email: str, groups=("role_partner",)) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{groups[0]}:{email}", email=email, groups=list(groups))


class _Vault:
    def __init__(self, records: Dict[str, Dict[str, Any]]):
        self.records = records
        self.deleted: list = []

    def vault_read(self, path: str) -> Optional[Dict[str, Any]]:
        key_id = path.split("/")[-2]
        return self.records.get(key_id)


@pytest.fixture
def vault(monkeypatch):
    store = _Vault({
        "key_a": {"id": "key_a", "node": SHARED, "owner_email": OWNER, "group_name": "key_a"},
        "key_legacy": {"id": "key_legacy", "node": SHARED, "group_name": "key_legacy"},
    })
    monkeypatch.setattr(my_keys, "get_vault_keys_client", lambda: store)
    monkeypatch.setattr(my_keys, "get_key_from_vault", lambda _v, _k: (b"cert", b"key"))
    monkeypatch.setattr(my_keys, "create_pfx", lambda _c, _k: b"pfx")
    return store


@pytest.fixture
def partner_scope():
    token = org_scope._allowed_clusters_var.set({SHARED})
    yield
    org_scope._allowed_clusters_var.reset(token)


async def test_another_partner_on_the_shared_cluster_cannot_download_the_key(vault, partner_scope):
    with pytest.raises(HTTPException) as caught:
        await my_keys.download_key("key_a", _rbac=_user(OTHER))
    assert caught.value.status_code == 404


async def test_the_owner_downloads_their_own_key(vault, partner_scope):
    response = await my_keys.download_key("key_a", _rbac=_user(OWNER))
    assert isinstance(response, StreamingResponse)


async def test_the_owner_match_ignores_email_case(vault, partner_scope):
    response = await my_keys.download_key("key_a", _rbac=_user(OWNER.upper()))
    assert isinstance(response, StreamingResponse)


async def test_a_record_that_predates_the_owner_field_stays_on_cluster_scope(vault, partner_scope):
    response = await my_keys.download_key("key_legacy", _rbac=_user(OTHER))
    assert isinstance(response, StreamingResponse)


async def test_an_administrator_reads_any_key(vault):
    # An administrator's request carries no cluster restriction (None), so no owner applies.
    response = await my_keys.download_key("key_a", _rbac=_user("ops@example.test", ("role_admin",)))
    assert isinstance(response, StreamingResponse)


async def test_another_partner_cannot_delete_the_key(vault, partner_scope):
    with pytest.raises(HTTPException) as caught:
        await my_keys.delete_key("key_a", lxd_service=object(), _=None, _rbac=_user(OTHER))
    assert caught.value.status_code == 404
