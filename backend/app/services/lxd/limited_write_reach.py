"""Carry out a project-limited key's write plan on the cluster, before the write is sent.

core/limited_key_writes.py turns a write into the reads it depends on: the profiles it names or
gets implicitly, a copy's source, a restored snapshot, an image. This module makes those reads and
names the first thing that reaches the host.

important: fails closed. A source, snapshot or image that cannot be read refuses the write, because
what it would apply cannot be judged. A named profile that does not exist is left to LXD, which
refuses the write itself. A read that does not finish in time, or that the cluster answers with an
error, sends nothing (ChecksNotFinished), and the cluster's breaker is left alone for it.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

import requests
from fastapi import HTTPException

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.limited_key_writes import (
    CarriedFrom,
    ImageApplied,
    NamedProfiles,
    PlanStep,
    host_reach,
    limited_key_refusal,
    profile_reach,
)
from app.core.lxd_path import safe_segment
from app.services.lxd.availability import require_client
from app.services.lxd.proxy import socket_timeout_for

logger = logging.getLogger(__name__)
T = TypeVar("T")

_DEFAULT_PROJECT = "default"
_VM_TYPES = ("container", "virtual-machine")


class ChecksNotFinished(Exception):
    """The reads before a limited key's write did not finish, so the write was not sent."""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class ReadRefused(Exception):
    """The cluster answered a plan read with an error other than 404."""


def reader(session: Any, endpoint: str, socket_timeout: Tuple[int, int]) -> Callable[..., Any]:
    def get(path: str, in_project: str, **query: str) -> Any:
        params: Dict[str, str] = dict(query)
        if in_project and in_project != _DEFAULT_PROJECT:
            params["project"] = in_project
        response = session.get(
            f"{endpoint}{path}", params=params or None, verify=False, timeout=socket_timeout,
            allow_redirects=False,
        )
        if response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise ReadRefused(f"LXD answered {response.status_code} to a read the write depends on")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ReadRefused("LXD answered a read the write depends on with no JSON") from exc
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        return {} if metadata is None else metadata

    return get


def _cached_fingerprints(get: Callable[..., Any], step: ImageApplied) -> Optional[List[str]]:
    """Every fingerprint of the create's type cached from that server and alias, in any project.

    important: LXD looks the cached copy up in every project (no project filter, filtered on type)
    and then applies the target project's own record of it, so every candidate is judged. A daemon
    that ignores all-projects lists one project with no project field, and that answer (or no
    list at all) is None: the copy LXD would pick cannot be known.
    """
    cached = get("/images", _DEFAULT_PROJECT, **{"all-projects": "true", "recursion": "1"})
    if not isinstance(cached, list):
        return None
    if any(isinstance(image, dict) and "project" not in image for image in cached):
        return None
    refs = {ref for ref in (step.alias, step.fingerprint) if ref}
    any_type = step.kind not in _VM_TYPES
    found = [step.fingerprint] if step.fingerprint else []
    for image in cached:
        if not isinstance(image, dict) or not image.get("fingerprint"):
            continue
        origin = image.get("update_source") or {}
        if not isinstance(origin, dict) or origin.get("server") != step.server or origin.get("alias") not in refs:
            continue
        if any_type or (image.get("type") or "container") == step.kind:
            found.append(str(image["fingerprint"]))
    return list(dict.fromkeys(found))


def _image_reach(get: Callable[..., Any], profiles_in: Callable[[str], Callable[[str], Any]],
                 step: ImageApplied) -> Optional[str]:
    if step.server:
        found = _cached_fingerprints(get, step)
        if found is None:
            return (f"an image cached from {step.server} that this proxy could not list, "
                    "so name the profiles in the request")
        held = [get(f"/images/{safe_segment(fingerprint)}", step.project) for fingerprint in found]
        # note: a fresh download gets the default profile, so default is judged whatever is cached
        names = ["default"] + [
            str(name) for image in held if isinstance(image, dict) for name in image.get("profiles") or []
        ]
        return profile_reach(profiles_in(step.project), list(dict.fromkeys(names)))
    # note: an image a project does not hold fails the create in LXD versions that look there, so the
    # union of the records that exist is what any version can apply
    held = [image for image in (_local_image(get, step, in_project) for in_project in step.lookup_projects)
            if isinstance(image, dict)]
    if not held:
        return "an image this proxy could not resolve, so name the profiles in the request"
    names = [str(name) for image in held for name in image.get("profiles") or []]
    return profile_reach(profiles_in(step.project), list(dict.fromkeys(names)))


def _local_image(get: Callable[..., Any], step: ImageApplied, in_project: str) -> Any:
    """The record of a local image create's source as `in_project` resolves it, or None."""
    fingerprint = step.fingerprint
    if not fingerprint and step.alias:
        alias = get(f"/images/aliases/{safe_segment(step.alias)}", in_project) or {}
        fingerprint = alias.get("target") if isinstance(alias, dict) else None
    return get(f"/images/{safe_segment(fingerprint)}", in_project) if fingerprint else None


def _carried_reach(get: Callable[..., Any], profiles_in: Callable[[str], Callable[[str], Any]],
                   step: CarriedFrom) -> Optional[str]:
    path = f"/instances/{safe_segment(step.instance)}"
    if step.snapshot:
        path += f"/snapshots/{safe_segment(step.snapshot)}"
    held = get(path, step.read_project)
    if not isinstance(held, dict):
        what = f"snapshot {step.snapshot} of instance" if step.snapshot else "instance"
        return f"{what} {step.instance}, which could not be read"
    if step.with_config:
        # note: LXD skips the source's config keys and devices the body sets itself (judged as the body)
        config = {k: v for k, v in (held.get("config") or {}).items() if k not in step.set_config}
        devices = {k: v for k, v in (held.get("devices") or {}).items() if k not in step.set_devices}
        reach = host_reach({"config": config, "devices": devices})
        if reach:
            return f"instance {step.instance} with {reach}"
    if step.with_profiles:
        return profile_reach(profiles_in(step.judge_project), [str(name) for name in held.get("profiles") or []])
    return None


def limited_write_reach(
    session: Any, endpoint: str, plan: Iterable[PlanStep], socket_timeout: Tuple[int, int]
) -> Optional[str]:
    """Blocking: carry out a plan and name the first thing that reaches the host, or None."""
    get = reader(session, endpoint, socket_timeout)

    def profiles_in(in_project: str) -> Callable[[str], Any]:
        return lambda name: get(f"/profiles/{safe_segment(name)}", in_project)

    for step in plan:
        if isinstance(step, NamedProfiles):
            reach = profile_reach(profiles_in(step.project), step.names)
        elif isinstance(step, CarriedFrom):
            reach = _carried_reach(get, profiles_in, step)
        else:
            reach = _image_reach(get, profiles_in, step)
        if reach:
            return reach
    return None


def proxy_write_reach(lxd_service: Any, cluster_id: str, plan: List[PlanStep]) -> Optional[str]:
    """Blocking: limited_write_reach for a /proxy operation, on the cluster's pooled client."""
    client = require_client(lxd_service.connection_manager.get_client(cluster_id), cluster_id)
    return limited_write_reach(client.api.session, client.api._api_endpoint, plan, socket_timeout_for(client))


async def run_checks(
    check_fn: Callable[..., T],
    *args: Any,
    timeout: float,
    label: str,
    executor: ThreadPoolExecutor,
    max_in_flight: Optional[int],
) -> T:
    """Run the reads a write depends on off the loop as a job of their own, and return their answer.

    important: the write is a separate job that only starts after this returns, so checks that
    time out can never be followed by a late write.
    """
    try:
        return await run_blocking(
            check_fn, *args, timeout=timeout, label=label, executor=executor, max_in_flight=max_in_flight
        )
    except BlockingTimeout as timeout_error:
        raise ChecksNotFinished(
            504, "timeout", f"The write was not sent: the checks before it timed out after {timeout_error.timeout:g}s"
        ) from timeout_error
    except BlockingPoolFull as pool_error:
        raise ChecksNotFinished(503, "overloaded", f"The write was not sent: {pool_error}") from pool_error
    except ReadRefused as refused:
        raise ChecksNotFinished(502, "cluster_unavailable", f"The write was not sent: {refused}") from refused
    except requests.exceptions.Timeout as read_timeout:
        # note: the requests message names the LXD host's address, which the caller has no use for
        raise ChecksNotFinished(
            504, "timeout", "The write was not sent: a read it depends on timed out"
        ) from read_timeout


async def refuse_what_a_limited_write_applies(
    reach_fn: Callable[..., Optional[str]],
    *args: Any,
    allowed_projects: Optional[List[str]],
    timeout: float,
    label: str,
    executor: ThreadPoolExecutor,
    max_in_flight: Optional[int],
) -> None:
    """Run a plan's reads off the loop as a job of their own, then refuse what reaches the host."""
    reach = await run_checks(
        reach_fn, *args, timeout=timeout, label=label, executor=executor, max_in_flight=max_in_flight
    )
    if reach:
        logger.warning("Refusing a write applying %s for a key limited to %s", reach, sorted(allowed_projects or []))
        raise HTTPException(status_code=403, detail=limited_key_refusal(reach, allowed_projects))
