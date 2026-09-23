"""A blocking call offloaded to a worker runs with the request's org scope.

run_blocking used loop.run_in_executor, which does not copy ContextVars, so every scope read in
the worker returned its default, and the defaults fail open: no cluster filter, no project
bound, no tenant ACL. create_network's project-bound check ran in such a worker and never
refused. The check now also runs on the loop, and run_blocking carries the context.
"""

import contextvars
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.blocking import run_blocking
from app.routers import networks
from tests.instance_access_fakes import CLUSTER, partner, scoped

MARK: contextvars.ContextVar[str] = contextvars.ContextVar("qa_mark", default="unset")


@pytest.fixture
def pool():
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test_scope")
    yield executor
    executor.shutdown(wait=False)


async def test_a_worker_reads_the_callers_context_vars(pool) -> None:
    token = MARK.set("request")
    try:
        seen = await run_blocking(MARK.get, timeout=5, label="mark", executor=pool)
    finally:
        MARK.reset(token)
    assert seen == "request"


async def test_a_worker_sees_the_project_bound(pool) -> None:
    with partner({"default"}):
        seen = await run_blocking(
            org_scope.visible_projects_for, CLUSTER, timeout=5, label="bound", executor=pool
        )
    assert seen == {"default"}


async def test_a_worker_cannot_change_the_callers_context(pool) -> None:
    def overwrite() -> str:
        MARK.set("worker")
        return MARK.get()

    token = MARK.set("request")
    try:
        assert await run_blocking(overwrite, timeout=5, label="overwrite", executor=pool) == "worker"
        assert MARK.get() == "request"
    finally:
        MARK.reset(token)


class _Service:
    def __init__(self) -> None:
        self.asked: List[str] = []

    def get_client_with_reconnect(self, host: str) -> Any:
        self.asked.append(host)
        raise RuntimeError("the offload was reached")


def _body() -> networks.NetworkCreateRequest:
    return networks.NetworkCreateRequest(name="qa-net", type="ovn")


async def test_a_network_in_another_orgs_project_is_refused_on_the_loop() -> None:
    service = _Service()
    with partner({"default", "own-web"}):
        with pytest.raises(HTTPException) as caught:
            await networks.create_network(
                request=_body(), host=CLUSTER, project="other-org", lxd_service=service, _=None, _rbac=None
            )
    assert caught.value.status_code == 403
    assert service.asked == [], "the create reached the cluster before the bound was checked"


@pytest.mark.parametrize("project", [None, "default", "own-web"])
async def test_a_network_inside_the_bound_goes_on_to_the_cluster(project: Any) -> None:
    service = _Service()
    with partner({"default", "own-web"}):
        with pytest.raises(Exception):
            await networks.create_network(
                request=_body(), host=CLUSTER, project=project, lxd_service=service, _=None, _rbac=None
            )
    assert service.asked == [CLUSTER]


async def test_an_unbounded_caller_goes_on_to_the_cluster() -> None:
    service = _Service()
    with scoped(allowed={CLUSTER}):
        with pytest.raises(Exception):
            await networks.create_network(
                request=_body(), host=CLUSTER, project="anything", lxd_service=service, _=None, _rbac=None
            )
    assert service.asked == [CLUSTER]
