"""Shared fake pylxd graph for the snapshot and mobility routers.

Hand-rolled rather than MagicMock so an assertion about what reached the daemon is an assertion
about a real recorded call, not about a mock's memory of one. The whole point of these files is
to prove the EFFECT of a control, and a mock that answers anything makes that impossible.
"""

from typing import Any, Dict, List, Optional

HOST = "nuc2"
OTHER_HOST = "pandora"
PROJECT = "cust-qa-abcdef01"
INSTANCE = "ct-qa-fixture-0001"


class _Response:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeSnapshots:
    def __init__(self, owner: "FakeInstanceNode"):
        self._owner = owner

    def get(self, params=None):
        self._owner.recorded.append(("list_snapshots", dict(params or {})))
        return _Response({"metadata": list(self._owner.snapshots)})

    def post(self, json=None, params=None):
        self._owner.recorded.append(("create_snapshot", dict(json or {}), dict(params or {})))
        if self._owner.create_error:
            raise self._owner.create_error
        entry = dict(json or {})
        entry.setdefault("created_at", "2026-09-08T00:00:00Z")
        self._owner.snapshots.append(entry)
        if self._owner.omit_operation:
            return _Response({}, 202)
        return _Response({"operation": "/1.0/operations/op-create"}, 202)

    def __getitem__(self, name):
        return _SnapshotNode(self._owner, name)


class _SnapshotNode:
    def __init__(self, owner: "FakeInstanceNode", name: str):
        self._owner = owner
        self._name = name

    def delete(self, params=None):
        self._owner.recorded.append(("delete_snapshot", self._name, dict(params or {})))
        self._owner.snapshots = [s for s in self._owner.snapshots if s.get("name") != self._name]
        if self._owner.omit_operation:
            return _Response({}, 202)
        return _Response({"operation": "/1.0/operations/op-delete"}, 202)


class FakeInstanceNode:
    def __init__(self, config: Optional[Dict[str, str]] = None, snapshots=None):
        self.config = dict(config or {})
        self.snapshots: List[Dict[str, Any]] = list(snapshots or [])
        self.recorded: List[tuple] = []
        self.create_error: Optional[Exception] = None
        self.state_usage = 1_000_000_000
        # How the LXD operation behind an accepted request settles.
        self.devices: Dict[str, Any] = {}
        self.profiles: List[str] = ["default"]
        self.operation_status = "Success"
        self.operation_err = None
        # When set, every accepted (202) response names no operation. Without this the fakes
        # always hand back an id, so the branch that decides what "no operation" means was
        # unreachable from any test and an accepted-but-unconfirmable action looked like a
        # completed one.
        self.omit_operation = False

    @property
    def snapshots_api(self):
        return FakeSnapshots(self)

    @property
    def backups(self):
        return _Backups(self)

    def get(self, params=None):
        return _Response(
            {
                "metadata": {
                    "config": self.config,
                    "devices": self.devices,
                    "profiles": self.profiles,
                    "architecture": "x86_64",
                }
            }
        )

    def put(self, json=None, params=None):
        self.recorded.append(("put_instance", dict(json or {}), dict(params or {})))
        return _Response({"operation": "/1.0/operations/op-restore"}, 202)

    def post(self, json=None, params=None):
        self.recorded.append(("post_instance", dict(json or {}), dict(params or {})))
        return _Response(
            {
                "operation": "/1.0/operations/op-migrate",
                "metadata": {"metadata": {"control": "c-secret", "fs": "f-secret"}},
            },
            202,
        )

    @property
    def state(self):
        return _State(self)


# `snapshots` is both an attribute name on the node and the sub-resource pylxd exposes, so it is
# bound after the class body to keep the data attribute and the API node from shadowing.
FakeInstanceNode.snapshots_node = property(lambda self: FakeSnapshots(self))


class _State:
    def __init__(self, owner):
        self._owner = owner

    def get(self, params=None):
        return _Response({"metadata": {"disk": {"root": {"usage": self._owner.state_usage}}}})


class _Backups:
    def __init__(self, owner):
        self._owner = owner

    def post(self, json=None, params=None):
        self._owner.recorded.append(("create_backup", dict(json or {})))
        return _Response({"operation": "/1.0/operations/op-backup"}, 202)

    def __getitem__(self, name):
        return _BackupNode(self._owner, name)


class _BackupNode:
    def __init__(self, owner, name):
        self._owner = owner
        self._name = name

    def delete(self, params=None):
        self._owner.recorded.append(("delete_backup", self._name))
        return _Response({}, 200)


class _InstanceProxy:
    """Wraps the node so `.snapshots` resolves to the API sub-resource, as pylxd does."""

    def __init__(self, node: FakeInstanceNode):
        self._node = node

    @property
    def snapshots(self):
        return FakeSnapshots(self._node)

    def __getattr__(self, item):
        return getattr(self._node, item)


class _Instances:
    def __init__(self, node: FakeInstanceNode):
        self._node = node
        self.created: List[tuple] = []
        # The recursion=2 project listing the fleet route reads. Left as None so the default is
        # derived from the single node, and set explicitly by a test that needs several sources.
        self.project_listing: Optional[List[Dict[str, Any]]] = None

    def __getitem__(self, name):
        return _InstanceProxy(self._node)

    def get(self, params=None):
        """`recursion=2` folds each instance's snapshots into the instance listing."""
        self._node.recorded.append(("list_project", dict(params or {})))
        if self.project_listing is not None:
            return _Response({"metadata": self.project_listing})
        return _Response(
            {
                "metadata": [
                    {
                        "name": INSTANCE,
                        "status": "Running",
                        "config": self._node.config,
                        "snapshots": list(self._node.snapshots),
                    }
                ]
            }
        )

    def post(self, json=None, params=None):
        self.created.append((dict(json or {}), dict(params or {})))
        return _Response({"operation": "/1.0/operations/op-copy"}, 202)


class _Operations:
    """LXD's operation wait endpoint, so a test can make an accepted operation fail."""

    def __init__(self, node: FakeInstanceNode):
        self._node = node

    def __getitem__(self, operation_id):
        return _OperationNode(self._node)


class _OperationNode:
    def __init__(self, node):
        self._node = node

    @property
    def wait(self):
        return self

    def get(self, params=None, timeout=None):
        # Recorded so a test can assert the socket window outlasts the wait asked of LXD. Without
        # that, the read dies first and every slow operation reports "still running".
        self._node.recorded.append(("wait_operation", dict(params or {}), timeout))
        return _Response(
            {"metadata": {"status": self._node.operation_status, "err": self._node.operation_err}}
        )


class _Api:
    def __init__(self, node: FakeInstanceNode):
        self.instances = _Instances(node)
        self.operations = _Operations(node)
        self._api_endpoint = "https://fake:8443/1.0"

    @property
    def projects(self):
        return _Response({"metadata": [f"/1.0/projects/{PROJECT}"]})

    @property
    def storage_pools(self):
        return _Response({"metadata": ["/1.0/storage-pools/default"]})


class FakeClient:
    def __init__(self, node: Optional[FakeInstanceNode] = None):
        self.node = node or FakeInstanceNode()
        self.api = _Api(self.node)
        self.host_info = {
            "environment": {
                "certificate": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----"
            }
        }


class ConnectionManager:
    def __init__(self, client, hosts=(HOST,)):
        self._client = client
        self._hosts = list(hosts)

    def get_client(self, host, *args, **kwargs):
        return self._client if host in self._hosts else None

    def get_node_names(self):
        return list(self._hosts)


def service(client, hosts=(HOST,)):
    from app.services.lxd_service import LXDService

    svc = LXDService.__new__(LXDService)
    svc.connection_manager = ConnectionManager(client, hosts)
    svc.cert_manager = None
    return svc
