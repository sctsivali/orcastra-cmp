"""A caller's name that is empty, `.` or `..` is refused with 400 before any cluster call.

requests folds a dot segment out of a URL path, so `..` as an instance name would address the
instance collection, and for a delete the server root. /proxy puts the instance name, an operation
id and a network, storage pool or project name into the path pylxd requests
(services/lxd/proxy_path_params.py), and a project-limited key's write plan reads the copy source,
snapshot, profiles and image its body names (services/lxd/limited_write_reach.py). Each goes
through core/lxd_segment.lxd_segment.

The service records a client being built as a cluster call, since the real one revalidates the pooled
connection with a request, and the session records every request pylxd's own path builder sends.
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest
import requests
from fastapi import HTTPException

from app.routers.integrations import (
    ProxyRequest,
    RawProxyRequest,
    get_integration_operation,
    proxy_operation,
    raw_proxy,
)
from tests._limited_write_fakes import _no_webhook_events  # noqa: F401
from tests._raw_proxy_fakes import CLUSTER_ID, _clean_breaker, fence_calls, key  # noqa: F401
from tests.test_a_caller_name_stays_one_lxd_path_segment import PylxdClient, WireSession, lxd_reads

DOTS = ["..", "."]
WILDCARD = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.exec", "instance.files",
                                    "desktop.view", "desktop.input", "desktop.clipboard"])
LIMITED = key(["tenant-a"], capabilities=["inventory.read", "lxd.proxy"])


class Session(WireSession):
    def put(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("PUT", url, **kwargs)


class RecordingService:
    """An LXD service whose client is pylxd's path builder over a recording session."""

    def __init__(self) -> None:
        self.built: List[Tuple[str, ...]] = []
        self.session = Session()
        client = PylxdClient(self.session, project="tenant-a")

        def get_client(cluster_id: str, **_: Any) -> Any:
            self.built.append(("get_client", cluster_id))
            return client

        def get_project_client(cluster_id: str, project: str, **_: Any) -> Any:
            self.built.append(("get_project_client", cluster_id, project))
            return client

        self.connection_manager = SimpleNamespace(get_client=get_client, get_project_client=get_project_client)

    @property
    def untouched(self) -> bool:
        return self.built == [] and self.session.calls == []


async def _proxy(operation: str, params: Dict[str, Any], api_key: Dict[str, Any] = WILDCARD,
                 project: str = "tenant-a", wait: bool = True) -> Tuple[Any, RecordingService]:
    service = RecordingService()
    try:
        answer = await proxy_operation(
            request=ProxyRequest(cluster_id=CLUSTER_ID, operation=operation, project=project, params=params,
                                 wait=wait),
            api_key=api_key, lxd_service=service, _=None,
        )
    except HTTPException as refusal:
        return refusal, service
    return answer, service


async def _raw(body: Dict[str, Any], api_key: Dict[str, Any] = LIMITED) -> Tuple[Any, RecordingService]:
    service = RecordingService()
    try:
        answer = await raw_proxy(
            request=RawProxyRequest(cluster_id=CLUSTER_ID, method="POST", path="/1.0/instances", project="tenant-a",
                                    body=body),
            api_key=api_key, lxd_service=service, _=None,
        )
    except HTTPException as refusal:
        return refusal, service
    return answer, service


def _refused_untouched(outcome: Any, service: RecordingService, what: str) -> None:
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400, outcome
    assert what in str(outcome.detail), outcome.detail
    assert service.untouched, (service.built, service.session.calls)


INSTANCE_OPERATIONS: List[Tuple[str, Dict[str, Any]]] = [
    ("delete_instance", {}),
    ("start_instance", {}),
    ("stop_instance", {}),
    ("restart_instance", {}),
    ("update_instance", {"config": {"limits.cpu": "2"}}),
    ("get_instance", {}),
    ("list_snapshots", {}),
    ("create_snapshot", {"snapshot_name": "s0"}),
    ("restore_snapshot", {"snapshot_name": "s0"}),
    ("delete_snapshot", {"snapshot_name": "s0"}),
    ("exec_instance", {"command": ["true"]}),
    ("get_file", {"path": "/etc/hostname"}),
]


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("wait", [True, False], ids=["wait", "no-wait"])
@pytest.mark.parametrize("name", DOTS, ids=["dotdot", "dot"])
@pytest.mark.parametrize("operation,extra", INSTANCE_OPERATIONS, ids=[op for op, _ in INSTANCE_OPERATIONS])
async def test_a_dot_instance_name_is_refused_before_any_cluster_call(
    operation: str, extra: Dict[str, Any], name: str, wait: bool
) -> None:
    outcome, service = await _proxy(operation, {"name": name, **extra}, wait=wait)

    _refused_untouched(outcome, service, "Instance name")


@pytest.mark.usefixtures("fence_calls")
async def test_an_update_carrying_cloud_init_is_refused_before_its_read() -> None:
    no_exec = key(["*"], capabilities=["inventory.read", "lxd.proxy"])
    outcome, service = await _proxy("update_instance", {"name": "..", "config": {"user.user-data": "#cloud-config"}},
                                    api_key=no_exec)

    _refused_untouched(outcome, service, "Instance name")


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize(
    "operation,params,what",
    [
        ("delete_network", {"name": ".."}, "Network name"),
        ("create_network", {"name": ".."}, "Network name"),
        ("delete_storage_pool", {"name": ".."}, "Storage pool name"),
        ("create_storage_pool", {"definition": {"name": "..", "driver": "dir"}}, "Storage pool name"),
        ("create_storage_pool", {"name": ".", "driver": "dir"}, "Storage pool name"),
        ("delete_project", {"name": ".."}, "Project name"),
        ("create_project", {"name": "."}, "Project name"),
        ("get_operation", {"operation_id": ".."}, "Operation id"),
        ("delete_snapshot", {"name": "web", "snapshot_name": ".."}, "Snapshot name"),
    ],
    ids=["delete-network", "create-network", "delete-pool", "create-pool", "create-pool-flat", "delete-project",
         "create-project", "operation", "snapshot"],
)
async def test_a_dot_name_on_any_other_operation_is_refused_before_any_cluster_call(
    operation: str, params: Dict[str, Any], what: str
) -> None:
    outcome, service = await _proxy(operation, params, project="default")

    _refused_untouched(outcome, service, what)


async def test_the_operations_endpoint_refuses_a_dot_id_before_any_cluster_call() -> None:
    service = RecordingService()
    with pytest.raises(HTTPException) as refused:
        await get_integration_operation(operation_id="..", cluster_id=CLUSTER_ID, project="default",
                                        api_key=WILDCARD, lxd_service=service)

    assert refused.value.status_code == 400 and "Operation id" in refused.value.detail
    assert service.untouched


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize(
    "operation,verb,segments",
    [("delete_instance", "DELETE", ["instances", "web?project=tenant-z&"]),
     ("start_instance", "PUT", ["instances", "web?project=tenant-z&", "state"])],
    ids=["delete", "start"],
)
async def test_a_name_that_is_not_a_dot_segment_still_reaches_its_instance(
    operation: str, verb: str, segments: List[str]
) -> None:
    outcome, service = await _proxy(operation, {"name": "web?project=tenant-z&"}, wait=False)

    assert not isinstance(outcome, HTTPException) and outcome.success, outcome
    sent = [(url, params) for method, url, params in service.session.calls if method == verb]
    assert [lxd_reads(verb, url, params) for url, params in sent] == [(segments, "tenant-a")]


# -- a project-limited key's write plan ------------------------------------------------------------------

LIMITED_BODIES: List[Tuple[Dict[str, Any], str]] = [
    ({"name": "c1", "source": {"type": "copy", "source": ".."}}, "Instance name"),
    ({"name": "c1", "source": {"type": "copy", "source": "."}}, "Instance name"),
    ({"name": "c1", "source": {"type": "copy", "source": "web/.."}}, "Snapshot name"),
    ({"name": "c1", "source": {"type": "image", "alias": "ubuntu"}, "profiles": [".."]}, "Profile name"),
    ({"name": "c1", "source": {"type": "image", "alias": "."}}, "Image alias"),
    ({"name": "c1", "source": {"type": "image", "fingerprint": ".."}}, "Image fingerprint"),
]
LIMITED_IDS = ["copy-dotdot", "copy-dot", "copy-snapshot-dotdot", "profile", "image-alias", "image-fingerprint"]


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("body,what", LIMITED_BODIES, ids=LIMITED_IDS)
async def test_a_limited_raw_proxy_write_naming_a_dot_source_is_refused_before_any_cluster_call(
    body: Dict[str, Any], what: str
) -> None:
    outcome, service = await _raw(body)

    _refused_untouched(outcome, service, what)


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("body,what", LIMITED_BODIES, ids=LIMITED_IDS)
async def test_a_limited_proxy_create_naming_a_dot_source_is_refused_before_any_cluster_call(
    body: Dict[str, Any], what: str
) -> None:
    outcome, service = await _proxy("create_instance", body, api_key=LIMITED)

    _refused_untouched(outcome, service, what)


@pytest.mark.usefixtures("fence_calls")
async def test_a_limited_copy_of_a_real_source_is_still_read_as_one_segment() -> None:
    outcome, service = await _raw({"name": "c1", "source": {"type": "copy", "source": "web?project=tenant-z&"}})

    reads = [lxd_reads(verb, url, params) for verb, url, params in service.session.calls if verb == "GET"]
    assert (["instances", "web?project=tenant-z&"], "tenant-a") in reads, reads
    assert not isinstance(outcome, HTTPException) and outcome.status_code == 200, outcome
    assert [verb for verb, *_ in service.session.calls][-1] == "POST"
