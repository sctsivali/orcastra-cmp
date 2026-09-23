"""The operations list, detail, wait and cancel are held to the caller's project bound.

The list filtered by the tenant ACL alone, which is always None for a Partner, so on a cluster
bound to two organizations it showed the other organization's operations, instance names
included. The detail, wait and cancel routes took any operation id on the host, so a Partner
could cancel the other organization's live console session or instance create. An operation
outside the bound now reads as missing, and an operation that names no instance stays
cluster-level.
"""

from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import HTTPException

from app.routers import lxd_operations
from tests.instance_access_fakes import CLUSTER, partner

FOREIGN = "other-db"


def _resources(project: Optional[str]) -> dict:
    if project is None:
        return {"images": ["/1.0/images/abc"]}
    query = "" if project == "default" else f"?project={project}"
    return {"instances": [f"/1.0/instances/web01{query}"]}


def _op_data(op_id: str, project: Optional[str]) -> dict:
    return {"id": op_id, "class": "task", "status": "Running", "status_code": 103,
            "description": "Creating instance", "metadata": {}, "resources": _resources(project)}


class _Operation:
    def __init__(self, op_id: str, project: Optional[str]):
        self.id = op_id
        self.resources = _resources(project)
        self.metadata = {}
        self.waited = False

    def wait(self) -> None:
        """pylxd's own signature: Operation.wait takes no timeout."""
        self.waited = True


class _Client:
    def __init__(self, operations: dict[str, _Operation]):
        self.deleted: list[str] = []
        self.waited: list[tuple[str, dict]] = []
        self._operations = operations
        by_id = self._operations

        def get(op_id: str) -> _Operation:
            if op_id not in by_id:
                raise RuntimeError("not found")
            return by_id[op_id]

        def item(op_id: str) -> Any:
            def wait_get(params: Optional[dict] = None, timeout: Any = None) -> Any:
                self.waited.append((op_id, dict(params or {})))
                return SimpleNamespace(json=lambda: {"metadata": {"status": "Success", "status_code": 200}})

            return SimpleNamespace(
                delete=lambda: self.deleted.append(op_id), wait=SimpleNamespace(get=wait_get)
            )

        running = [_op_data(op.id, project) for op, project in (
            (by_id["op-own"], "default"), (by_id["op-foreign"], FOREIGN), (by_id["op-image"], None)
        )]
        listing = SimpleNamespace(
            get=lambda params=None, timeout=None: SimpleNamespace(
                status_code=200, json=lambda: {"metadata": {"running": running}}
            ),
        )
        self.operations = SimpleNamespace(get=get)
        self.api = SimpleNamespace(operations=_Indexable(listing, item))


class _Indexable:
    """`client.api.operations`: listable with .get and indexable by id."""

    def __init__(self, listing: Any, item: Any):
        self._listing = listing
        self._item = item

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._listing.get(*args, **kwargs)

    def __getitem__(self, op_id: str) -> Any:
        return self._item(op_id)


@pytest.fixture
def cluster():
    operations = {
        "op-own": _Operation("op-own", "default"),
        "op-foreign": _Operation("op-foreign", FOREIGN),
        "op-image": _Operation("op-image", None),
    }
    client = _Client(operations)
    service = SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda host: client, get_node_names=lambda: [CLUSTER]),
        get_node_names=lambda: [CLUSTER],
    )
    return SimpleNamespace(client=client, service=service, operations=operations)


async def test_the_list_hides_operations_outside_the_bound(cluster):
    with partner({"default"}):
        response = await lxd_operations.list_operations(host=CLUSTER, lxd_service=cluster.service)
    assert sorted(op.id for op in response.operations) == ["op-image", "op-own"]


@pytest.mark.parametrize("op_id", ["op-own", "op-image"])
async def test_an_operation_inside_the_bound_can_be_read_and_cancelled(op_id, cluster):
    with partner({"default"}):
        info = await lxd_operations.get_operation(operation_id=op_id, host=CLUSTER, lxd_service=cluster.service)
        await lxd_operations.cancel_operation(
            operation_id=op_id, host=CLUSTER, lxd_service=cluster.service, _=None, _rbac=None
        )
    assert info.id == op_id
    assert cluster.client.deleted == [op_id]


async def test_a_foreign_operation_reads_as_missing(cluster):
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await lxd_operations.get_operation(
                operation_id="op-foreign", host=CLUSTER, lxd_service=cluster.service
            )
    assert exc.value.status_code == 404


async def test_a_foreign_operation_cannot_be_cancelled(cluster):
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await lxd_operations.cancel_operation(
                operation_id="op-foreign", host=CLUSTER, lxd_service=cluster.service, _=None, _rbac=None
            )
    assert exc.value.status_code == 404
    assert cluster.client.deleted == []


async def test_a_foreign_operation_cannot_be_waited_on(cluster):
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await lxd_operations.wait_operation(
                operation_id="op-foreign", host=CLUSTER, timeout=1, lxd_service=cluster.service
            )
    assert exc.value.status_code == 404
    assert not cluster.operations["op-foreign"].waited
    assert cluster.client.waited == []
