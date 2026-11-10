"""An Incus key is refused below the security baseline, before any certificate exists.

Below Incus 7.6.0, or the LTS point releases 7.0.3 and 6.0.8, a project certificate is not held to
its project: a restricted client can create or read resources in the default project
(GHSA-5c64-j5m6-8433, GHSA-8r3h-vc36-6xpx, GHSA-jcw9-v2ff-8m79). A key there would be an admin
credential, so it is refused with the reason and the build to move to, and nothing is minted or
posted.

A key whose record cannot be stored also loses its cluster credential: the private key exists only
in the failed request, so it could not be revoked afterwards.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.routers import my_keys
from app.services import lxd_key_bounds
from tests.test_an_incus_key_is_a_restricted_certificate_on_its_project import _Incus, _Store


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(user_id="role_partner:p@example.test", email="p@example.test", groups=["role_partner"])


@pytest.mark.parametrize(
    "version,allowed,minimum",
    [("7.6", True, "7.6.0"), ("7.5.1", False, "7.6.0"), ("7.0.2", False, "7.0.3"), ("6.0.7", False, "6.0.8"), ("6.0.8", True, "6.0.8")],
)
def test_the_capability_names_the_build_keys_need(version, allowed, minimum):
    capability = my_keys._key_capability(_Incus(_Store(), version), "incus-lts")
    assert (capability.engine, capability.allowed, capability.minimum) == ("incus", allowed, minimum)
    if not allowed:
        assert f"Incus {version}" in capability.reason and minimum in capability.reason


def test_lxd_keeps_its_behaviour():
    client = SimpleNamespace(host_info={"auth": "trusted", "environment": {"server": "lxd", "server_version": "5.21.8"}})
    capability = my_keys._key_capability(client, "nuc2")
    assert (capability.engine, capability.allowed) == ("lxd", True)


def test_an_engine_not_known_yet_is_refused_as_unavailable():
    client = SimpleNamespace(host_info={"auth": "untrusted"})
    with pytest.raises(HTTPException) as caught:
        my_keys._key_engine(client, "incus-lab")
    assert caught.value.status_code == 503


@pytest.fixture
def issuing(monkeypatch):
    """Bound, Vault and minting stubbed; the cluster is a fake Incus trust store."""
    seen = {"minted": [], "saved": []}

    async def _authorize(node, project, level, db, user):
        return SimpleNamespace(project=project or "default", isolated_project_required=False)

    def _certificate(common_name, days):
        seen["minted"].append(common_name)
        return b"CERT", b"KEY", "f" * 64

    monkeypatch.setattr(my_keys, "authorize_new_key", _authorize)
    monkeypatch.setattr(my_keys, "verify_cluster_access", lambda node: None)
    monkeypatch.setattr(my_keys, "get_vault_keys_client", lambda: object())
    monkeypatch.setattr(my_keys, "generate_certificate", _certificate)
    monkeypatch.setattr(lxd_key_bounds, "authorize_new_key", _authorize)
    return seen


def _service(client):
    return SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda node: client))


async def test_a_key_below_the_floor_is_refused_before_anything_is_minted_or_posted(issuing):
    store = _Store()
    request = my_keys.KeyCreateRequest(name="ci", node="incus-lts", project="web", auth_level="standard", expire_days=30)
    with pytest.raises(HTTPException) as caught:
        await my_keys.create_key(request, _service(_Incus(store, "7.0.2")), None, _user(), None)
    assert caught.value.status_code == 409
    assert caught.value.detail["error"] == "below_floor" and "7.0.3" in caught.value.detail["message"]
    assert issuing["minted"] == [] and store.posts == []


async def test_a_key_that_cannot_be_stored_takes_its_certificate_with_it(issuing, monkeypatch):
    from tests.test_an_incus_key_is_a_restricted_certificate_on_its_project import _mint_factory

    store = _Store()
    mint, _ = _mint_factory(store)
    monkeypatch.setattr(my_keys, "generate_certificate", lambda cn, days: mint(cn))

    def _vault_down(*_args, **_kwargs):
        raise RuntimeError("vault sealed")

    monkeypatch.setattr(my_keys, "save_key_to_vault", _vault_down)
    request = my_keys.KeyCreateRequest(name="ci", node="incus-lab", project="web", auth_level="standard", expire_days=30)
    with pytest.raises(HTTPException) as caught:
        await my_keys.create_key(request, _service(_Incus(store, "7.6")), None, _user(), None)
    assert caught.value.status_code == 500
    assert len(store.posts) == 1 and store.entries == {} and len(store.deleted) == 1


async def test_a_key_for_a_project_that_does_not_exist_is_refused_before_anything_is_minted(issuing):
    """The project is looked up first, on either engine; otherwise the daemon refuses the
    certificate with "Project not found" and the create answers 500."""
    from pylxd import exceptions as lxd_exceptions
    from tests.test_an_incus_key_is_a_restricted_certificate_on_its_project import _Response

    store = _Store()
    client = _Incus(store, "7.6")

    class _Projects:
        def __getitem__(self, name):
            class _One:
                def get(self, timeout=None):
                    raise lxd_exceptions.NotFound(_Response(404, {"error": "Project not found"}))
            return _One()

    client.api.projects = _Projects()
    request = my_keys.KeyCreateRequest(name="ci", node="incus-lab", project="nope", auth_level="standard", expire_days=30)
    with pytest.raises(HTTPException) as caught:
        await my_keys.create_key(request, _service(client), None, _user(), None)
    assert caught.value.status_code == 404 and "nope" in caught.value.detail
    assert issuing["minted"] == [] and store.posts == []


async def test_a_worker_that_never_connected_to_the_cluster_connects_to_create(issuing):
    """get_client is memory-only (services/lxd/lazy_client.py), so the create connects first
    instead of answering 404 "not connected" on a worker with no pooled client."""
    store = _Store()
    pool: dict = {}

    def _ensure(node, **_kw):
        pool[node] = _Incus(store, "7.0.2")
        return True

    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda node, **_kw: pool.get(node)),
                              ensure_connection=_ensure)
    request = my_keys.KeyCreateRequest(name="ci", node="incus-lts", project="web", auth_level="standard", expire_days=30)
    with pytest.raises(HTTPException) as caught:
        await my_keys.create_key(request, service, None, _user(), None)
    # The floor answered, so the create reached the cluster.
    assert caught.value.status_code == 409 and caught.value.detail["error"] == "below_floor"
