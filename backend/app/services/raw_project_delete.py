"""A project DELETE that arrives through the raw proxy, settled like every other project delete.

OrcaHub removes a buyer's project at the end of its retention window with
`POST /integrations/raw-proxy` `DELETE /1.0/projects/<name>`. The raw path sent that straight to
LXD and only cleared caches, so the tenant's `auto:<project>` rule outlived the project and kept
the cluster in the tenant's scope: the buyer still saw the cluster on Overview with no projects
and no instances. The dashboard route and the structured proxy already delete through
`services/project_deletion`, which revokes the rules once the project is known to be gone. This
module lets the raw path use that same core: it recognises the request and classifies LXD's
answer into the outcomes the core understands, keeping LXD's own answer for the caller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import requests
from fastapi import HTTPException
from pylxd.exceptions import LXDAPIException

from app.core.blocking import BlockingTimeout, run_blocking
from app.core.lxd_errors import definitive_lxd_refusal
from app.core.raw_proxy_path import canonical_proxy_path
from app.services import project_deletion
from app.services.lxd.exceptions import (
    LXDOperationError,
    LXDOperationUnknownError,
    LXDProjectNotFoundError,
)
from app.services.lxd.project_delete import DELETE_OPERATION_SECONDS
from app.services.lxd.proxy import (
    PROXY_MAX_IN_FLIGHT,
    RAW_PROXY_TIMEOUT,
    proxy_executor,
    raw_request,
    socket_timeout_for,
)
from app.services.lxd.utils import wait_for_lxd_operation


class RawDeleteRefused(Exception):
    """LXD refused the DELETE itself (a 4xx other than 404); its answer goes back unchanged."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"LXD refused the project delete with HTTP {status_code}")


@dataclass
class RawDeleteAnswer:
    """What LXD said to the DELETE, kept so the raw caller receives exactly that."""

    status_code: Optional[int] = None
    data: Any = None


def raw_project_delete_target(method: str, path: Optional[str]) -> Optional[str]:
    """The project a raw request deletes, or None when it is anything else.

    Exactly `DELETE /1.0/projects/<name>`: a deeper path (a project's state) or another verb is
    not a project delete. A path the canonical parser refuses is left to the proxy's own guards.
    """
    if (method or "").upper() != "DELETE":
        return None
    try:
        canonical = canonical_proxy_path(path)
    except HTTPException:
        return None
    if len(canonical.segments) == 2 and canonical.lowered[0] == "projects":
        return canonical.segments[1]
    return None


def _json_or_raw(response: Any) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body still goes back to the caller as text
        return {"raw": getattr(response, "text", "")}


async def send_and_classify(
    name: str,
    answer: RawDeleteAnswer,
    *,
    request_once: Callable[[], Awaitable[Any]],
    wait_operation: Callable[[Any], Awaitable[None]],
) -> None:
    """Send the DELETE once and turn LXD's answer into an outcome the deletion core acts on.

    Returns when the project is gone. Raises LXDProjectNotFoundError for a 404,
    RawDeleteRefused for LXD's own 4xx refusal, LXDOperationError when an accepted operation
    failed, and LXDOperationUnknownError whenever the request may have reached LXD and its result
    is not known (a timeout, a dropped connection, a 5xx, an operation still running).

    important: once the request may have left, a failure is never a refusal. The core keeps the
    captured rules for the sweeper on an unknown outcome and revokes nothing until it reads 404.
    """
    try:
        response = await request_once()
    except (BlockingTimeout, requests.exceptions.RequestException) as exc:
        raise LXDOperationUnknownError("delete", name) from exc
    answer.status_code = response.status_code
    answer.data = _json_or_raw(response)
    status = response.status_code
    if status == 404:
        raise LXDProjectNotFoundError(name)
    if status == 202:
        try:
            await wait_operation(response)
        except LXDAPIException as exc:
            if definitive_lxd_refusal(exc, "waiting"):
                raise LXDOperationError(f"Could not delete project: {exc}") from exc
            raise LXDOperationUnknownError("delete", name) from exc
        except (BlockingTimeout, requests.exceptions.RequestException) as exc:
            raise LXDOperationUnknownError("delete", name) from exc
        return
    if 200 <= status < 300:
        return
    if 400 <= status < 500:
        raise RawDeleteRefused(status)
    raise LXDOperationUnknownError("delete", name)


async def delete_through_core(
    lxd_service: Any,
    client: Any,
    *,
    cluster_id: str,
    name: str,
    scope_project: str,
    requester: Optional[str],
    full_url: str,
    body: Any,
) -> RawDeleteAnswer:
    """Run the raw DELETE inside the shared deletion core and return LXD's answer to it.

    The core holds the cluster lock, records the follow-up, revokes the rules naming the project
    once it is gone and announces it. Its own outcomes propagate: ProjectDeleteUnknown,
    ClusterConfigurationBusy (ProjectDeletesBacklogged included) and MigrationFenceConflict. A
    refusal or a failed operation returns LXD's answer, since the project is still there.

    important: spawned and awaited shielded, like the structured proxy's delete, so a caller that
    disconnects cannot cut short the revoke of a project LXD already removed.
    """
    answer = RawDeleteAnswer()

    async def request_once() -> Any:
        return await run_blocking(
            raw_request,
            client.api.session,
            "DELETE",
            full_url,
            body,
            socket_timeout_for(client),
            timeout=RAW_PROXY_TIMEOUT,
            label=f"raw_proxy[DELETE project {cluster_id}]",
            executor=proxy_executor,
            max_in_flight=PROXY_MAX_IN_FLIGHT,
        )

    async def wait_operation(response: Any) -> None:
        # note: the operation budget is the project service's own (lxd/project_delete.py), passed
        # positionally because run_blocking keeps `timeout` for its own deadline.
        await run_blocking(
            wait_for_lxd_operation,
            client,
            response,
            DELETE_OPERATION_SECONDS,
            timeout=DELETE_OPERATION_SECONDS + RAW_PROXY_TIMEOUT,
            label=f"raw_proxy[wait project delete {cluster_id}]",
            executor=proxy_executor,
            max_in_flight=PROXY_MAX_IN_FLIGHT,
        )

    async def send() -> None:
        await send_and_classify(name, answer, request_once=request_once, wait_operation=wait_operation)

    task = project_deletion.spawn(
        project_deletion.delete_for_integration(
            lxd_service, cluster_id, name, requester=requester, scope_project=scope_project, send=send
        )
    )
    try:
        await asyncio.shield(task)
    except (RawDeleteRefused, LXDOperationError) as refused:
        if isinstance(refused, LXDOperationUnknownError):
            raise
        if answer.status_code is None or answer.status_code < 400:
            # The DELETE was accepted and its operation then failed: say so in LXD's own shape.
            answer.status_code = 400
            answer.data = {"type": "error", "error": str(refused), "error_code": 400, "metadata": None}
    return answer
