"""A project-limited key's body is read the way LXD reads it: a field spelled otherwise is refused.

LXD decodes a request body with Go's encoding/json, which matches a struct field whatever its case,
and with Unicode case folding, so "Profiles", "CONFIG" or "\u017fource" are the fields profiles, config
and source to LXD. Every body rule for a limited key (the project named in the body, host-reaching
settings, the profiles a write applies) reads the plain lowercase spelling, so a body that spells a
field another way would carry past all of them. Map keys (config keys, device names) match exactly.
"""

import pytest
from fastapi import HTTPException

from app.routers import integrations
from app.routers.integrations import ProxyRequest, proxy_operation
from app.services.lxd import health
from tests.test_a_limited_write_is_judged_by_what_it_applies import _sent, _write
from tests.test_an_applied_profile_cannot_reach_the_host import CLUSTER_ID, TWO, _Client, _key, _ProjectSession, _Svc


@pytest.fixture(autouse=True)
def _reset_breaker():
    health.clear(CLUSTER_ID)
    integrations._cluster_slots.clear()
    yield
    health.clear(CLUSTER_ID)


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/1.0/instances", {"name": "c1", "Profiles": ["priv"]}),
        ("POST", "/1.0/instances", {"name": "c1", "profiles": ["default"], "CONFIG": {"security.privileged": "true"}}),
        ("POST", "/1.0/instances", {"name": "c1", "Devices": {"d": {"type": "unix-block", "source": "/dev/sda"}}}),
        ("POST", "/1.0/instances/web", {"Project": "tenant-c"}),
        ("PUT", "/1.0/instances/app", {"Restore": "s0"}),
        ("POST", "/1.0/instances", {"name": "c1", "\u017fource": {"type": "copy", "source": "web"}}),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web", "Project": "tenant-c"}}),
    ],
    ids=["Profiles", "CONFIG", "Devices", "Project", "Restore", "long-s", "nested-Project"],
)
async def test_a_limited_body_field_spelled_otherwise_is_refused(method, path, body) -> None:
    outcome, session = await _write(method, path, "tenant-a", body)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400 and session.calls == []


async def test_a_star_key_body_is_not_held_to_the_spelling() -> None:
    outcome, session = await _write("POST", "/1.0/instances", "tenant-a", {"name": "c1", "Profiles": ["default"]},
                                    key=_key(["*"]))
    assert outcome.status_code == 200 and _sent(session)


async def test_proxy_create_refuses_an_odd_field_spelling() -> None:
    client = _Client(_ProjectSession())
    with pytest.raises(HTTPException) as caught:
        await proxy_operation(
            request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_instance", project="tenant-b",
                                 params={"name": "vm", "Profiles": ["priv"]}),
            api_key=TWO, lxd_service=_Svc(client), _=None,
        )
    assert caught.value.status_code == 400 and client.created == []
