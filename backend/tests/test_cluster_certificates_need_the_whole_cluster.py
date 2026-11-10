"""A cluster's client certificate is read, renewed, verified or cancelled only by its whole holder.

The certificate and its renewal act for every project on the cluster, so the cluster scope alone is
not enough: on a cluster another party holds, a partner is refused, and the list leaves that cluster
out. The party is read from real rows on SQLite; the certificate store, rotation and trust cleanup
are recorders, so a refusal is proven to land before any of them.
"""

from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.whole_cluster_access import SHARED_CLUSTER_DETAIL
from app.models.cert_trust_schemas import TrustCleanupRequest
from app.models.schemas import CertificateRenewalRequest, CertificateVerifyRequest
from app.routers import cert_manager as certs
from tests.test_whole_cluster_state_needs_the_whole_cluster import ADMIN, MIXED, OWN, SELLER, SOLO, _world

REACHED = 418


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


class _Store:
    """The certificate manager: lists every registered cluster, records nothing else."""

    def list_clusters(self, use_cache: bool = True) -> List[str]:
        return [OWN, MIXED, SOLO]


@pytest.fixture
def scope():
    clusters = org_scope._allowed_clusters_var.set({OWN, MIXED, SOLO})
    bound = org_scope._visible_projects_var.set({MIXED: {"default"}})
    yield
    org_scope._visible_projects_var.reset(bound)
    org_scope._allowed_clusters_var.reset(clusters)


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[str]]:
    seen: Dict[str, List[str]] = {"rows": [], "pending": [], "rotation": [], "sweep": []}

    async def _rows(_store: Any, cluster_ids: List[str]) -> list:
        seen["rows"].extend(cluster_ids)
        return []

    async def _stop(kind: str, cluster: str) -> Any:
        seen[kind].append(cluster)
        raise HTTPException(status_code=REACHED, detail=f"{kind} was reached")

    async def _pending(_store: Any, cluster: str) -> Any:
        return await _stop("pending", cluster)

    async def _renew(_db: Any, _store: Any, request: Any) -> Any:
        return await _stop("rotation", request.cluster_id)

    async def _verify(_db: Any, _store: Any, cluster: str, _pending_id: Any) -> Any:
        return await _stop("rotation", cluster)

    async def _cancel(_db: Any, _store: Any, cluster: str, **_k: Any) -> Any:
        return await _stop("rotation", cluster)

    async def _sweep(*_a: Any, **k: Any) -> Any:
        return await _stop("sweep", k.get("cluster_id") or next(a for a in _a if a in (OWN, MIXED, SOLO)))

    monkeypatch.setattr(certs, "_read_rows", _rows)
    monkeypatch.setattr(certs.pending_certs, "pending_view", _pending)
    monkeypatch.setattr(certs.rotation, "renew", _renew)
    monkeypatch.setattr(certs.rotation, "verify", _verify)
    monkeypatch.setattr(certs.rotation, "cancel", _cancel)
    monkeypatch.setattr(certs.trust_cleanup, "sweep_cluster", _sweep)
    return seen


def _routes(cluster: str, user: AuthenticatedUser, db: Any) -> Dict[str, Any]:
    store = _Store()
    return {
        "get": lambda: certs.get_certificate(cluster, store, db, user),
        "pending": lambda: certs.get_pending_certificate(cluster, store, db, user),
        "renew": lambda: certs.initiate_renewal(CertificateRenewalRequest(cluster_id=cluster), store, db, None, user),
        "verify": lambda: certs.verify_renewal(CertificateVerifyRequest(cluster_id=cluster), store, db, None, user),
        "cancel": lambda: certs.cancel_renewal(cluster, store, db, None, user),
        "remove-superseded": lambda: certs.remove_superseded_trust(cluster, TrustCleanupRequest(), store, db, None, user),
    }


ROUTES = list(_routes(OWN, _user(SELLER), None))


@pytest.mark.parametrize("cluster", [MIXED, SOLO], ids=["held-beside-another-party", "held-by-another-party-alone"])
@pytest.mark.parametrize("route", ROUTES)
async def test_another_partys_certificate_is_refused_before_it_is_read(scope, reached, route: str, cluster: str) -> None:
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await _routes(cluster, _user(SELLER), db)[route]()
    assert caught.value.status_code == 403 and caught.value.detail == SHARED_CLUSTER_DETAIL
    assert reached == {"rows": [], "pending": [], "rotation": [], "sweep": []}


@pytest.mark.parametrize("route", ROUTES)
async def test_the_sellers_own_certificate_after_a_sale_is_the_sellers(scope, reached, route: str) -> None:
    db = await _world()
    try:
        await _routes(OWN, _user(SELLER), db)[route]()
    except HTTPException as exc:
        assert exc.status_code != 403, exc.detail
    assert OWN in sum(reached.values(), [])


@pytest.mark.parametrize("route", ROUTES)
async def test_an_admin_reaches_any_certificate(reached, route: str) -> None:
    db = await _world()
    try:
        await _routes(SOLO, _user(ADMIN, "role_admin"), db)[route]()
    except HTTPException as exc:
        assert exc.status_code != 403, exc.detail
    assert SOLO in sum(reached.values(), []) and db.statements == []


async def test_the_list_keeps_only_the_clusters_the_seller_holds_whole(scope, reached) -> None:
    db = await _world()
    answer = await certs.list_certificates(_Store(), db, _user(SELLER))
    assert reached["rows"] == [OWN] and answer.total == 0


async def test_an_admin_lists_every_cluster(reached) -> None:
    db = await _world()
    await certs.list_certificates(_Store(), db, _user(ADMIN, "role_admin"))
    assert reached["rows"] == [OWN, MIXED, SOLO]
