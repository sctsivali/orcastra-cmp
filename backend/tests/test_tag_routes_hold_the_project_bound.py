"""Tag reads and writes are held to the instance, project or node they name.

A tag write is a real LXD write (user.tags on the node, the project or the instance) and a row
keyed by host and project. The routes checked only the cluster in the query, so a Partner on a
cluster bound to two organizations could tag the other organization's instance or project,
a write that named its host in the body instead of the query was not checked at all, and a
node tag wrote to whichever cluster the path named. The two read routes answered for an
instance a Tenant could not see.

The project is normalized for the check only. The tag store keys rows on the raw value, so the
service must keep receiving exactly what it received before.
"""

from typing import Any, Callable

import pytest
from fastapi import HTTPException

from app.models.schemas import BulkTagAssignRequest
from app.routers import tags
from tests.instance_access_fakes import CLUSTER, AsyncRecorder, partner, scoped, tenant

FOREIGN = "other-db"


def _body(**extra: Any) -> BulkTagAssignRequest:
    return BulkTagAssignRequest(
        tags=["env_prod"], resource_type="instance", resource_id="web01", **extra
    )


def _set(service, resource_type="instance", resource_id="web01", host=CLUSTER, project=FOREIGN,
         body=None):
    return tags.set_resource_tags(
        resource_type=resource_type, resource_id=resource_id, request=body or _body(),
        host=host, project=project, user_id=None, tag_service=service, _=None, _rbac=None,
    )


def _add(service, project=FOREIGN):
    return tags.add_resource_tags(
        resource_type="instance", resource_id="web01", request=_body(), host=CLUSTER,
        project=project, user_id=None, tag_service=service, _=None, _rbac=None,
    )


def _remove(service, project=FOREIGN):
    return tags.remove_resource_tag(
        resource_type="instance", resource_id="web01", tag_name="env_prod", host=CLUSTER,
        project=project, tag_service=service, _=None, _rbac=None,
    )


def _import(service, project=FOREIGN):
    return tags.import_from_lxd(
        resource_type="instance", resource_id="web01", host=CLUSTER, project=project,
        user_id=None, tag_service=service, _=None, _rbac=None,
    )


def _legacy_instance(service, project=FOREIGN):
    return tags.update_instance_tags_legacy(
        host=CLUSTER, instance_name="web01", request=_body(), project=project,
        user_id=None, tag_service=service, _=None, _rbac=None,
    )


def _legacy_project(service, project=FOREIGN):
    return tags.update_project_tags_legacy(
        host=CLUSTER, project_name=project, request=_body(), user_id=None,
        tag_service=service, _=None, _rbac=None,
    )


def _generic_project(service, project=FOREIGN):
    return _set(service, resource_type="project", resource_id=project, project=None)


WRITES: dict[str, Callable] = {
    "set": _set,
    "add": _add,
    "remove": _remove,
    "import": _import,
    "legacy-instance": _legacy_instance,
    "legacy-project": _legacy_project,
    "generic-project": _generic_project,
}


@pytest.mark.parametrize("label", sorted(WRITES))
async def test_a_write_into_a_foreign_project_is_refused(label):
    service = AsyncRecorder()
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await WRITES[label](service)
    assert exc.value.status_code == 403
    assert service.calls == [], "the tag service was reached, so the row and LXD were written"


@pytest.mark.parametrize("label", sorted(WRITES))
async def test_a_write_inside_the_bound_goes_through(label):
    service = AsyncRecorder()
    with partner({"default", "own-web"}):
        await WRITES[label](service, project="own-web")
    assert len(service.calls) == 1


async def test_a_host_named_only_in_the_body_is_checked():
    service = AsyncRecorder()
    with scoped(allowed={CLUSTER}):
        with pytest.raises(HTTPException) as exc:
            await _set(service, host=None, project=None, body=_body(resource_host="cluster-b"))
    assert exc.value.status_code == 403
    assert service.calls == []


async def test_a_node_tag_is_held_to_the_callers_clusters():
    service = AsyncRecorder()
    with scoped(allowed={CLUSTER}):
        with pytest.raises(HTTPException) as exc:
            await _set(service, resource_type="node", resource_id="cluster-b", host=None, project=None)
        await _set(service, resource_type="node", resource_id=CLUSTER, host=None, project=None)
    assert exc.value.status_code == 403
    assert len(service.calls) == 1


async def test_the_service_still_receives_the_raw_project():
    """The tag store matches resource_project exactly, so None must stay None."""
    service = AsyncRecorder()
    with partner({"default"}):
        await _set(service, project=None)
    assert service.calls[0][2]["resource_project"] is None


async def test_a_tenant_cannot_read_tags_of_an_instance_it_cannot_view():
    service = AsyncRecorder()
    with tenant({("own-web", "other"): {"view"}}):
        with pytest.raises(HTTPException) as generic:
            await tags.get_resource_tags(
                resource_type="instance", resource_id="web01", host=CLUSTER,
                project="own-web", tag_service=service,
            )
        with pytest.raises(HTTPException) as legacy:
            await tags.get_instance_tags_legacy(
                host=CLUSTER, instance_name="web01", project="own-web", tag_service=service
            )
    assert generic.value.status_code == legacy.value.status_code == 403
    assert service.calls == []


async def test_a_tenant_reads_tags_of_its_own_instance():
    service = AsyncRecorder()
    with tenant({("own-web", "web01"): {"view"}}):
        await tags.get_resource_tags(
            resource_type="instance", resource_id="web01", host=CLUSTER,
            project="own-web", tag_service=service,
        )
    assert service.calls[0][2]["resource_project"] == "own-web"
