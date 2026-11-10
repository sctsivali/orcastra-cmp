"""
Making an image of a guest public costs `instance.files`.

LXD serves a public image to any client that reaches it, trusted or not, so publishing an instance or
a snapshot with `public: true`, or setting it on an image later, hands out the guest's filesystem as
an image export does. A copy is priced whatever its source: LXD's remote and URL copy (images.go
imgPostRemoteInfo, imgPostURLInfo) call UpdateImage with the request's `public` when the project
already holds the fingerprint. A private publish stays unpriced: the integrator's marketplace publish
(`public: false`, a key preset without `instance.files`) and its pre-cache pull are pinned as sent.
LXD answers 400 for a `public` that is not a JSON bool, so any value but absent, null or false is
priced rather than refused here. Every raw-proxy test drives the real handler; the session records
the request it would send.
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import HTTPException

from app.core.api_key_capabilities import APP_TYPE_CAPABILITIES
from app.core.guest_data_export import guest_data_capability_for
from app.routers import integrations
from tests._raw_proxy_fakes import ENDPOINT, _clean_breaker, call_raw_proxy, fence_calls, key  # noqa: F401

FP = "0f3c9a1b2c3d4e5f"
SNAPSHOT = {"type": "snapshot", "name": "web-1/mktimg-a1b2"}

# The integrator's publish, as its client builds it.
ORCAHUB_PUBLISH = {
    "source": {"type": "snapshot", "name": "web-1/mktimg-a1b2"},
    "aliases": [{"name": "mkt/org-7/app", "description": "App"}],
    "public": False,
    "compression_algorithm": "gzip",
}
# The integrator's pre-cache pull, as the same file builds it. It names a trusted catalogue; any
# other server needs KEY_IMAGE_SERVERS_ALLOW (core/key_pull_sources.py).
ORCAHUB_PULL = {
    "source": {"type": "image", "mode": "pull", "protocol": "simplestreams",
               "server": "https://images.lxd.canonical.com", "alias": "ubuntu/24.04", "image_type": "container"},
    "aliases": [{"name": "ubuntu/24.04"}], "public": False, "auto_update": True,
}

PRICED: List[Tuple[str, str, Dict[str, Any]]] = [
    ("POST", "/1.0/images", {**ORCAHUB_PUBLISH, "public": True}),
    ("POST", "/1.0/images", {"source": {"type": "instance", "name": "web-1"}, "public": True}),
    ("POST", "/1.0/images", {"source": {"type": "container", "name": "web-1"}, "public": True}),
    ("POST", "/1.0/images", {"source": {"type": "virtual-machine", "name": "vm-1"}, "public": True}),
    ("POST", "/1.0/images?project=tenant-a", {"source": SNAPSHOT, "public": True}),
    ("POST", "/1.0/%69mages/", {"source": SNAPSHOT, "public": True}),
    # LXD refuses a type spelled otherwise, and it is priced rather than guessed at.
    ("POST", "/1.0/images", {"source": {"type": "Snapshot", "name": "web-1/s0"}, "public": True}),
    ("PUT", f"/1.0/images/{FP}", {"public": True, "auto_update": False, "properties": {}}),
    ("PATCH", f"/1.0/images/{FP}", {"public": True}),
    ("PATCH", f"/1.0/IMAGES/{FP}/", {"public": True}),
    ("PATCH", f"/1.0/images/{FP}?project=tenant-a", {"public": True}),
    # A copy is priced whatever its source, since a copy of a fingerprint the project holds sets public
    # on that image (imgPostRemoteInfo, imgPostURLInfo).
    ("POST", "/1.0/images", {"source": {"type": "image", "fingerprint": FP}, "public": True}),
    ("POST", "/1.0/images", {"source": {"type": "image", "fingerprint": FP, "server": "https://cluster-a.example.test:8443",
                                        "protocol": "lxd"}, "public": True}),
    ("POST", "/1.0/images", {**ORCAHUB_PULL, "public": True}),
    ("POST", "/1.0/images", {"source": {"type": "url", "url": "https://example.test/i"}, "public": True}),
    ("POST", "/1.0/images", {"source": {"type": "image", "mode": "push", "fingerprint": FP}, "public": True}),
    ("POST", "/1.0/images", {"source": {"type": "none"}, "public": True}),
    ("POST", "/1.0/images", {"public": True}),
    # A value LXD would refuse with 400 is priced, never read as private.
    ("POST", "/1.0/images", {"source": SNAPSHOT, "public": "true"}),
    ("POST", "/1.0/images", {"source": SNAPSHOT, "public": 1}),
    ("PATCH", f"/1.0/images/{FP}", {"public": "yes"}),
    ("PUT", f"/1.0/images/{FP}", {"public": {}}),
]
PRICED_IDS = [
    "publish-snapshot", "publish-instance", "publish-container", "publish-vm", "publish-query",
    "publish-encoded-path", "publish-odd-type", "put", "patch", "patch-upper-path", "patch-query",
    "copy-local-fingerprint", "copy-from-a-server", "pull-public", "url-public", "push-public", "source-none",
    "no-source", "publish-string", "publish-number", "patch-string", "put-object",
]

NOT_PRICED: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
    ("POST", "/1.0/images", ORCAHUB_PUBLISH),
    ("POST", "/1.0/images", {"source": SNAPSHOT}),
    ("POST", "/1.0/images", {"source": SNAPSHOT, "public": None}),
    ("POST", "/1.0/images", {"source": {"type": "instance", "name": "web-1"}, "public": False}),
    ("POST", "/1.0/images", ORCAHUB_PULL),
    ("POST", "/1.0/images", {"source": {"type": "image", "fingerprint": FP}, "public": False}),
    ("POST", "/1.0/images", {"source": {"type": "url", "url": "https://example.test/i"}}),
    ("POST", "/1.0/images", {"source": {"type": "image", "mode": "push", "fingerprint": FP}, "public": None}),
    ("PUT", f"/1.0/images/{FP}", {"public": False, "auto_update": True, "properties": {}}),
    ("PATCH", f"/1.0/images/{FP}", {"auto_update": True}),
    ("PATCH", f"/1.0/images/{FP}", {"public": False}),
    ("GET", f"/1.0/images/{FP}", None),
    ("POST", "/1.0/images/aliases", {"name": "mkt/org-7/app", "target": FP}),
    ("PATCH", "/1.0/images/aliases/ubuntu-24.04", {"public": True}),
    ("DELETE", f"/1.0/images/{FP}", None),
]
NOT_PRICED_IDS = [
    "orcahub-publish", "public-absent", "public-null", "public-false", "orcahub-pull", "copy-private",
    "url-public-absent", "push-public-null", "put-private", "patch-auto-update", "patch-private", "read",
    "alias-create", "alias-patch", "delete",
]

ORCAHUB = key(["*"], capabilities=sorted(APP_TYPE_CAPABILITIES["orcahub"]))
STAR_NO_FILES = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.exec"])
NARROW_NO_FILES = key(["tenant-a"], capabilities=["inventory.read", "lxd.proxy"])
WITH_FILES = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.files"])


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """What raw_proxy writes to the audit trail, as the real _audit_key_action receives it."""
    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(integrations, "_audit_key_action", lambda api_key, **fields: recorded.append(fields))
    return recorded


def _project(api_key: Dict[str, Any]) -> Optional[str]:
    projects = api_key["cluster_permissions"][0]["projects"]
    return None if "*" in projects else projects[0]


@pytest.mark.parametrize("method,path,body", PRICED, ids=PRICED_IDS)
def test_making_a_guest_image_public_costs_files(method: str, path: str, body: Dict[str, Any]) -> None:
    assert guest_data_capability_for(path, method, body) == "instance.files"


@pytest.mark.parametrize("method,path,body", NOT_PRICED, ids=NOT_PRICED_IDS)
def test_a_private_publish_and_other_image_writes_are_not_priced(method: str, path: str, body: Any) -> None:
    assert guest_data_capability_for(path, method, body) is None


def test_the_export_and_secret_rules_are_unchanged() -> None:
    assert guest_data_capability_for(f"/1.0/images/{FP}/export", "GET", None) == "instance.files"
    assert guest_data_capability_for(f"/1.0/images/{FP}/secret", "POST", {}) == "instance.files"
    assert guest_data_capability_for(f"/1.0/images/{FP}/secret", "POST", {"public": False}) == "instance.files"
    assert guest_data_capability_for("/1.0/instances/web-1", "POST", {"migration": True}) == "instance.files"


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("api_key", [STAR_NO_FILES, NARROW_NO_FILES, ORCAHUB], ids=["star", "narrow", "orcahub"])
@pytest.mark.parametrize("method,path,body", [case for case in PRICED if "tenant" not in case[1]])
async def test_the_raw_proxy_refuses_it_without_files(
    method: str, path: str, body: Dict[str, Any], api_key: Dict[str, Any], audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(api_key, method, path, project=_project(api_key), body=body)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403, outcome
    assert "instance.files" in outcome.detail
    assert session.calls == []
    assert [(record["result"], record["details"]["refused"]) for record in audits] == [
        ("failure", "capability_not_granted")
    ]


GRANTED = [
    (case, case_id)
    for case, case_id in zip(PRICED[:5] + PRICED[7:9] + PRICED[11:18], PRICED_IDS[:5] + PRICED_IDS[7:9] + PRICED_IDS[11:18])
    # A URL source is never sent for a key (test_a_key_cannot_make_the_cluster_fetch_from_anywhere.py).
    if case_id != "url-public"
]


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("method,path,body", [case for case, _ in GRANTED], ids=[case_id for _, case_id in GRANTED])
async def test_a_key_granted_files_sends_it_as_asked(
    method: str, path: str, body: Dict[str, Any], audits: List[Dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import key_pull_sources

    # cluster-a is the cluster a copy names as its source, registered in this dashboard.
    monkeypatch.setattr(key_pull_sources, "_is_registered_cluster", lambda server: "cluster-a.example.test" in server)
    outcome, session = await call_raw_proxy(WITH_FILES, method, path, body=body)

    assert outcome.status_code == 200, outcome
    assert [(verb, kwargs.get("json")) for verb, _url, kwargs in session.calls] == [(method, body)]
    assert session.calls[0][1].startswith(ENDPOINT)


async def test_a_legacy_key_never_inherits_it(audits: List[Dict[str, Any]]) -> None:
    outcome, session = await call_raw_proxy(key(["*"]), "PATCH", f"/1.0/images/{FP}", body={"public": True})

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert session.calls == []


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("body", [ORCAHUB_PUBLISH, ORCAHUB_PULL], ids=["publish", "pull"])
async def test_the_integrators_image_writes_are_still_sent(body: Dict[str, Any], audits: List[Dict[str, Any]]) -> None:
    outcome, session = await call_raw_proxy(ORCAHUB, "POST", "/1.0/images", body=body)

    assert outcome.status_code == 200, outcome
    assert [(verb, kwargs.get("json")) for verb, _url, kwargs in session.calls] == [("POST", body)]
    assert [record["result"] for record in audits] == ["success"]


@pytest.mark.usefixtures("fence_calls")
async def test_a_private_publish_cannot_be_made_public_by_a_copy(audits: List[Dict[str, Any]]) -> None:
    """The integrator's key publishes privately, which is sent. Copying the fingerprint it now holds with
    public true would make that guest image public through LXD's copy path, so it costs files."""
    publish, session = await call_raw_proxy(ORCAHUB, "POST", "/1.0/images", body=ORCAHUB_PUBLISH)
    assert publish.status_code == 200 and [verb for verb, *_ in session.calls] == ["POST"]

    copy = {"source": {"type": "image", "fingerprint": FP}, "aliases": [{"name": "mkt/org-7/app-public"}],
            "public": True}
    outcome, session = await call_raw_proxy(ORCAHUB, "POST", "/1.0/images", body=copy)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and "instance.files" in outcome.detail
    assert session.calls == []
    assert [(record["result"], record["details"].get("refused")) for record in audits] == [
        ("success", None), ("failure", "capability_not_granted")
    ]


@pytest.mark.usefixtures("fence_calls")
async def test_a_publish_with_public_spelled_otherwise_is_refused_not_missed(audits: List[Dict[str, Any]]) -> None:
    outcome, session = await call_raw_proxy(STAR_NO_FILES, "POST", "/1.0/images",
                                            body={"source": SNAPSHOT, "Public": True})

    assert isinstance(outcome, HTTPException) and outcome.status_code == 400 and "'Public'" in outcome.detail
    assert session.calls == []
    assert [record["details"]["refused"] for record in audits] == ["field_name_not_canonical"]
