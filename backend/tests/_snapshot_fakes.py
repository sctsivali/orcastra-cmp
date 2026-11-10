"""Shared fake pylxd graph for the snapshot and mobility routers.

Hand-rolled rather than MagicMock so an assertion about what reached the daemon is about a
recorded call. A mock that answers anything cannot prove the effect of a control.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

HOST = "nuc2"
OTHER_HOST = "pandora"
PROJECT = "cust-qa-abcdef01"
INSTANCE = "ct-qa-fixture-0001"


class LXDAPIException(Exception):
    """What pylxd raises when the daemon refuses. Named to match so a handler's except reads true."""


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
    def __init__(self, owner: "FakeInstanceNode", name: Optional[str] = None):
        self._owner = owner
        self._name = name

    def get(self, params=None, timeout=None):
        """The per-instance snapshot listing, the one read that answers a size.

        Recorded with the socket timeout and the instance asked about, so a test can prove the
        fleet size phase read exactly the holders it may see. A per-instance list, error or delay
        keyed by (project, instance) lets one holder answer differently from the rest.
        """
        params = dict(params or {})
        holder = (params.get("project"), self._name)
        self._owner.recorded.append(("list_snapshots", params, timeout, self._name))
        delay = self._owner.size_read_delay.get(holder)
        if delay:
            time.sleep(delay)
        error = self._owner.size_read_error.get(holder)
        if error is not None:
            raise error
        if holder in self._owner.snapshots_by_instance:
            return _Response({"metadata": list(self._owner.snapshots_by_instance[holder])})
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
        # A distinct id per create keys operation_status, so the safety copy can fail while the
        # restore succeeds.
        self._owner.create_count += 1
        return _Response(
            {"operation": f"/1.0/operations/op-create-{self._owner.create_count}"}, 202
        )

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
        self.create_count = 0
        self.state_usage = 1_000_000_000
        self.devices: Dict[str, Any] = {}
        self.profiles: List[str] = ["default"]
        # How the LXD operation behind an accepted request settles.
        self.operation_status = "Success"
        self.operation_err = None
        # When set, every accepted (202) response names no operation, which reaches the branch
        # that decides what an accepted but unconfirmable action means.
        self.omit_operation = False
        # Projects the fallback path enumerates when a daemon cannot answer about all of them.
        self.projects: List[str] = [PROJECT]
        # What the per-instance listing answers for one (project, instance), when it should
        # differ from `snapshots`; and how that read misbehaves, when a test needs it to.
        self.snapshots_by_instance: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self.size_read_error: Dict[Tuple[str, str], Exception] = {}
        self.size_read_delay: Dict[Tuple[str, str], float] = {}
        # What `GET /1.0/operations?recursion=1` answers: bucket name -> operations. The
        # all-projects read answers `operation_buckets`; a per-project read answers the
        # project's own entry, so a test can prove the fallback walked every project.
        self.operation_buckets: Dict[str, List[Dict[str, Any]]] = {}
        self.operation_buckets_by_project: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        self.operations_project_errors: Dict[str, Exception] = {}
        # Raised by the all-projects operations read, so a daemon without the extension can be
        # played (an exception carrying `.response.status_code == 400`).
        self.operations_all_projects_error: Optional[Exception] = None
        # What `GET /1.0/warnings?recursion=1` answers.
        self.warnings: List[Dict[str, Any]] = []
        # The pool behind the root disk and its driver. None leaves the root disk out of
        # `expanded_devices`.
        self.root_pool: Optional[str] = None
        self.pool_driver: str = "dir"

    @property
    def snapshots_api(self):
        return FakeSnapshots(self)

    @property
    def backups(self):
        return _Backups(self)

    def get(self, params=None, timeout=None):
        expanded = dict(self.devices)
        if self.root_pool:
            expanded["root"] = {"type": "disk", "path": "/", "pool": self.root_pool}
        return _Response(
            {
                "metadata": {
                    "config": self.config,
                    "devices": self.devices,
                    "expanded_devices": expanded,
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

    def __init__(self, node: FakeInstanceNode, name: Optional[str] = None):
        self._node = node
        self._name = name

    @property
    def snapshots(self):
        return FakeSnapshots(self._node, self._name)

    def __getattr__(self, item):
        return getattr(self._node, item)


class _Instances:
    def __init__(self, node: FakeInstanceNode):
        self._node = node
        self.created: List[tuple] = []
        # The recursion=2 project listing the fleet route reads. Left as None so the default is
        # derived from the single node, and set explicitly by a test that needs several sources.
        self.project_listing: Optional[List[Dict[str, Any]]] = None
        # The all-projects listing. Entries carry their own `project`, which is what the fleet
        # reader trusts and what the tenant ACL is then asked about.
        self.all_projects_listing: Optional[List[Dict[str, Any]]] = None

    def __getitem__(self, name):
        return _InstanceProxy(self._node, name)

    def get(self, params=None, timeout=None):
        """`recursion=2` folds each instance's snapshots into the instance listing.

        Honours `all-projects` the way LXD does, including refusing a request that carries both
        it and `project`: a caller reusing `_project_params` on the fast path sends that shape,
        and every cluster would answer 400.
        """
        params = dict(params or {})
        if "all-projects" in params and "project" in params:
            raise LXDAPIException("Cannot specify a project when requesting all projects")
        if "all-projects" in params:
            self._node.recorded.append(("list_all_projects", params, timeout))
            return _Response({"metadata": self.all_projects_listing or self._default_listing()})
        self._node.recorded.append(("list_project", params))
        if self.project_listing is not None:
            return _Response({"metadata": self.project_listing})
        return _Response({"metadata": self._default_listing()})

    def _default_listing(self):
        return [
            {
                "name": INSTANCE,
                "project": PROJECT,
                "status": "Running",
                "config": self._node.config,
                "snapshots": list(self._node.snapshots),
            }
        ]

    def post(self, json=None, params=None):
        self.created.append((dict(json or {}), dict(params or {})))
        return _Response({"operation": "/1.0/operations/op-copy"}, 202)


class _Operations:
    """LXD's operation wait endpoint, so a test can make an accepted operation fail."""

    def __init__(self, node: FakeInstanceNode):
        self._node = node

    def __getitem__(self, operation_id):
        # Carry the id so one operation can fail while another settles.
        return _OperationNode(self._node, operation_id)

    def get(self, params=None, timeout=None):
        """The bucketed listing the task journal seeds from, recorded with its params and the
        socket timeout so a test can assert both."""
        params = dict(params or {})
        self._node.recorded.append(("list_operations", params, timeout))
        if "all-projects" in params:
            if self._node.operations_all_projects_error is not None:
                raise self._node.operations_all_projects_error
            return _Response({"metadata": _copy_buckets(self._node.operation_buckets)})
        project = params.get("project")
        refused = getattr(self._node, "operations_project_errors", {}).get(project)
        if refused is not None:
            raise refused
        return _Response(
            {"metadata": _copy_buckets(self._node.operation_buckets_by_project.get(project, {}))}
        )


def _copy_buckets(buckets: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    return {bucket: [dict(op) for op in ops] for bucket, ops in buckets.items()}


class _Warnings:
    def __init__(self, node: FakeInstanceNode):
        self._node = node

    def get(self, params=None, timeout=None):
        self._node.recorded.append(("list_warnings", dict(params or {}), timeout))
        return _Response({"metadata": [dict(w) for w in self._node.warnings]})


class _OperationNode:
    def __init__(self, node, operation_id=None):
        self._node = node
        self._id = operation_id

    def __getitem__(self, operation_id):
        return _OperationNode(self._node, operation_id)

    @property
    def wait(self):
        return _OperationWait(self)

    def _outcome(self):
        """One status for the node, or a different one per operation id.

        A dict makes "the safety copy failed but the restore would have succeeded" reachable,
        which the branch that aborts a restore on a failed safety copy needs. A scalar applies to
        every operation.
        """
        status = self._node.operation_status
        err = self._node.operation_err
        if isinstance(status, dict):
            status = status.get(self._id, status.get("*", "Success"))
        if isinstance(err, dict):
            err = err.get(self._id, err.get("*"))
        return status, err

    def get(self, params=None, timeout=None):
        """The operation itself, which reads Failure and its reason even when the wait refuses."""
        self._node.recorded.append(("get_operation", dict(params or {}), timeout))
        status, err = self._outcome()
        return _Response({"type": "sync", "metadata": {"status": status, "err": err}})


class _OperationWait:
    """`/1.0/operations/<id>/wait`, as LXD answers it.

    important: LXD answers the wait of a failed or cancelled operation with an error-typed body
    (LXD 5.21, ex; a ZFS restore refused for a later snapshot), and pylxd raises that as
    LXDAPIException. A fake returning a plain Failure body would hide failed snapshot writes
    behind "still running".
    """

    def __init__(self, operation: _OperationNode):
        self._operation = operation

    def get(self, params=None, timeout=None):
        # Recorded so a test can assert the socket window outlasts the wait asked of LXD. Without
        # that, the read dies first and every slow operation reports "still running".
        node = self._operation._node
        node.recorded.append(("wait_operation", dict(params or {}), timeout))
        status, err = self._operation._outcome()
        if status in ("Failure", "Cancelled"):
            from pylxd.exceptions import LXDAPIException as PylxdAPIException

            raise PylxdAPIException(
                _Response({"type": "error", "error": err or status, "error_code": 500, "metadata": None})
            )
        return _Response({"type": "sync", "metadata": {"status": status, "err": err}})


class _Projects(_Response):
    """Doubles as the response the mobility code reads and the node the fallback path calls."""

    def __init__(self, node: FakeInstanceNode):
        super().__init__({"metadata": [f"/1.0/projects/{name}" for name in node.projects]})
        self._node = node

    def get(self, params=None, timeout=None):
        self._node.recorded.append(("list_project_names", dict(params or {})))
        return _Response({"metadata": [f"/1.0/projects/{n}" for n in self._node.projects]})


class _Api:
    def __init__(self, node: FakeInstanceNode):
        self._node = node
        self.instances = _Instances(node)
        self.operations = _Operations(node)
        self._api_endpoint = "https://fake:8443/1.0"

    @property
    def projects(self):
        return _Projects(self._node)

    @property
    def warnings(self):
        return _Warnings(self._node)

    @property
    def storage_pools(self):
        return _StoragePools(self._node)


class _StoragePools(_Response):
    """The pool listing, plus one pool's record for the restore plan's driver read."""

    def __init__(self, node: FakeInstanceNode):
        super().__init__({"metadata": ["/1.0/storage-pools/default"]})
        self._node = node

    def __getitem__(self, pool):
        node = self._node

        class _Pool:
            def get(self, params=None, timeout=None):
                node.recorded.append(("get_storage_pool", pool, timeout))
                return _Response({"metadata": {"name": pool, "driver": node.pool_driver}})

        return _Pool()


class FakeClient:
    def __init__(
        self,
        node: Optional[FakeInstanceNode] = None,
        supports_all_projects: bool = True,
    ):
        self.node = node or FakeInstanceNode()
        self.api = _Api(self.node)
        self.host_info = {
            "environment": {
                "certificate": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----"
            },
            # A constructor argument so a test reaches both fleet reader branches; a daemon
            # without the extension takes the fallback path.
            "api_extensions": ["instance_all_projects"] if supports_all_projects else [],
        }


class ConnectionManager:
    """One client for every host, or an explicit per-host mapping.

    Passing `clients` makes the mapping authoritative, even an empty one: a host it omits gets no
    client, which is how a test models an unreachable cluster.
    """

    def __init__(self, client, hosts=(HOST,), clients=None):
        self._client = client
        self._hosts = list(hosts)
        self._clients = None if clients is None else dict(clients)

    def get_client(self, host, *args, **kwargs):
        if self._clients is not None:
            return self._clients.get(host)
        return self._client if host in self._hosts else None

    def get_node_names(self):
        return list(self._hosts)


class _CertManager:
    """Only list_clusters is exercised: LXDService.get_node_names runs apply_cluster_filter over
    it, which scopes a fan-out read that names no host."""

    def __init__(self, hosts):
        self._hosts = list(hosts)

    def list_clusters(self):
        return list(self._hosts)


def service(client, hosts=(HOST,), clients=None):
    """An LXDService over one fake client, or a mapping of host to client for a fan-out test.

    `clients` makes a per-cluster assertion possible: with one client shared by every host, two
    clusters are indistinguishable and a memo that collides across them still passes.
    """
    from app.services.lxd_service import LXDService

    svc = LXDService.__new__(LXDService)
    svc.connection_manager = ConnectionManager(client, hosts, clients)
    svc.cert_manager = _CertManager(hosts)
    return svc
