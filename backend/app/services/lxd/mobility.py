"""Moving a workload between projects, cluster members and clusters.

`choose_transport` picks the transport from the cluster pair:

* ``copy``      same cluster, LXD copies internally. No data leaves the host.
* ``migration`` different clusters, LXD's own pull migration. The target opens a connection to
                the source and pulls, so the bytes never touch the control plane.

No caller selects ``stream`` (the backup export and import helpers at the end): it would be one
unresumable HTTP body through the control plane's disk, while a pull migration reports progress.

important: LXD's migration secrets are per operation and short lived, so the two clusters never
trust each other's certificates permanently, which would be a standing security relationship
between two customers' infrastructure.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import SplitResult, urlsplit

from app.core.lxd_path import safe_segment

logger = logging.getLogger(__name__)

TRANSPORT_COPY = "copy"
TRANSPORT_MIGRATION = "migration"
TRANSPORT_STREAM = "stream"

# Chunk size for the streaming fallback. Large enough that the per-chunk overhead is noise,
# small enough that a handful of concurrent transfers cannot exhaust the container's memory.
STREAM_CHUNK_BYTES = 4 * 1024 * 1024

# Size cap for the streaming fallback, which holds a worker for the whole transfer and buffers
# in the control plane.
STREAM_MAX_BYTES = 20 * 1024 * 1024 * 1024

# Socket bounds for a transfer; they do not bound the whole operation. Far above the pooled
# client's (3, 8) because a multi-gigabyte read takes minutes.
TRANSFER_CONNECT_TIMEOUT = 15
TRANSFER_READ_TIMEOUT = 3600


class MobilityError(RuntimeError):
    """A move could not be started, with an operator-facing reason."""


class MobilityUnreachable(MobilityError):
    """The target cannot reach the source, so pull migration is unavailable."""


_OPERATION_PATH_RE = re.compile(r"^/1\.0/operations/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _effective_port(parsed: SplitResult) -> Optional[int]:
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme.lower() == "https":
        return 443
    if parsed.scheme.lower() == "http":
        return 80
    return None


def validated_operation_url(client, operation_url: str) -> str:
    """Bind a daemon-provided operation URL to the registered source endpoint.

    important: the destination hypervisor dereferences this URL from its own network, so an
    absolute URL from a compromised or confused daemon would make migration a hypervisor-side
    SSRF primitive. Only the connected source origin and the exact LXD operation path are
    accepted; query strings, fragments and user info have no valid migration meaning.
    """
    endpoint = urlsplit(api_base(client))
    raw = (operation_url or "").strip()
    if raw.startswith("/"):
        raw = f"{endpoint.scheme}://{endpoint.netloc}{raw}"
    parsed = urlsplit(raw)

    same_origin = (
        parsed.scheme.lower() in {"http", "https"}
        and parsed.scheme.lower() == endpoint.scheme.lower()
        and (parsed.hostname or "").lower() == (endpoint.hostname or "").lower()
        and _effective_port(parsed) == _effective_port(endpoint)
        and parsed.username is None
        and parsed.password is None
    )
    if (
        not same_origin
        or parsed.query
        or parsed.fragment
        or not _OPERATION_PATH_RE.fullmatch(parsed.path)
    ):
        raise MobilityError(
            "The source cluster returned an operation URL outside its registered endpoint."
        )
    return raw


def _project_params(project: Optional[str], **extra: Any) -> Dict[str, Any]:
    params: Dict[str, Any] = {"project": project or "default"}
    params.update({k: v for k, v in extra.items() if v is not None})
    return params


def _metadata(response: Any) -> Any:
    body = response.json()
    return body.get("metadata") if isinstance(body, dict) else None


def _operation_url(response: Any) -> Optional[str]:
    body = response.json()
    if not isinstance(body, dict):
        return None
    operation = body.get("operation")
    return operation if isinstance(operation, str) and operation else None


# ── preflight ──────────────────────────────────────────────────────────────────────────────


def instance_footprint(client, instance_name: str, project: str) -> Optional[int]:
    """Bytes the instance's disks currently use, when the driver reports it.

    Lets the preflight size a move against the target pool before it starts, rather than
    discovering the cost when the pool fills.
    """
    try:
        state = _metadata(
            client.api.instances[safe_segment(instance_name)].state.get(params=_project_params(project))
        ) or {}
        disks = state.get("disk") or {}
        # important: a reported 0 is a measurement. A volume never written past its image (an
        # instance created and never started) holds no bytes of its own, and the preflight tells
        # that case apart from no report at all (None).
        reported = [
            d.get("usage") for d in disks.values() if isinstance(d, dict) and d.get("usage") is not None
        ]
        if not reported:
            return None
        return sum(int(value) for value in reported)
    except Exception as exc:  # noqa: BLE001 - a missing figure is not a failure to move
        logger.debug("Could not read footprint for %s: %s", instance_name, exc)
        return None


def pool_headroom(client, pool: str) -> Optional[Dict[str, int]]:
    try:
        resources = _metadata(client.api.storage_pools[safe_segment(pool)].resources.get()) or {}
        space = resources.get("space") or {}
        total = int(space.get("total") or 0)
        used = int(space.get("used") or 0)
        if not total:
            return None
        return {"total": total, "used": used, "free": max(0, total - used)}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read headroom for pool %s: %s", pool, exc)
        return None


def host_architectures(client) -> List[str]:
    """Architectures the host can run.

    LXD refuses to copy an instance to a host that cannot execute its binaries and translates
    nothing, so a mismatch is reported before the move.
    """
    env = (client.host_info or {}).get("environment") or {}
    arches = env.get("architectures") or []
    return [a for a in arches if isinstance(a, str)]


def architecture_compatible(instance_architecture: Optional[str], target_arches: List[str]) -> bool:
    """Whether a target can run this instance.

    Unknown architecture on either side is treated as compatible: refusing on missing
    information would block a legitimate move because a read failed, and LXD still gets the
    final say when the copy is attempted.
    """
    if not instance_architecture or not target_arches:
        return True
    return instance_architecture in target_arches


def instance_architecture(client, instance_name: str, project: str) -> Optional[str]:
    try:
        meta = _metadata(
            client.api.instances[safe_segment(instance_name)].get(params=_project_params(project))
        ) or {}
        arch = meta.get("architecture")
        return arch if isinstance(arch, str) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read architecture for %s: %s", instance_name, exc)
        return None


def choose_transport(source_cluster: str, target_cluster: str) -> str:
    """The transport for a pair of clusters: copy within one cluster, pull migration across two.

    Whether the target can reach the source cannot be probed in advance, so a cross-cluster move
    learns it from the attempt.
    """
    return TRANSPORT_COPY if source_cluster == target_cluster else TRANSPORT_MIGRATION


def is_unreachable_error(exc: Exception) -> bool:
    """Whether a failed migration means the target could not open the source.

    No LXD call answers "can you reach this host", so reachability is learned from the attempt.
    A failure on this link is hard to diagnose from the raw text, because the control plane
    never uses the link itself.
    """
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "connection refused",
            "no route to host",
            "network is unreachable",
            "timeout",
            "timed out",
            "dial tcp",
            "connection reset",
            "certificate",
            "failed to connect",
        )
    )


# ── same cluster ───────────────────────────────────────────────────────────────────────────


def read_instance_definition(client, instance_name: str, project: str) -> Dict[str, Any]:
    """The source instance's config, devices and profiles, ready to be reproduced elsewhere.

    important: a copy carries these explicitly. A destination project with `limits.cpu` or
    `limits.disk` set refuses an instance that does not declare its own, during the quota check
    that runs before anything is resolved from the source ("instance has no limits.cpu config").

    `volatile.*` is dropped: it holds per-instance identity such as generated MAC addresses, and
    reproducing it would put two instances with the same hardware identity on one network.
    """
    meta = _metadata(client.api.instances[safe_segment(instance_name)].get(params=_project_params(project))) or {}
    instance_type = meta.get("type")
    if instance_type not in {"container", "virtual-machine"}:
        raise MobilityError("The source instance returned an unsupported type.")
    config = {
        key: value
        for key, value in (meta.get("config") or {}).items()
        if not key.startswith("volatile.")
    }
    return {
        "type": instance_type,
        "config": config,
        "devices": meta.get("devices") or {},
        "profiles": meta.get("profiles") or ["default"],
        "architecture": meta.get("architecture"),
    }


def _map_root_pool(devices: Dict[str, Any], target_pool: Optional[str]) -> Dict[str, Any]:
    """Apply a pool mapping to the effective root disk without guessing its device key."""
    mapped = dict(devices)
    if not target_pool:
        return mapped
    root_key = next(
        (
            str(name)
            for name, device in mapped.items()
            if isinstance(device, dict)
            and device.get("type") == "disk"
            and device.get("path") == "/"
        ),
        None,
    )
    root_key = root_key or "root"
    root = dict(mapped.get(root_key) or {"path": "/", "type": "disk"})
    root["pool"] = target_pool
    mapped[root_key] = root
    return mapped


def copy_within_cluster(
    client,
    *,
    source_name: str,
    source_project: str,
    target_name: str,
    target_project: str,
    target_member: Optional[str] = None,
    target_pool: Optional[str] = None,
    definition: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Copy an instance inside one cluster.

    The source survives, so a failed destination is recoverable and the operator decides when to
    remove the original. LXD copies internally, so nothing crosses the network twice and no
    tarball exists. The destination project is a query parameter and the source project travels
    inside `source` (the `container_copy_project` extension).
    """
    if definition is None:
        definition = read_instance_definition(client, source_name, source_project)
        devices = _map_root_pool(definition["devices"], target_pool)
    else:
        # A migration contract already decided every device, including the root pool.
        devices = dict(definition.get("devices") or {})

    body: Dict[str, Any] = {
        "name": target_name,
        "type": definition["type"],
        "source": {
            "type": "copy",
            "source": source_name,
            "project": source_project or "default",
        },
        "config": definition["config"],
        "devices": devices,
        "profiles": definition["profiles"],
    }
    # important: LXD 5.21 rejects an explicit-config copy without an architecture when the
    # source lives on the member answering the request ("Architecture is not supported").
    if definition.get("architecture"):
        body["architecture"] = definition["architecture"]

    params = _project_params(target_project, target=target_member)
    response = client.api.instances.post(json=body, params=params)
    return _operation_url(response)


# ── cross cluster: native pull migration ───────────────────────────────────────────────────


def begin_source_migration(
    source_client,
    *,
    instance_name: str,
    project: str,
    instance_only: bool = False,
) -> Tuple[str, Dict[str, str]]:
    """Ask the source to stand up a migration and hand back its operation URL and secrets.

    The secrets are one-time and scoped to this operation, which is what lets the target pull
    without the two servers trusting each other permanently.
    """
    response = source_client.api.instances[safe_segment(instance_name)].post(
        json={"migration": True, "live": False, "instance_only": bool(instance_only)},
        params=_project_params(project),
    )
    operation_url = _operation_url(response)
    metadata = _metadata(response) or {}
    secrets = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else metadata
    secrets = {k: v for k, v in (secrets or {}).items() if isinstance(v, str)}
    if not operation_url or not secrets:
        raise MobilityError(
            "The source cluster did not return a migration operation. Its LXD may be too old "
            "or the instance may be in a state that cannot be migrated."
        )

    # important: LXD answers with a relative path, and the target host consumes this URL. Passed
    # as-is, the target has no host to resolve it against: it accepts the request with a 202, no
    # bytes move, and the instance never appears. validated_operation_url makes it absolute
    # against the registered source endpoint.
    #
    # That is the address the dashboard reaches the source on. A topology where the target needs
    # a different address would have to record that address in the registry.
    operation_url = validated_operation_url(source_client, operation_url)
    return operation_url, secrets


def accept_migration_on_target(
    target_client,
    *,
    target_name: str,
    target_project: str,
    source_operation_url: str,
    source_certificate: str,
    secrets: Dict[str, str],
    target_member: Optional[str] = None,
    target_pool: Optional[str] = None,
    definition: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Tell the target to pull the instance from the source.

    `source_operation_url` and `source_certificate` come from the registry and the source's own
    response. Neither is ever taken from the request body.
    """
    body: Dict[str, Any] = {
        "name": target_name,
        "source": {
            "type": "migration",
            "mode": "pull",
            "operation": source_operation_url,
            "certificate": source_certificate,
            "secrets": secrets,
        },
    }
    # Same reason as the copy path: a destination project carrying limits refuses an instance
    # that does not declare its own, during a check that runs before the transfer begins.
    if definition:
        instance_type = definition.get("type")
        if instance_type not in {"container", "virtual-machine"}:
            raise MobilityError("The source instance definition has an unsupported type.")
        body["type"] = instance_type
        body["config"] = definition.get("config") or {}
        body["profiles"] = definition.get("profiles") or ["default"]
        # A migration target builds the instance record before any bytes move, and it does not
        # infer the architecture from the source the way a same-cluster copy does. Omitting it
        # is refused outright with `Architecture is not supported: ""`.
        if definition.get("architecture"):
            body["architecture"] = definition["architecture"]
        devices = _map_root_pool(definition.get("devices") or {}, target_pool)
        if devices:
            body["devices"] = devices
    else:
        raise MobilityError("A validated source instance definition is required for migration.")

    params = _project_params(target_project, target=target_member)
    response = target_client.api.instances.post(json=body, params=params)
    return _operation_url(response)


def server_certificate(client) -> str:
    """The source server's own certificate, for the target to pin the pull against."""
    info = client.host_info or {}
    cert = (info.get("environment") or {}).get("certificate")
    if not cert:
        raise MobilityError("Could not read the source cluster's certificate.")
    return cert


# ── cross cluster: streaming fallback ──────────────────────────────────────────────────────


def create_backup(
    source_client,
    *,
    instance_name: str,
    project: str,
    backup_name: str,
    instance_only: bool = True,
) -> Optional[str]:
    """Create a backup object on the source.

    The backup occupies pool space on the source cluster. The caller must delete it on every path
    out, including a crash of this process.
    """
    response = source_client.api.instances[safe_segment(instance_name)].backups.post(
        json={
            "name": backup_name,
            "instance_only": bool(instance_only),
            "optimized_storage": False,
            "compression_algorithm": "gzip",
        },
        params=_project_params(project),
    )
    return _operation_url(response)


def delete_backup(source_client, *, instance_name: str, project: str, backup_name: str) -> None:
    """Best-effort teardown. Never raises: it runs on the failure path too."""
    try:
        source_client.api.instances[safe_segment(instance_name)].backups[safe_segment(backup_name)].delete(
            params=_project_params(project)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not delete backup %s on %s/%s: %s", backup_name, project, instance_name, exc
        )


def open_backup_stream(source_client, *, instance_name: str, project: str, backup_name: str):
    """A streaming response for the backup tarball.

    `stream=True` keeps the body off the heap, and the explicit timeout overrides the pooled
    adapter's 8 second read, which a multi-gigabyte download would trip. The adapter fills in a
    default only when the caller supplies none, so this widens one call without weakening every
    LXD read in the process.
    """
    return source_client.api.instances[safe_segment(instance_name)].backups[safe_segment(backup_name)].export.get(
        params=_project_params(project),
        stream=True,
        timeout=(TRANSFER_CONNECT_TIMEOUT, TRANSFER_READ_TIMEOUT),
    )


def non_retrying_session(client):
    """A session for the import POST that will not repeat it.

    important: the pooled LXD session mounts a Retry that includes POST with a 5xx
    status_forcelist. Retrying an instance import creates the instance twice, which for a
    multi-gigabyte transfer leaves two half-built instances and a filled pool. This session
    borrows the pooled session's TLS material and response hooks but has no retry policy.
    """
    import requests
    from .connection import FingerprintHTTPAdapter

    pooled = client.api.session
    session = requests.Session()
    session.cert = pooled.cert
    session.verify = pooled.verify
    session.headers.update(pooled.headers)
    # Preserve the pooled client's response guards. In particular, a 307/308 must be rejected
    # before requests can replay an import body or migration secret to another origin.
    pooled_hooks = getattr(pooled, "hooks", {})
    session.hooks["response"] = list(pooled_hooks.get("response", []))
    pooled_adapter = None
    if hasattr(pooled, "get_adapter"):
        pooled_adapter = pooled.get_adapter(api_base(client))
        if not getattr(pooled_adapter, "server_fingerprint", None):
            raise MobilityError(
                "The source connection has no pinned certificate fingerprint."
            )
    adapter = FingerprintHTTPAdapter(
        max_retries=0,
        server_fingerprint=getattr(pooled_adapter, "server_fingerprint", None),
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def api_base(client) -> str:
    """The client's `/1.0` endpoint, for a call that has to bypass pylxd's path builder."""
    return client.api._api_endpoint


def iter_chunks(response, chunk_bytes: int = STREAM_CHUNK_BYTES):
    """Bounded-memory iteration over the export body."""
    return response.iter_content(chunk_size=chunk_bytes)
