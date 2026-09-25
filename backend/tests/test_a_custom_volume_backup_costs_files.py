"""
Every request under a storage volume's /backups costs `instance.files`, like an instance's /backups.

A volume backup is the volume's data in a tarball. The instance /backups path has cost
`instance.files` for every method since the guest-access table existed, while
storage-pools/<pool>/volumes/<type>/<volume>/backups cost nothing: a `*` key without files could
create a backup and export it, and a key granted only `read` on one project could export a backup
that already existed, because reads under /storage-pools are never refused. So create, list, read,
rename, export and delete are all priced, for every volume type, on the path alone and before the
body is read.

The integrator never calls a /backups path (its client source was read), and its volume reads and
writes stay unpriced. Every raw-proxy test drives the real handler; the session records the request
it would send.
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import HTTPException

from app.core.guest_data_export import guest_data_capability_for, guest_data_path_capability_for
from app.routers import integrations
from tests._raw_proxy_fakes import (  # noqa: F401 - fixtures are registered by import
    ENDPOINT,
    ORCAHUB_READS,
    ORCAHUB_WRITES,
    _clean_breaker,
    call_raw_proxy,
    fence_calls,
    key,
)

VOL = "/1.0/storage-pools/default/volumes/custom/vol1"

PRICED: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
    ("POST", f"{VOL}/backups", {"name": "b0", "volume_only": True}),
    ("GET", f"{VOL}/backups", None),
    ("GET", f"{VOL}/backups?recursion=1", None),
    ("GET", f"{VOL}/backups/b0", None),
    ("GET", f"{VOL}/backups/b0/export", None),
    ("POST", f"{VOL}/backups/b0", {"name": "b1"}),
    ("DELETE", f"{VOL}/backups/b0", None),
    ("GET", "/1.0/storage-pools/default/volumes/virtual-machine/vm-1/backups", None),
    ("GET", "/1.0/storage-pools/default/volumes/container/web-1/backups/b0/export", None),
    ("POST", "/1.0/storage-pools/ceph/volumes/image/0f3c9a/backups", {"name": "b0"}),
    # Spellings the canonical parser reads as the same request.
    ("GET", f"{VOL}/BACKUPS/b0/Export", None),
    ("GET", f"{VOL}/%62ackups/b0/export", None),
    ("GET", f"{VOL}/backups/", None),
    ("GET", "/1.0/STORAGE-POOLS/default/VOLUMES/custom/vol1/backups", None),
    ("GET", f"{VOL}/backups/b0/export?project=tenant-a", None),
]
PRICED_IDS = [
    "create", "list", "list-recursive", "read", "export", "rename", "delete", "vm-volume",
    "container-volume-export", "image-volume", "upper", "encoded", "trailing-slash", "upper-prefix",
    "export-with-project",
]

NOT_PRICED: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
    ("GET", "/1.0/storage-pools/default/volumes/custom/backups", None),
    ("POST", "/1.0/storage-pools/default/volumes/custom", {"name": "backups"}),
    ("GET", f"{VOL}/snapshots/backups", None),
    ("GET", VOL, None),
    ("GET", "/1.0/storage-pools/default/volumes/custom?recursion=1", None),
    ("POST", f"{VOL}/snapshots", {"name": "backups"}),
    ("GET", "/1.0/storage-pools/default/buckets/backups", None),
    ("GET", "/1.0/storage-pools/backups", None),
]

STAR_NO_FILES = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.exec"])
NARROW_READ = key(["tenant-a"], capabilities=["inventory.read", "lxd.proxy"])
WITH_FILES = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.files"])


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """What raw_proxy writes to the audit trail, as the real _audit_key_action receives it."""
    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(integrations, "_audit_key_action", lambda api_key, **fields: recorded.append(fields))
    return recorded


@pytest.mark.parametrize("method,path,body", PRICED, ids=PRICED_IDS)
def test_a_volume_backup_request_costs_files_on_its_path(method: str, path: str, body: Any) -> None:
    assert guest_data_path_capability_for(path) == "instance.files"
    assert guest_data_capability_for(path, method, body) == "instance.files"


@pytest.mark.parametrize("method,path,body", NOT_PRICED)
def test_a_volume_or_snapshot_named_backups_is_not_priced(method: str, path: str, body: Any) -> None:
    assert guest_data_path_capability_for(path) is None
    assert guest_data_capability_for(path, method, body) is None


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("method,path,body", [case for case in PRICED if "tenant" not in case[1]],
                         ids=[i for case, i in zip(PRICED, PRICED_IDS) if "tenant" not in case[1]])
async def test_a_star_key_without_files_is_refused_and_recorded(
    method: str, path: str, body: Any, audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(STAR_NO_FILES, method, path, body=body)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403, outcome
    assert "instance.files" in outcome.detail
    assert session.calls == []
    assert [(record["result"], record["details"]["refused"]) for record in audits] == [
        ("failure", "capability_not_granted")
    ]


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("path", [f"{VOL}/backups/b0/export", f"{VOL}/backups?recursion=1", f"{VOL}/backups/b0"])
async def test_a_narrow_read_key_can_no_longer_export_an_existing_backup(
    path: str, audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(NARROW_READ, "GET", path, project="tenant-a")

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403, outcome
    assert "instance.files" in outcome.detail
    assert session.calls == []


@pytest.mark.usefixtures("fence_calls")
async def test_an_odd_cased_create_is_refused_for_its_path(audits: List[Dict[str, Any]]) -> None:
    outcome, session = await call_raw_proxy(STAR_NO_FILES, "POST", f"{VOL}/backups", body={"Name": "b0"})

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert session.calls == []
    assert [record["details"]["refused"] for record in audits] == ["capability_not_granted"]


async def test_a_legacy_key_never_inherits_it(audits: List[Dict[str, Any]]) -> None:
    outcome, session = await call_raw_proxy(key(["*"]), "GET", f"{VOL}/backups/b0/export")

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert session.calls == []


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("method,path,body", [PRICED[0], PRICED[4], PRICED[6]], ids=["create", "export", "delete"])
async def test_a_key_granted_files_sends_it_as_asked(
    method: str, path: str, body: Any, audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(WITH_FILES, method, path, body=body)

    assert outcome.status_code == 200, outcome
    assert [verb for verb, *_ in session.calls] == [method]
    assert session.calls[0][1].startswith(ENDPOINT)
    assert session.calls[0][2].get("json") == body


def test_the_integrators_volume_paths_stay_unpriced() -> None:
    volume_paths = [path for path in ORCAHUB_READS if "/storage-pools" in path]
    volume_paths += [path for _method, path in ORCAHUB_WRITES if "/storage-pools" in path]
    assert volume_paths
    assert [path for path in volume_paths if guest_data_path_capability_for(path)] == []
