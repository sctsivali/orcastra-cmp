"""When LXD refuses a create for a project limit, the answer names the limit and the project.

The daemon's refusal is synchronous and reached the console as free text:

    Failed to create VM: Failed checking if instance creation allowed: Reached maximum
    aggregate value "4" for "limits.cpu" in project "web-tier"

Read by a person that is clear enough. Read by the form it is nothing: no field to mark, no
number to compare, no way to tell it from any other 400. The create path now recognises the
two shapes LXD uses (an aggregate limit, an instance count) and raises a typed error the
router answers as a 400 whose detail is structured: code, the limit key, the project, the
maximum, and the daemon's sentence for anyone reading the raw body. Any other 400 keeps its
free text, so nothing else changes shape.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.lxd.exceptions import LXDOperationError, LXDProjectLimitError
from app.services.lxd.project_limits import project_limit_from_lxd_error

AGGREGATE = (
    'Failed checking if instance creation allowed: Reached maximum aggregate value "4" '
    'for "limits.cpu" in project "web-tier"'
)
COUNT = 'Failed checking if instance creation allowed: Reached maximum number of instances in project "web-tier"'
COUNT_TYPED = (
    'Failed checking if instance creation allowed: Reached maximum number of instances of '
    'type "virtual-machine" in project "web-tier"'
)


def test_the_aggregate_error_is_mapped_to_limit_and_project():
    hit = project_limit_from_lxd_error(AGGREGATE)
    assert hit is not None
    assert hit.limit == "limits.cpu"
    assert hit.project == "web-tier"
    assert hit.maximum == "4"


def test_a_memory_aggregate_keeps_its_unit():
    hit = project_limit_from_lxd_error('Reached maximum aggregate value "8GiB" for "limits.memory" in project "p"')
    assert hit is not None and hit.limit == "limits.memory" and hit.maximum == "8GiB"


def test_an_instance_count_error_is_mapped_too():
    hit = project_limit_from_lxd_error(COUNT)
    assert hit is not None and hit.limit == "limits.instances" and hit.project == "web-tier"
    typed = project_limit_from_lxd_error(COUNT_TYPED)
    assert typed is not None and typed.limit == "limits.virtual-machines"


def test_an_unrelated_refusal_is_not_a_limit():
    assert project_limit_from_lxd_error("Failed creating instance record: Instance name already exists") is None
    assert project_limit_from_lxd_error("") is None


def test_the_typed_error_is_still_an_operation_error():
    """Every handler that catches LXDOperationError keeps working unchanged."""
    err = LXDProjectLimitError(f"Failed to create VM: {AGGREGATE}", project_limit_from_lxd_error(AGGREGATE))
    assert isinstance(err, LXDOperationError)
    assert err.hit.limit == "limits.cpu"


class _Service:
    def __init__(self, error):
        self._error = error

    async def create_instance(self, **kwargs):
        raise self._error

    async def fail_create_job(self, *_args, **_kwargs):
        return None


def _prepare_handler(monkeypatch):
    from app.routers import instances as instances_module
    from app.models.schemas import (
        InstanceCreateEffectiveSummary,
        InstanceCreatePreflightResponse,
    )
    from app.services.lxd.create_preflight import CreatePreflightEvaluation
    monkeypatch.setattr(instances_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(instances_module, "project_is_visible", lambda host, project: True)
    monkeypatch.setattr(instances_module, "verify_project_access", lambda _host, project: project)
    monkeypatch.setattr(instances_module.activity_logger, "log_activity", lambda **k: None)

    async def ready_preflight(_request, _service):
        return CreatePreflightEvaluation(
            response=InstanceCreatePreflightResponse(
                ready=True,
                plan_revision="a" * 64,
                effective=InstanceCreateEffectiveSummary(
                    profiles=["default"], nic_names=["eth0"], root_device="root"
                ),
            ),
            effective_config={},
            effective_devices={},
        )

    monkeypatch.setattr(instances_module, "_run_create_preflight", ready_preflight)
    monkeypatch.setattr(
        instances_module,
        "get_operation_job_by_request",
        lambda *_args: asyncio.sleep(0, result=None),
    )
    monkeypatch.setattr(
        instances_module,
        "resolve_cluster_write_policy",
        lambda *_args: asyncio.sleep(0, result=SimpleNamespace()),
    )
    monkeypatch.setattr(
        instances_module,
        "resolve_instance_job_organization",
        lambda *_args, **_kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(
                organization_id=17,
                organization_slug="org-seventeen",
                resolution="exact",
            ),
        ),
    )
    monkeypatch.setattr(
        instances_module, "validate_target_project_isolation", lambda *_args: None
    )
    monkeypatch.setattr(
        instances_module,
        "validate_instance_payload",
        lambda *_args, **_kwargs: None,
    )
    job = SimpleNamespace(
        job_id="job-1", operation_id=None, status="pending"
    )
    monkeypatch.setattr(
        instances_module,
        "create_operation_job",
        lambda **_kwargs: asyncio.sleep(0, result=(job, True)),
    )
    return instances_module


BODY = {"name": "web01", "host": "nuc1", "image": "ubuntu/24.04", "instance_type": "virtual-machine", "project": "web-tier"}


def test_the_router_answers_a_structured_400(monkeypatch):
    err = LXDProjectLimitError(f"Failed to create VM: {AGGREGATE}", project_limit_from_lxd_error(AGGREGATE))
    module = _prepare_handler(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            module.create_instance(
                request=module.InstanceCreateRequest(**BODY),
                idempotency_key="limit-refusal",
                lxd_service=_Service(err),
                db=SimpleNamespace(),
                _=None,
                _rbac=SimpleNamespace(user_id="partner", email="partner@example.test"),
            )
        )
    assert caught.value.status_code == 400
    detail = caught.value.detail
    assert detail["code"] == "PROJECT_LIMIT_REACHED"
    assert detail["limit"] == "limits.cpu"
    assert detail["project"] == "web-tier"
    assert detail["max"] == "4"
    assert "Reached maximum aggregate value" in detail["message"]


def test_an_unrelated_400_is_still_free_text(monkeypatch):
    err = LXDOperationError("Failed to create VM: Invalid devices: Device validation failed for \"root\"")
    module = _prepare_handler(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            module.create_instance(
                request=module.InstanceCreateRequest(**BODY),
                idempotency_key="device-refusal",
                lxd_service=_Service(err),
                db=SimpleNamespace(),
                _=None,
                _rbac=SimpleNamespace(user_id="partner", email="partner@example.test"),
            )
        )
    assert caught.value.status_code == 400
    assert isinstance(caught.value.detail, str)
