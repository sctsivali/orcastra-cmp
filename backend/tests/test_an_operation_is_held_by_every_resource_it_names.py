"""An operation is inside the project bound only when every resource it names is.

The operations bound read only `resources.instances`, so an operation on another
organization's project-scoped volume, image or network, or a delete of its project, counted as
cluster-level. Its detail, wait and cancel by id then passed the bound on a shared cluster, and
once the list reads every project it would have shown there too.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.core.instance_access import operation_projects
from app.routers import lxd_operations
from tests.instance_access_fakes import CLUSTER, partner

FOREIGN = "other-db"
FOREIGN_RESOURCES = {
    "volume": {"storage_volumes": [f"/1.0/storage-pools/p/volumes/custom/v?project={FOREIGN}"]},
    "image": {"images": [f"/1.0/images/abc?project={FOREIGN}"]},
    "network": {"networks": [f"/1.0/networks/n1?project={FOREIGN}"]},
    "project": {"projects": [f"/1.0/projects/{FOREIGN}"]},
    "mixed": {"instances": ["/1.0/instances/web01"],
              "storage_volumes": [f"/1.0/storage-pools/p/volumes/custom/v?project={FOREIGN}"]},
}
OWN_RESOURCES = {
    "pool": {"storage_pools": ["/1.0/storage-pools/p"]},
    "default image": {"images": ["/1.0/images/abc"]},
    "own volume": {"storage_volumes": ["/1.0/storage-pools/p/volumes/custom/v?project=own-web"]},
    "none": {},
}


@pytest.mark.parametrize("kind", sorted(FOREIGN_RESOURCES))
def test_a_foreign_resource_of_any_kind_is_outside_the_bound(kind: str) -> None:
    assert FOREIGN in operation_projects({"resources": FOREIGN_RESOURCES[kind]})
    with partner({"default", "own-web"}):
        assert not lxd_operations._operation_is_visible(CLUSTER, FOREIGN_RESOURCES[kind])


@pytest.mark.parametrize("kind", sorted(OWN_RESOURCES))
def test_own_and_cluster_level_resources_stay_visible(kind: str) -> None:
    with partner({"default", "own-web"}):
        assert lxd_operations._operation_is_visible(CLUSTER, OWN_RESOURCES[kind])


def test_malformed_resources_name_no_project() -> None:
    assert operation_projects({"resources": {"images": "not-a-list"}}) == set()
    assert operation_projects({"resources": None}) == set()


class _Operation:
    def __init__(self, op_id: str, resources: dict):
        self.id = op_id
        self.resources = resources
        self.metadata = {}


def _service(ops: dict[str, _Operation], running: list[dict]) -> Any:
    def get(op_id: str) -> _Operation:
        return ops[op_id]

    listing = SimpleNamespace(
        get=lambda params=None, timeout=None: SimpleNamespace(
            status_code=200, json=lambda: {"metadata": {"running": running}}
        )
    )
    client = SimpleNamespace(operations=SimpleNamespace(get=get), api=SimpleNamespace(operations=listing))
    return SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda host: client, get_node_names=lambda: [CLUSTER]),
        get_node_names=lambda: [CLUSTER],
    )


async def test_a_foreign_volume_operation_reads_as_missing() -> None:
    ops = {"op-vol": _Operation("op-vol", FOREIGN_RESOURCES["volume"])}
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await lxd_operations.get_operation(
                operation_id="op-vol", host=CLUSTER, lxd_service=_service(ops, [])
            )
    assert exc.value.status_code == 404


async def test_the_list_hides_a_foreign_image_operation() -> None:
    running = [
        {"id": "op-img", "class": "task", "metadata": {}, "resources": FOREIGN_RESOURCES["image"]},
        {"id": "op-own", "class": "task", "metadata": {}, "resources": OWN_RESOURCES["default image"]},
    ]
    with partner({"default"}):
        response = await lxd_operations.list_operations(host=CLUSTER, lxd_service=_service({}, running))
    assert [op.id for op in response.operations] == ["op-own"]
    assert "_resources" not in response.operations[0].model_dump()
