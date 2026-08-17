"""Scope enforcement on the API-key-facing external router.

Two ways a valid integration key could reach a cluster or project it was never granted. Both
were live in 1.0.0-RC6 and both are authorization defects, not availability ones.

  1. `provision-tenant-access` never consulted `cluster_permissions` at all, so any key could
     bind another partner's cluster to its own org and grant its tenants operate/console on it.
     Its own sibling `revoke-tenant-access` did check, which is what made the omission obvious.

  2. `raw-proxy` validated `request.project` and then pasted every query key parsed out of
     `request.path` into the outgoing URL. Declaring project="default" passed the check and
     also skipped the override (which only fires for a non-default project), so a
     `?project=<other>` carried in the path reached LXD verbatim. The cluster certificates are
     registered unrestricted, so LXD honours it.

Both tests fail against the code as it shipped in RC6.
"""

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.routers.integrations import (
    ProvisionTenantAccessRequest,
    RawProxyRequest,
    provision_tenant_access,
    raw_proxy,
)

GRANTED = "cluster-granted"
FOREIGN = "cluster-foreign"


def _key(cluster_id: str = GRANTED, projects: Optional[List[str]] = None) -> Dict[str, Any]:
    """An API key scoped to exactly one cluster, and by default only its `default` project."""
    return {
        "id": "key-under-test",
        "cluster_permissions": [
            {
                "cluster_id": cluster_id,
                "projects": ["default"] if projects is None else projects,
                "permissions": ["read", "write", "admin"],
            }
        ],
    }


class TenantAccessTests:
    """Grouping only; pytest collects the module-level test functions below."""


def test_provision_refuses_a_cluster_outside_the_key_scope() -> None:
    body = ProvisionTenantAccessRequest(
        organization_external_id="00000000-0000-0000-0000-000000000000",
        cluster_id=FOREIGN,
        project_name="default",
        user_emails=["victim@example.com"],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(provision_tenant_access(body=body, api_key=_key(), db=None))

    assert exc.value.status_code == 403
    assert FOREIGN in str(exc.value.detail)


def test_provision_refuses_a_project_outside_the_key_scope() -> None:
    body = ProvisionTenantAccessRequest(
        organization_external_id="00000000-0000-0000-0000-000000000000",
        cluster_id=GRANTED,
        project_name="someone-elses-project",
        user_emails=["victim@example.com"],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(provision_tenant_access(body=body, api_key=_key(), db=None))

    assert exc.value.status_code == 403
    assert "someone-elses-project" in str(exc.value.detail)


def test_provision_reaches_its_body_when_the_scope_matches() -> None:
    """The guard must not be so blunt that the legitimate call is refused too.

    A db of None means the handler raises as soon as it touches the session. Anything other
    than a 403 proves the scope check let it through, which is all this asserts.
    """
    body = ProvisionTenantAccessRequest(
        organization_external_id="00000000-0000-0000-0000-000000000000",
        cluster_id=GRANTED,
        project_name="default",
        user_emails=["tenant@example.com"],
    )

    with pytest.raises(Exception) as exc:
        asyncio.run(provision_tenant_access(body=body, api_key=_key(), db=None))

    assert not (isinstance(exc.value, HTTPException) and exc.value.status_code == 403)


class _CapturingSession:
    """Records the URL raw_proxy builds instead of talking to anything."""

    def __init__(self) -> None:
        self.url: Optional[str] = None

    def _record(self, url: str, **_: Any) -> Any:
        self.url = url

        class _Resp:
            status_code = 200
            headers: Dict[str, str] = {}
            text = "{}"

            @staticmethod
            def json() -> Dict[str, Any]:
                return {}

        return _Resp()

    get = _record
    post = _record
    put = _record
    patch = _record
    delete = _record


def _run_raw_proxy(path: str, project: str, session: _CapturingSession) -> None:
    """Drive raw_proxy far enough to build the URL, with the cluster client stubbed out."""
    captured = session

    class _Api:
        _api_endpoint = "https://lxd.invalid/1.0"

    api = _Api()
    api.session = captured

    class _Client:
        pass

    client = _Client()
    client.api = api

    class _ConnMgr:
        @staticmethod
        def get_client(_cluster_id: str) -> Any:
            return client

    class _Svc:
        connection_manager = _ConnMgr()

    from app.services.lxd import health

    health.clear(GRANTED)
    asyncio.run(
        raw_proxy(
            request=RawProxyRequest(
                cluster_id=GRANTED, project=project, method="GET", path=path
            ),
            api_key=_key(),
            lxd_service=_Svc(),
        )
    )


def test_raw_proxy_drops_a_project_smuggled_in_the_path() -> None:
    session = _CapturingSession()
    _run_raw_proxy("/1.0/instances?project=victim&recursion=1", "default", session)

    assert session.url is not None, "raw_proxy did not reach the URL build step"
    assert "project=victim" not in session.url
    assert "recursion=1" in session.url, "unrelated query keys must still pass through"


def test_raw_proxy_keeps_the_authorised_project() -> None:
    session = _CapturingSession()
    _run_raw_proxy("/1.0/instances?project=victim", "default", session)

    assert session.url is not None
    # project="default" is the LXD default, so the override deliberately omits it entirely.
    assert "project=" not in session.url
