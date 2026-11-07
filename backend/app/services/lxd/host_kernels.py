"""Every cluster member's kernel, as LXD reports it, for the kernel advisories.

A standalone host is one entry taken from `host_info`, which the pooled client refreshes on
connect. A cluster member answers `GET /1.0?target=<member>` with its own environment, so a
cluster is read member by member: a cluster patched on one member and not another is exactly
the case a single endpoint read would hide.

important: only facts are cached, never verdicts. The advisory data is code, so a deploy that
learns of a vendor fix changes every verdict at once instead of waiting out a cache entry.
Kernels change only on a reboot, which is why a complete read is kept for ten minutes.

important: a host is an untrusted source here. A Partner can register any endpoint, so every
fact is type-checked and capped, a cluster is read at most MEMBER_CAP members deep through its
own small pool with one deadline, and a member answer is size- and time-bounded. One hostile or
broken host degrades to its own "unverified" rows; it cannot fail or starve the fleet's report.

note: a slow or refused `?target` read is recorded on that member and never counts against
the cluster's breaker. The endpoint answered; only one member did not.
"""

import asyncio
import contextvars
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.cache import HOST_KERNELS_CACHE_PREFIX
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.fanout import classify
from app.services.lxd.lazy_client import connect_or_get_client
from app.services.lxd.member_reads import members_read_failed, read_members_list
from app.services.lxd.resources import PUBLIC_ENVIRONMENT_KEYS

logger = logging.getLogger(__name__)

HOST_KERNELS_TTL = 600
HOST_KERNELS_DEGRADED_TTL = 60
MEMBER_READ_TIMEOUT = (3.0, 8.0)
MEMBER_CAP = 64
MEMBER_READS_PER_CLUSTER = 4
MEMBER_PHASE_SECONDS = 20.0
MEMBER_BODY_LIMIT = 256 * 1024
_TEXT_CAP = 128
# note: server_version rides along so the release check can see a rolling upgrade
# (services/hypervisor_releases); the read is the same, and the version is a fact like the kernel.
_KERNEL_KEYS = ("kernel_version", "os_name", "os_version", "instance_types", "server_name", "server_version")
_TEXT_FIELDS = ("kernel_version", "os_name", "os_version", "server_version")

_cluster_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="host_kernel_cluster")


@dataclass
class MemberKernel:
    member: str
    kernel_version: Optional[str] = None
    os_name: Optional[str] = None
    os_version: Optional[str] = None
    instance_types: List[str] = field(default_factory=list)
    error: Optional[str] = None
    server_version: Optional[str] = None


@dataclass
class ClusterKernels:
    cluster_id: str
    clustered: bool
    members_listed: bool
    members: List[MemberKernel]
    collected_at: str

    @property
    def complete(self) -> bool:
        return self.members_listed and all(m.error is None for m in self.members)


@dataclass
class FleetKernels:
    clusters: List[ClusterKernels] = field(default_factory=list)
    unreachable_nodes: List[str] = field(default_factory=list)
    node_errors: Dict[str, str] = field(default_factory=dict)


def _cache_key(cluster_id: str) -> str:
    return f"{HOST_KERNELS_CACHE_PREFIX}:{cluster_id}"


def _text(value: Any) -> Optional[str]:
    return value[:_TEXT_CAP] if isinstance(value, str) and value else None


def _member_record(record: Dict[str, Any]) -> MemberKernel:
    """A MemberKernel from untrusted data: LXD's environment or a cache entry.

    A field of the wrong type is dropped and named in `error`, so the member reads as
    unverified instead of failing whatever judges it.
    """
    invalid = [k for k in _TEXT_FIELDS if record.get(k) is not None and not isinstance(record.get(k), str)]
    types = record.get("instance_types")
    if types is not None and not (isinstance(types, list) and all(isinstance(t, str) for t in types)):
        invalid.append("instance_types")
    error = _text(record.get("error"))
    if invalid and not error:
        error = f"invalid: {', '.join(invalid)} not text"
    return MemberKernel(
        member=_text(record.get("member")) or "unknown",
        kernel_version=_text(record.get("kernel_version")),
        os_name=_text(record.get("os_name")),
        os_version=_text(record.get("os_version")),
        instance_types=[t[:32] for t in types[:8]] if "instance_types" not in invalid and types else [],
        error=error,
        server_version=_text(record.get("server_version")),
    )


def _member_from(name: str, environment: Optional[Dict[str, Any]]) -> MemberKernel:
    env = environment if isinstance(environment, dict) else {}
    facts = {k: v for k, v in env.items() if k in PUBLIC_ENVIRONMENT_KEYS and k in _KERNEL_KEYS}
    return _member_record({**facts, "member": name})


def _endpoint_name(cluster_id: str, environment: Dict[str, Any]) -> str:
    name = _text(environment.get("server_name")) or ""
    return name if name and name.lower() != "none" else cluster_id


def _bounded_environment(response: Any, deadline: float) -> Dict[str, Any]:
    """The member's environment, read under a size and a wall-clock cap."""
    chunks: List[bytes] = []
    size = 0
    try:
        for chunk in response.iter_content(8192):
            size += len(chunk)
            if size > MEMBER_BODY_LIMIT:
                raise ValueError("answer larger than the member read limit")
            if time.monotonic() > deadline:
                raise TimeoutError("answer slower than the member read deadline")
            chunks.append(chunk)
    finally:
        response.close()
    body = json.loads(b"".join(chunks) or b"{}")
    metadata = body.get("metadata") if isinstance(body, dict) else None
    environment = metadata.get("environment") if isinstance(metadata, dict) else None
    return environment if isinstance(environment, dict) else {}


def _read_member(client: Any, member: str, deadline: float) -> MemberKernel:
    try:
        response = client.api.get(params={"target": member}, timeout=MEMBER_READ_TIMEOUT, stream=True)
        environment = _bounded_environment(response, deadline)
    except Exception as exc:  # noqa: BLE001 - recorded on the member, the cluster still answers
        kind, reason = classify(exc)
        if isinstance(exc, (ValueError, TimeoutError)):
            kind, reason = "error", str(exc)
        return MemberKernel(member=member, error=f"{kind}: {reason}")
    answered_as = _text(environment.get("server_name")) or ""
    if answered_as != member:
        # important: a daemon that ignores `target` answers as the endpoint member, which
        # would copy one kernel onto every member and report a half-patched cluster as patched
        return MemberKernel(member=member, error=f"mismatch: answered as {answered_as[:64] or 'unknown'}")
    return _member_from(member, environment)


def _member_rows(members_body: Any) -> List[Tuple[str, str, str]]:
    rows = members_body.get("metadata") if isinstance(members_body, dict) else None
    out: List[Tuple[str, str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        name = _text(row.get("server_name")) if isinstance(row, dict) else None
        if name:
            out.append((name, _text(row.get("status")) or "", _text(row.get("message")) or ""))
    return out


def _read_members(client: Any, listed: List[Tuple[str, str, str]], endpoint: str,
                  environment: Dict[str, Any]) -> List[MemberKernel]:
    """Each listed member's kernel, the endpoint's from host_info, the rest by `?target`."""
    members: List[MemberKernel] = []
    to_read: List[Tuple[int, str]] = []
    for name, status, message in listed[:MEMBER_CAP]:
        if name == endpoint:
            members.append(_member_from(name, environment))
        elif status.lower() != "online":
            members.append(MemberKernel(member=name, error=f"offline: {(message or status)[:120]}"))
        else:
            members.append(MemberKernel(member=name))
            to_read.append((len(members) - 1, name))
    for name, _status, _message in listed[MEMBER_CAP:]:
        members.append(MemberKernel(member=name, error=f"not read: more than {MEMBER_CAP} members"))
    if not to_read:
        return members

    deadline = time.monotonic() + MEMBER_PHASE_SECONDS
    # note: a pool per cluster read, not a shared one, so one slow cluster's members cannot queue
    # ahead of every other cluster's. shutdown(wait=False) leaves stragglers to their socket timeout.
    pool = ThreadPoolExecutor(max_workers=MEMBER_READS_PER_CLUSTER, thread_name_prefix="host_kernel_member")
    try:
        futures = {
            pool.submit(contextvars.copy_context().run, _read_member, client, name, deadline): index
            for index, name in to_read
        }
        done, pending = wait(futures, timeout=MEMBER_PHASE_SECONDS)
        for future in done:
            members[futures[future]] = future.result()
        for future in pending:
            future.cancel()
            name = members[futures[future]].member
            members[futures[future]] = MemberKernel(
                member=name, error=f"unreachable: did not answer within {int(MEMBER_PHASE_SECONDS)}s"
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return members


def read_cluster_kernels(lxd_service: Any, cluster_id: str, cache: Any) -> ClusterKernels:
    """One cluster's members and their kernels. Blocking; runs in a worker thread."""
    client = connect_or_get_client(lxd_service, cluster_id)
    if client is None:
        raise LXDConnectionError(f"Could not connect to {cluster_id}")
    host_info = client.host_info if isinstance(client.host_info, dict) else {}
    environment = host_info.get("environment") if isinstance(host_info.get("environment"), dict) else {}
    endpoint = _endpoint_name(cluster_id, environment)
    collected_at = datetime.now(timezone.utc).isoformat()

    if environment.get("server_clustered") is not True:
        result = ClusterKernels(cluster_id, False, True, [_member_from(endpoint, environment)], collected_at)
    else:
        body = None if members_read_failed(cache, cluster_id) else read_members_list(cache, client, cluster_id)
        listed = _member_rows(body) if body is not None else []
        if not listed:
            result = ClusterKernels(cluster_id, True, False, [_member_from(endpoint, environment)], collected_at)
        else:
            result = ClusterKernels(cluster_id, True, True, _read_members(client, listed, endpoint, environment), collected_at)

    try:
        ttl = HOST_KERNELS_TTL if result.complete else HOST_KERNELS_DEGRADED_TTL
        cache.set("api_responses", _cache_key(cluster_id), asdict(result), ttl=ttl)
    except Exception as exc:  # noqa: BLE001 - an uncached read is still a correct answer
        logger.debug("Could not cache host kernels for %s: %s", cluster_id, exc)
    return result


def _from_cache(cache: Any, cluster_id: str) -> Optional[ClusterKernels]:
    try:
        body = cache.get("api_responses", _cache_key(cluster_id))
        if not isinstance(body, dict) or body.get("cluster_id") != cluster_id:
            return None
        members = [_member_record(m) for m in body.get("members") or [] if isinstance(m, dict)]
        return ClusterKernels(
            cluster_id, body.get("clustered") is True, body.get("members_listed") is not False,
            members, str(body.get("collected_at") or ""),
        )
    except Exception:  # noqa: BLE001 - a malformed entry is a cache miss
        return None


def cached_member_versions(cache: Any, cluster_id: str) -> Optional[List[Tuple[str, str]]]:
    """(member, server_version) from the last complete-enough read, or None when nothing is cached.

    Cache only, never a read: GET /nodes asks this on every poll, and the kernel advisories keep
    the entry warm. A member without a version (an older entry, a failed read) is left out.
    """
    cached = _from_cache(cache, cluster_id)
    if cached is None:
        return None
    return [(m.member, m.server_version) for m in cached.members if m.server_version and m.error is None]


async def fleet_kernels(
    lxd_service: Any, cluster_ids: Sequence[str], cache: Any, *, fresh: bool, wall: float
) -> FleetKernels:
    """Every cluster's kernels, from cache unless `fresh`, read in parallel and bounded."""

    async def one(cluster_id: str) -> Any:
        cached = await asyncio.to_thread(_from_cache, cache, cluster_id)
        if cached is not None and not fresh:
            return cached
        try:
            return await run_blocking(
                read_cluster_kernels, lxd_service, cluster_id, cache,
                timeout=wall, label=f"host_kernels[{cluster_id}]", executor=_cluster_executor,
                max_in_flight=8, concurrency_key=cluster_id, max_in_flight_per_key=1,
            )
        except BlockingPoolFull as exc:
            # note: a read for this cluster is already running; its result lands in the cache
            return cached if cached is not None else exc

    results = await asyncio.gather(*(one(cid) for cid in cluster_ids), return_exceptions=True)
    fleet = FleetKernels()
    for cluster_id, result in zip(cluster_ids, results):
        if isinstance(result, ClusterKernels):
            fleet.clusters.append(result)
            continue
        if isinstance(result, BlockingTimeout):
            reason = f"unreachable: did not answer within {int(wall)}s; it keeps reading in the background"
        elif isinstance(result, BlockingPoolFull):
            reason = "unreachable: still being read by an earlier request"
        elif isinstance(result, BaseException):
            kind, text = classify(result)
            reason = f"{kind}: {text}"
        else:
            reason = f"error: unexpected {type(result).__name__}"
        fleet.unreachable_nodes.append(cluster_id)
        fleet.node_errors[cluster_id] = reason
        logger.info("Host kernels for %s not read (%s)", cluster_id, reason)
    return fleet
