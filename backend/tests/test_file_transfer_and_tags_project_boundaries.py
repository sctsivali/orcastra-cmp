"""File transfer and resource tags must authorize the exact project they act on."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.config import get_settings
from app.models.schemas import BulkTagAssignRequest
from app.routers import file_transfer, tags


HOST = "shared-cluster"
INSTANCE = "same-name"
OWN_PROJECT = "partner-owned"
FOREIGN_PROJECT = "other-org"


@pytest.fixture(autouse=True)
def partner_scope():
    allowed = org_scope._allowed_clusters_var.set({HOST})
    projects = org_scope._visible_projects_var.set({HOST: {"default", OWN_PROJECT}})
    acl = org_scope._tenant_acl_var.set(None)
    try:
        yield
    finally:
        org_scope._tenant_acl_var.reset(acl)
        org_scope._visible_projects_var.reset(projects)
        org_scope._allowed_clusters_var.reset(allowed)


class _Upload:
    filename = "payload.bin"

    def __init__(self, payload: bytes = b"payload") -> None:
        self.payload = payload
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.payload[:size]


class _TagService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def get_resource_tags(self, **kwargs):
        self.calls.append(("get", kwargs))
        return {"ok": True}

    async def set_resource_tags(self, **kwargs):
        self.calls.append(("set", kwargs))
        return {"ok": True}

    async def assign_tags_to_resource(self, request, **kwargs):
        self.calls.append(("add", {"request": request, **kwargs}))
        return {"ok": True}

    async def remove_tag_from_resource(self, **kwargs):
        self.calls.append(("remove", kwargs))

    async def import_tags_from_lxd(self, **kwargs):
        self.calls.append(("import", kwargs))
        return {"ok": True}


def _bulk(
    *,
    resource_type: str = "instance",
    resource_id: str = INSTANCE,
    host: str | None = None,
    project: str | None = None,
) -> BulkTagAssignRequest:
    return BulkTagAssignRequest(
        tags=["env_prod"],
        resource_type=resource_type,
        resource_id=resource_id,
        resource_host=host,
        resource_project=project,
    )


def test_file_upload_denies_a_foreign_project_before_reading_the_body():
    upload = _Upload()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            file_transfer.push_file_to_instance(
                file=upload,
                host=HOST,
                instance_name=INSTANCE,
                project=FOREIGN_PROJECT,
                dest_path="/tmp/payload.bin",
                mode="0644",
                uid=0,
                gid=0,
                lxd_service=SimpleNamespace(),
                _=None,
            )
        )

    assert exc.value.status_code == 403
    assert upload.read_sizes == []


def test_file_upload_carries_one_normalized_project_and_bounds_the_read(monkeypatch):
    upload = _Upload()
    pushed: list[tuple] = []
    audit: list[dict] = []

    monkeypatch.setattr(
        file_transfer,
        "_push_file_to_instance",
        lambda *args: pushed.append(args) or {"status": "ok"},
    )
    monkeypatch.setattr(
        file_transfer.activity_logger,
        "log_activity",
        lambda **kwargs: audit.append(kwargs),
    )

    response = asyncio.run(
        file_transfer.push_file_to_instance(
            file=upload,
            host=HOST,
            instance_name=INSTANCE,
            project=f" {OWN_PROJECT} ",
            dest_path="/tmp/payload.bin",
            mode="0644",
            uid=0,
            gid=0,
            lxd_service=SimpleNamespace(),
            _=None,
        )
    )

    assert response["success"] is True
    assert upload.read_sizes == [file_transfer.MAX_UPLOAD_SIZE + 1]
    assert pushed[0][3] == OWN_PROJECT
    assert len(audit) == 1
    assert audit[0]["target_project"] == OWN_PROJECT


def test_tag_body_cannot_bypass_the_shared_cluster_project_bound():
    service = _TagService()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            tags.set_resource_tags(
                resource_type="instance",
                resource_id=INSTANCE,
                request=_bulk(host=HOST, project=FOREIGN_PROJECT),
                host=None,
                project=None,
                user_id="partner@example.test",
                tag_service=service,
                _=None,
                _rbac=SimpleNamespace(),
            )
        )

    assert exc.value.status_code == 403
    assert service.calls == []


def test_explicit_blank_project_cannot_fall_through_to_a_body_target():
    service = _TagService()

    asyncio.run(
        tags.set_resource_tags(
            resource_type="instance",
            resource_id=INSTANCE,
            request=_bulk(host=HOST, project=FOREIGN_PROJECT),
            host=HOST,
            project="",
            user_id="partner@example.test",
            tag_service=service,
            _=None,
            _rbac=SimpleNamespace(),
        )
    )

    assert service.calls[0][1]["resource_project"] == "default"


def test_same_instance_name_reads_only_the_authorized_project():
    service = _TagService()

    asyncio.run(
        tags.get_resource_tags(
            resource_type="instance",
            resource_id=INSTANCE,
            host=HOST,
            project=f" {OWN_PROJECT} ",
            tag_service=service,
        )
    )
    assert service.calls == [
        (
            "get",
            {
                "resource_type": "instance",
                "resource_id": INSTANCE,
                "resource_host": HOST,
                "resource_project": OWN_PROJECT,
            },
        )
    ]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            tags.get_resource_tags(
                resource_type="instance",
                resource_id=INSTANCE,
                host=HOST,
                project=FOREIGN_PROJECT,
                tag_service=service,
            )
        )
    assert exc.value.status_code == 403
    assert len(service.calls) == 1


def test_tenant_view_is_enforced_even_when_operation_enforcement_is_off():
    settings = get_settings()
    previous = settings.tenant_perm_enforcement_enabled
    object.__setattr__(settings, "tenant_perm_enforcement_enabled", False)
    acl = org_scope._tenant_acl_var.set({HOST: {(OWN_PROJECT, "tenant-owned"): {"view"}}})
    projects = org_scope._visible_projects_var.set(None)
    service = _TagService()
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                tags.get_resource_tags(
                    resource_type="instance",
                    resource_id=INSTANCE,
                    host=HOST,
                    project=OWN_PROJECT,
                    tag_service=service,
                )
            )
    finally:
        org_scope._visible_projects_var.reset(projects)
        org_scope._tenant_acl_var.reset(acl)
        object.__setattr__(settings, "tenant_perm_enforcement_enabled", previous)

    assert exc.value.status_code == 403
    assert service.calls == []


def test_tenant_project_tags_require_a_view_grant_before_the_database_read():
    acl = org_scope._tenant_acl_var.set({HOST: {(OWN_PROJECT, "tenant-owned"): {"view"}}})
    projects = org_scope._visible_projects_var.set(None)
    service = _TagService()
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                tags.get_resource_tags(
                    resource_type="project",
                    resource_id=FOREIGN_PROJECT,
                    host=HOST,
                    project=None,
                    tag_service=service,
                )
            )
    finally:
        org_scope._visible_projects_var.reset(projects)
        org_scope._tenant_acl_var.reset(acl)

    assert exc.value.status_code == 403
    assert service.calls == []


def test_project_tag_mutation_denies_before_the_service():
    service = _TagService()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            tags.update_project_tags_legacy(
                host=HOST,
                project_name=FOREIGN_PROJECT,
                request=_bulk(
                    resource_type="project",
                    resource_id=FOREIGN_PROJECT,
                ),
                user_id="partner@example.test",
                tag_service=service,
                _=None,
                _rbac=SimpleNamespace(),
            )
        )

    assert exc.value.status_code == 403
    assert service.calls == []


def test_node_tags_keep_resource_id_as_the_authorized_connection_key():
    service = _TagService()

    asyncio.run(
        tags.set_resource_tags(
            resource_type="node",
            resource_id=HOST,
            request=_bulk(
                resource_type="node",
                resource_id=HOST,
                host="not-the-node",
                project=FOREIGN_PROJECT,
            ),
            host=None,
            project=None,
            user_id="partner@example.test",
            tag_service=service,
            _=None,
            _rbac=SimpleNamespace(),
        )
    )

    assert service.calls[0][1]["resource_host"] is None
    assert service.calls[0][1]["resource_project"] is None
    assert service.calls[0][1]["resource_id"] == HOST


def test_lxd_tag_import_denies_a_foreign_project_before_lxd_io():
    service = _TagService()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            tags.import_from_lxd(
                resource_type="instance",
                resource_id=INSTANCE,
                host=HOST,
                project=FOREIGN_PROJECT,
                user_id="partner@example.test",
                tag_service=service,
                _=None,
                _rbac=SimpleNamespace(),
            )
        )

    assert exc.value.status_code == 403
    assert service.calls == []
