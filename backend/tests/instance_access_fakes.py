"""Shared fakes for the instance isolation tests.

A request scope is three ContextVars (allowed clusters, tenant ACL, project bound), a cluster
is a fake pylxd client that holds named instances per project, and the LXD service records the
project every call received so a test can assert which project was acted on.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator, Optional

from app.core import org_scope

CLUSTER = "cluster-a"


@contextmanager
def scoped(
    allowed: Optional[set[str]] = None,
    acl: Optional[dict] = None,
    bound: Optional[dict[str, set[str]]] = None,
) -> Iterator[None]:
    """Run the body as a caller with this cluster set, tenant ACL and project bound."""
    tokens = [
        (org_scope._allowed_clusters_var, org_scope._allowed_clusters_var.set(allowed)),
        (org_scope._tenant_acl_var, org_scope._tenant_acl_var.set(acl)),
        (org_scope._visible_projects_var, org_scope._visible_projects_var.set(bound)),
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def partner(bound_projects: set[str]) -> Any:
    """A Partner on a shared cluster, held to `bound_projects` there."""
    return scoped(allowed={CLUSTER}, acl=None, bound={CLUSTER: set(bound_projects)})


def tenant(rules: dict[tuple[str, str], set[str]]) -> Any:
    """A Tenant whose ACL on the cluster is `rules`, with no project bound."""
    return scoped(allowed={CLUSTER}, acl={CLUSTER: rules}, bound=None)


class _Response:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def json(self) -> dict:
        return self._body


class _Instances:
    def __init__(self, holds: dict[str, set[str]]):
        self.holds = holds

    def __getitem__(self, name: str) -> Any:
        def get(params: Optional[dict] = None, **_: Any) -> _Response:
            project = (params or {}).get("project")
            if name not in self.holds.get(project, set()):
                raise RuntimeError(f"{name} not in {project}")
            return _Response({"metadata": {
                "name": name, "status": "Running", "status_code": 103,
                "architecture": "x86_64", "type": "container", "config": {},
                "devices": {}, "expanded_devices": {}, "profiles": ["default"],
            }})

        return SimpleNamespace(get=get)


class FakeClient:
    """A cluster holding `holds[project]` instances, listing projects in `order`."""

    def __init__(self, holds: dict[str, set[str]], order: Optional[list[str]] = None):
        names = order or list(holds)
        self.projects = SimpleNamespace(all=lambda: [SimpleNamespace(name=n) for n in names])
        self.api = SimpleNamespace(instances=_Instances(holds))


class FakeLXDService:
    """Records the project every instance call was given."""

    def __init__(self, client: Optional[FakeClient]):
        self.client = client
        self.calls: list[tuple[str, Optional[str]]] = []

    def get_client_with_reconnect(self, cluster_id: str) -> Optional[FakeClient]:
        return self.client

    def _record(self, action: str, project: Optional[str]) -> None:
        self.calls.append((action, project))

    async def start_vm(self, name: str, host: str, force: bool = False,
                       project: Optional[str] = None) -> Any:
        self._record("start", project)
        return SimpleNamespace(status="Running")

    async def stop_vm(self, name: str, host: str, force: bool = False, timeout: int = 30,
                      project: Optional[str] = None) -> Any:
        self._record("stop", project)
        return SimpleNamespace(status="Stopped")

    async def restart_vm(self, name: str, host: str, force: bool = False, timeout: int = 30,
                         project: Optional[str] = None) -> Any:
        self._record("restart", project)
        return SimpleNamespace(status="Running")

    async def delete_vm(self, name: str, host: str, force: bool = False,
                        project: Optional[str] = None) -> bool:
        self._record("delete", project)
        return True

    async def get_vm(self, name: str, host: str, project: Optional[str] = None) -> Any:
        self._record("get", project)
        return SimpleNamespace(name=name, host=host, project=project)

    async def get_instance_metrics(self, host: str, instance_name: str,
                                   project: str = "default") -> Any:
        self._record("metrics", project)
        return SimpleNamespace(instance_name=instance_name, host=host)

    async def get_rebuild_preflight(self, name: str, host: str,
                                    project: Optional[str] = None) -> dict:
        self._record("preflight", project)
        return {"project": project, "status": "Stopped", "snapshots": [], "config": {},
                "attached_volumes": {}, "base_image": None, "type": "container"}


class Recorder:
    """Stands in for any collaborator: every attribute is a callable that records its call."""

    def __init__(self, returns: Any = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._returns = returns

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return self._returns

        return call


class AsyncRecorder(Recorder):
    """A Recorder whose calls are awaitable."""

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return self._returns

        return call
