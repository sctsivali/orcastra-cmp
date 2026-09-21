"""Authoritative resource checks for dashboard instance-create preflight."""

from types import SimpleNamespace

import pytest

from app.models.schemas import InstanceCreateRequest
from app.services.lxd import create_preflight as preflight_module
from app.services.lxd.create_preflight import CreatePreflightUnavailable, _inspect_create
from app.services.lxd.project_usage import ProjectUsage


GIB = 1024**3
DEFAULT_PROJECT = "default"


class _Response:
    def __init__(self, metadata):
        self._metadata = metadata

    def json(self):
        return {"metadata": self._metadata}


class _HTTPError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status_code)


class _Node:
    def __init__(self, client, path=()):
        self._client = client
        self._path = path

    def __getitem__(self, segment):
        return _Node(self._client, (*self._path, str(segment)))

    def get(self, params=None, timeout=None):
        params = params or {}
        self._client.reads.append((self._path, dict(params), timeout))
        if self._path == ("projects",):
            rows = [{"name": name, "config": config} for name, config in self._client.projects.items()]
            return _Response(rows)
        if len(self._path) == 1 and self._path[0] in self._client.collections:
            collection = self._path[0]
            error = self._client.collection_errors.get(collection)
            if error is not None:
                raise error
            project = params.get("project", DEFAULT_PROJECT)
            return _Response(self._client.collections[collection].get(project, []))
        if len(self._path) == 2 and self._path[0] == "storage-pools":
            pool = self._path[1]
            error = self._client.pool_errors.get(pool)
            if error is not None:
                raise error
            if pool not in self._client.pools:
                raise _HTTPError("Storage pool not found", 404)
            return _Response(self._client.pools[pool])
        raise AssertionError(f"Unexpected daemon read: {self._path}")


class _Api(_Node):
    def __init__(self, client):
        super().__init__(client)

    @property
    def projects(self):
        return self["projects"]

    @property
    def storage_pools(self):
        return self["storage-pools"]


class _Client:
    def __init__(self, *, project=DEFAULT_PROJECT, root_size="10GiB"):
        root = {"type": "disk", "path": "/", "pool": "fast"}
        if root_size is not None:
            root["size"] = root_size
        self.projects = {
            DEFAULT_PROJECT: {},
            project: {
                "features.images": "true",
                "features.networks": "true",
                "features.profiles": "true",
            },
        }
        self.collections = {
            "profiles": {
                project: [
                    {
                        "name": "default",
                        "config": {},
                        "devices": {
                            "root": root,
                            "eth0": {"type": "nic", "network": "lxdbr0"},
                        },
                    }
                ]
            },
            "networks": {
                project: [
                    {
                        "name": "lxdbr0",
                        "managed": True,
                        "config": {"ipv4.address": "10.0.5.1/24"},
                    }
                ]
            },
            "instances": {project: []},
            "images": {
                project: [
                    {
                        "fingerprint": "a" * 64,
                        "aliases": [{"name": "ubuntu/24.04"}],
                        "type": "virtual-machine",
                    }
                ]
            },
        }
        self.pools = {"fast": {"name": "fast", "driver": "zfs", "status": "Created"}}
        self.collection_errors = {}
        self.pool_errors = {}
        self.reads = []
        self.project = None
        self.api = _Api(self)


class _Service:
    def __init__(self, client):
        self.client = client

    def get_client_with_reconnect(self, _host):
        return self.client


def _usage(
    *,
    cpu_limit=None,
    memory_limit=None,
    disk_limit=None,
    cpu_used=0,
    memory_used=0,
    disk_used=0,
    complete=True,
    complete_fields=None,
):
    return ProjectUsage(
        limits={
            "cpu": cpu_limit,
            "memory_bytes": memory_limit,
            "instances": None,
            "containers": None,
            "virtual_machines": None,
            "disk_bytes": disk_limit,
        },
        used={
            "cpu": cpu_used,
            "memory_bytes": memory_used,
            "instances": 0,
            "containers": 0,
            "virtual_machines": 0,
            "disk_bytes": disk_used,
        },
        complete=complete,
        complete_fields=complete_fields,
    )


@pytest.fixture(autouse=True)
def _unlimited_project(monkeypatch):
    monkeypatch.setattr(
        preflight_module,
        "read_project_usage",
        lambda _client, _project: _usage(),
    )


def _request(*, project=DEFAULT_PROJECT, **changes):
    body = {
        "name": "web01",
        "host": "nuc1",
        "project": project,
        "image": "ubuntu/24.04",
        "instance_type": "virtual-machine",
        "config": {"limits.cpu": "2", "limits.memory": "2GiB"},
    }
    body.update(changes)
    return InstanceCreateRequest(**body)


def _run(client, request=None):
    return _inspect_create(_Service(client), request or _request()).response


def _codes(response):
    return [issue.code for issue in response.blockers]


def test_project_aggregate_cpu_memory_and_disk_are_projected(monkeypatch):
    monkeypatch.setattr(
        preflight_module,
        "read_project_usage",
        lambda _client, _project: _usage(
            cpu_limit=4,
            memory_limit=8 * GIB,
            disk_limit=20 * GIB,
            cpu_used=3,
            memory_used=7 * GIB,
            disk_used=15 * GIB,
        ),
    )

    response = _run(_Client())

    assert response.ready is False
    assert _codes(response).count("PROJECT_QUOTA_EXCEEDED") == 3
    messages = " ".join(issue.message for issue in response.blockers)
    assert "limits.cpu" in messages
    assert "limits.memory" in messages
    assert "root disk size" in messages


def test_incomplete_usage_only_blocks_a_configured_relevant_quota(monkeypatch):
    usage = _usage(
        cpu_limit=4,
        complete=False,
        complete_fields={"memory_bytes", "disk_bytes"},
    )
    monkeypatch.setattr(preflight_module, "read_project_usage", lambda _client, _project: usage)

    response = _run(_Client())

    assert _codes(response) == ["QUOTA_USAGE_UNAVAILABLE"]
    assert response.blockers[0].field == "config"

    usage.limits["cpu"] = None
    assert _run(_Client()).ready is True


def test_a_configured_quota_requires_a_countable_effective_value(monkeypatch):
    monkeypatch.setattr(
        preflight_module,
        "read_project_usage",
        lambda _client, _project: _usage(
            cpu_limit=8,
            memory_limit=8 * GIB,
            disk_limit=40 * GIB,
        ),
    )
    client = _Client(root_size=None)

    response = _run(
        client,
        _request(config={"limits.cpu": "invalid", "limits.memory": "50%"}),
    )

    assert _codes(response).count("PROJECT_QUOTA_VALUE_REQUIRED") == 3


def test_quota_allocation_changes_the_plan_revision(monkeypatch):
    usage = _usage(cpu_limit=8, cpu_used=1)
    monkeypatch.setattr(preflight_module, "read_project_usage", lambda _client, _project: usage)
    client = _Client()

    before = _run(client).plan_revision
    usage.used["cpu"] = 2
    after = _run(client).plan_revision

    assert before != after


def test_local_image_is_read_from_the_project_that_owns_images():
    project = "tenant-a"
    client = _Client(project=project)

    response = _run(client, _request(project=project))

    assert response.ready is True
    assert any(path == ("images",) and params.get("project") == project for path, params, _timeout in client.reads)


def test_a_project_that_inherits_images_is_checked_in_default():
    project = "tenant-a"
    client = _Client(project=project)
    client.projects[project]["features.images"] = "false"
    client.collections["images"][DEFAULT_PROJECT] = client.collections["images"].pop(project)

    response = _run(client, _request(project=project))

    assert response.ready is True
    assert any(
        path == ("images",) and params.get("project") == DEFAULT_PROJECT for path, params, _timeout in client.reads
    )


def test_missing_local_image_is_a_structured_blocker():
    client = _Client()
    client.collections["images"][DEFAULT_PROJECT] = []

    response = _run(client)

    assert "IMAGE_NOT_FOUND" in _codes(response)
    assert response.ready is False


def test_an_image_for_the_other_instance_type_is_refused():
    client = _Client()
    client.collections["images"][DEFAULT_PROJECT][0]["type"] = "container"

    response = _run(client)

    assert "IMAGE_TYPE_MISMATCH" in _codes(response)


def test_an_unreadable_local_image_store_is_not_reported_as_empty():
    client = _Client()
    client.collection_errors["images"] = RuntimeError("read timed out")

    with pytest.raises(CreatePreflightUnavailable, match="Could not verify image"):
        _run(client)


def test_remote_pull_does_not_require_a_local_cached_image():
    client = _Client()
    client.collections["images"][DEFAULT_PROJECT] = []

    response = _run(
        client,
        _request(
            image="ubuntu/24.04",
            image_server="https://cloud-images.ubuntu.com/releases",
            image_protocol="simplestreams",
        ),
    )

    assert response.ready is True
    assert not any(path == ("images",) for path, _params, _timeout in client.reads)


def test_blank_source_does_not_read_an_image_store():
    client = _Client()

    response = _run(
        client,
        _request(image=None, source_type="none", instance_type="virtual-machine"),
    )

    assert response.ready is True
    assert not any(path == ("images",) for path, _params, _timeout in client.reads)


def test_missing_and_unready_declared_storage_pools_are_blockers():
    missing = _Client()
    missing.pools = {}
    missing_response = _run(missing)
    assert "STORAGE_POOL_NOT_FOUND" in _codes(missing_response)

    pending = _Client()
    pending.pools["fast"]["status"] = "Pending"
    pending_response = _run(pending)
    assert "STORAGE_POOL_UNAVAILABLE" in _codes(pending_response)


def test_an_unreadable_declared_storage_pool_fails_closed():
    client = _Client()
    client.pool_errors["fast"] = RuntimeError("connection reset")

    with pytest.raises(CreatePreflightUnavailable, match="storage pool 'fast'"):
        _run(client)

    client = _Client()
    del client.pools["fast"]["status"]
    with pytest.raises(CreatePreflightUnavailable, match="availability status"):
        _run(client)


def test_an_unreadable_project_quota_is_not_reported_as_available(monkeypatch):
    def _fail(_client, _project):
        raise RuntimeError("project read timed out")

    monkeypatch.setattr(preflight_module, "read_project_usage", _fail)

    with pytest.raises(CreatePreflightUnavailable, match="resource limits"):
        _run(_Client())


def test_an_omitted_pool_is_left_to_the_daemon_default():
    client = _Client()
    del client.collections["profiles"][DEFAULT_PROJECT][0]["devices"]["root"]["pool"]

    response = _run(client)

    assert response.ready is True
    assert not any(path[:1] == ("storage-pools",) for path, _params, _timeout in client.reads)
