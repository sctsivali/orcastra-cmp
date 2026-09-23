"""The operations list and detail never return the secrets an LXD operation carries.

An exec or console operation is websocket class and its metadata holds `fds`, whose values open
the operation websocket with no further authentication. A token operation holds `secret`, and
a migration source holds its channel secrets under `control`, `fs` and `criu`. The /operations
routes copied that metadata verbatim to every caller, and the operation detail dialog printed
every key, so anyone who could see an operation could take over the session it belonged to.
"""

from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.routers import lxd_operations
from app.services.lxd.utils import OPERATION_SECRET_KEYS, strip_operation_secrets
from tests.instance_access_fakes import CLUSTER, scoped

EXEC = {"fds": {"0": "s3cr3t", "control": "c0ntr0l"}, "command": ["bash"], "interactive": True}


@pytest.mark.parametrize(
    "metadata,kept,dropped",
    [
        (EXEC, {"command", "interactive"}, {"fds"}),
        ({"secret": "t0k3n", "fingerprint": "abc", "addresses": []}, {"fingerprint", "addresses"}, {"secret"}),
        ({"control": "a", "fs": "b", "criu": "c"}, set(), {"control", "fs", "criu"}),
        ({"download_progress": "rootfs: 48%"}, {"download_progress"}, set()),
    ],
    ids=["exec", "token", "migration", "progress"],
)
def test_the_secret_fields_are_dropped_and_the_rest_kept(metadata, kept, dropped):
    result = strip_operation_secrets(metadata)
    assert set(result) == kept
    assert not dropped & set(result)


@pytest.mark.parametrize("value", [None, [], "fds"])
def test_anything_but_a_dict_becomes_empty(value):
    assert strip_operation_secrets(value) == {}


def test_the_key_set_names_every_known_secret():
    assert {"fds", "secret", "control", "fs"} <= OPERATION_SECRET_KEYS


def _running(metadata: dict) -> dict:
    return {
        "id": "op-1", "class": "websocket", "status": "Running", "status_code": 103,
        "description": "Executing command", "metadata": metadata,
        "resources": {"instances": ["/1.0/instances/web01"]},
    }


def test_the_list_read_strips_the_secrets():
    client = SimpleNamespace(api=SimpleNamespace(operations=SimpleNamespace(
        get=lambda params=None, timeout=None: SimpleNamespace(
            status_code=200, json=lambda: {"metadata": {"running": [_running(dict(EXEC))]}}
        )
    )))
    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda host: client))
    operations = lxd_operations._fetch_operations_for_node(service, CLUSTER)
    assert operations[0].metadata == {"command": ["bash"], "interactive": True}
    assert "s3cr3t" not in operations[0].model_dump_json()


async def test_the_detail_read_strips_the_secrets():
    operation = SimpleNamespace(id="op-1", metadata=dict(EXEC), status="Running")

    def get(op_id: str) -> Any:
        return operation

    client = SimpleNamespace(operations=SimpleNamespace(get=get))
    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda host: client))
    with scoped():
        info = await lxd_operations.get_operation(operation_id="op-1", host=CLUSTER, lxd_service=service)
    assert "fds" not in info.metadata
    assert "s3cr3t" not in info.model_dump_json()


async def test_a_detail_with_no_metadata_is_an_empty_map():
    operation: Optional[Any] = SimpleNamespace(id="op-1", metadata=None)
    client = SimpleNamespace(operations=SimpleNamespace(get=lambda op_id: operation))
    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda host: client))
    with scoped():
        info = await lxd_operations.get_operation(operation_id="op-1", host=CLUSTER, lxd_service=service)
    assert info.metadata == {}
