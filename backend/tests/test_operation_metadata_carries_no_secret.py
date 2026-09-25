"""The operations list and detail never return the secrets an LXD operation carries.

An exec or console operation is websocket class and its metadata holds `fds`, whose values open
the operation websocket with no further authentication. A token operation holds `secret`, and
a migration source holds its channel secrets under `control`, `fs` and `criu`. The /operations
routes copied that metadata verbatim to every caller, and the operation detail dialog printed
every key, so anyone who could see an operation could take over the session it belonged to.

note: dev closed the dashboard half with an allowlist of scalar progress fields, which is stricter
than a list of secret names: `command` and `interactive` are dropped too. The allowlist now lives
in services/lxd/operation_metadata.py beside the secret key set the raw proxy strips, and the
detail route is covered by test_lxd_operation_authorization.py.
"""

from types import SimpleNamespace
from typing import Any, Dict

import pytest

from app.routers import lxd_operations
from app.services.lxd.operation_metadata import (
    OPERATION_SECRET_KEYS,
    sanitize_operation_metadata,
    strip_operation_secrets,
)

CLUSTER = "nuc2"
EXEC = {"fds": {"0": "s3cr3t", "control": "c0ntr0l"}, "command": ["bash"], "interactive": True}


@pytest.mark.parametrize(
    "metadata,kept,dropped",
    [
        (EXEC, {"command", "interactive"}, {"fds"}),
        ({"secret": "t0k3n", "fingerprint": "abc", "addresses": []}, {"fingerprint", "addresses"}, {"secret"}),
        ({"control": "a", "fs": "b", "criu": "c"}, set(), {"control", "fs", "criu"}),
        ({"download_progress": "rootfs: 48%"}, {"download_progress"}, set()),
        ({"FDS": {"0": "s"}, "Secret": "t", "return": 0}, {"return"}, {"FDS", "Secret"}),
    ],
    ids=["exec", "token", "migration", "progress", "any-case"],
)
def test_the_secret_fields_are_dropped_and_the_rest_kept(
    metadata: Dict[str, Any], kept: set, dropped: set
) -> None:
    result = strip_operation_secrets(metadata)
    assert set(result) == kept
    assert not dropped & set(result)
    assert all(result[key] == metadata[key] for key in kept)


@pytest.mark.parametrize(
    "metadata",
    [
        EXEC,
        {"secret": "t0k3n", "fingerprint": "abc"},
        {"control": "a", "fs": "b", "criu": "c", "secrets": {"control": "d"}},
    ],
    ids=["exec", "token", "migration"],
)
def test_the_allowlist_keeps_no_secret_field(metadata: Dict[str, Any]) -> None:
    assert sanitize_operation_metadata(metadata) == {}


@pytest.mark.parametrize("value", [None, [], "fds"])
def test_anything_but_a_dict_becomes_empty(value: Any) -> None:
    assert strip_operation_secrets(value) == {}
    assert sanitize_operation_metadata(value) == {}


def test_the_key_set_names_every_known_secret() -> None:
    assert {"fds", "secret", "secrets", "control", "fs", "criu"} <= OPERATION_SECRET_KEYS


def _running(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": "op-1", "class": "websocket", "status": "Running", "status_code": 103,
        "description": "Executing command", "metadata": metadata,
        "resources": {"instances": ["/1.0/instances/web01"]},
    }


def test_the_list_read_strips_the_secrets() -> None:
    reads = []

    def get(params: Any = None, timeout: Any = None) -> Any:
        reads.append(params)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"metadata": {"running": [_running(dict(EXEC, download_progress="rootfs: 48%"))]}},
        )

    client = SimpleNamespace(api=SimpleNamespace(operations=SimpleNamespace(get=get)))
    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda host: client))

    operations = lxd_operations._fetch_operations_for_node(service, CLUSTER)

    assert reads == [{"recursion": "1"}]
    assert operations[0].metadata == {"download_progress": "rootfs: 48%"}
    assert operations[0].status == "Running"
    assert "s3cr3t" not in operations[0].model_dump_json()
    assert "c0ntr0l" not in operations[0].model_dump_json()
