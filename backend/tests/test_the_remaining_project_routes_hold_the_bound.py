"""File push, project update and delete, image upload and node metrics hold the project bound.

A re-scan for routes that check the cluster and then act on a project the caller chose found
these after the instance routes were fixed. On a cluster bound to two organizations a Partner
could push a file into the other organization's instance, change or delete its project, stage
an image into its image store, and count its instances as their own on the node metrics page.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.models.schemas import ProjectUpdateRequest
from app.routers import file_transfer, image_uploads_dashboard, monitoring, projects
from app.routers.image_uploads import CreateUploadRequest
from tests.instance_access_fakes import CLUSTER, AsyncRecorder, Recorder, partner

FOREIGN = "other-db"


class _Upload:
    filename = "notes.txt"

    def __init__(self) -> None:
        self.read_called = False

    async def read(self) -> bytes:
        self.read_called = True
        return b"data"


async def test_a_file_push_into_a_foreign_project_is_refused_before_the_upload_is_read():
    upload = _Upload()
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await file_transfer.push_file_to_instance(
                file=upload, host=CLUSTER, instance_name="web01", project=FOREIGN,
                dest_path="/root/notes.txt", mode="0644", uid=0, gid=0,
                lxd_service=None, _=None,
            )
    assert exc.value.status_code == 403
    assert not upload.read_called


async def test_a_file_push_carries_the_authorized_project(monkeypatch):
    pushed = []

    def fake_push(lxd_service, host, instance_name, project, *rest):
        pushed.append(project)
        return {"status": "ok"}

    monkeypatch.setattr(file_transfer, "_push_file_to_instance", fake_push)
    monkeypatch.setattr(file_transfer, "activity_logger", Recorder())
    with partner({"default", "own-web"}):
        await file_transfer.push_file_to_instance(
            file=_Upload(), host=CLUSTER, instance_name="web01", project="own-web",
            dest_path="/root/notes.txt", mode="0644", uid=0, gid=0, lxd_service=None, _=None,
        )
    assert pushed == ["own-web"]


async def test_a_foreign_project_cannot_be_updated():
    service = AsyncRecorder(returns=[])
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await projects.update_project(
                host=CLUSTER, project_name=FOREIGN,
                request=ProjectUpdateRequest(description="x"),
                lxd_service=service, _=None, _rbac=None,
            )
    assert exc.value.status_code == 403
    assert service.calls == []


async def test_a_foreign_project_cannot_be_deleted():
    service = AsyncRecorder()
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await projects.delete_project(
                host=CLUSTER, project_name=FOREIGN, lxd_service=service, _=None, _rbac=None
            )
    assert exc.value.status_code == 403
    assert service.calls == []


async def test_an_own_project_can_still_be_deleted(monkeypatch):
    service = AsyncRecorder()
    monkeypatch.setattr(projects, "get_cache_manager", lambda: Recorder())
    monkeypatch.setattr(projects, "activity_logger", Recorder())
    monkeypatch.setattr(projects, "schedule_orcahub_event", Recorder().emit)
    with partner({"default", "own-web"}):
        await projects.delete_project(
            host=CLUSTER, project_name="own-web", lxd_service=service, _=None, _rbac=None
        )
    assert service.calls[0][0] == "delete_project"


async def test_an_image_upload_into_a_foreign_project_is_refused():
    body = CreateUploadRequest(
        cluster_id=CLUSTER, project=FOREIGN, alias="img", instance_type="container",
        total_bytes=1, max_bytes=1,
    )
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await image_uploads_dashboard.create_upload(body=body, db=None, _rbac=None, _ro=None)
    assert exc.value.status_code == 403


def _vm(name: str, project: str) -> Any:
    return SimpleNamespace(
        name=name, host=CLUSTER, project=project, status=SimpleNamespace(value="Running")
    )


async def test_node_metrics_count_only_instances_inside_the_bound(monkeypatch):
    resources = SimpleNamespace(
        cpu_total=4, cpu_used=1, memory_total=8, memory_used=2, storage_total=10,
        storage_available=5, gpu_total=0, gpu_used=0, gpu_available=0, gpu_cards_total=0,
        gpu_integrated_total=0, gpu_models=[], gpu_utilization_supported=False,
    )
    node = SimpleNamespace(name=CLUSTER, connected=True, node_type="lxd", resources=resources)

    class _Service:
        async def check_nodes_status_async(self) -> list:
            return [node]

        async def list_all_vms(self) -> tuple:
            return [_vm("web01", "default"), _vm("web01", FOREIGN), _vm("db01", FOREIGN)], [CLUSTER]

    cache = SimpleNamespace(get=lambda *a, **k: None, set=lambda *a, **k: None)
    monkeypatch.setattr(monitoring, "get_cache_manager", lambda: cache)
    with partner({"default"}):
        metrics = await monitoring.get_node_metrics(
            node_name=CLUSTER, lxd_service=_Service(), fresh=True
        )
    assert metrics.instance_count == 1
    assert metrics.running_instances == 1
