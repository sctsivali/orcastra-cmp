"""Moving a workload between projects, cluster members and clusters.

Three transports, chosen by a preflight rather than by the caller, because the caller cannot
know which one a given pair of endpoints supports:

* ``copy``      same cluster, LXD copies internally. No data leaves the host.
* ``migration`` different clusters, LXD's own pull migration. The target opens a connection to
                the source and pulls, so the bytes never touch the control plane.
* ``stream``    different clusters that cannot reach each other. Orcastra exports a backup on
                the source and feeds it to the target's import in bounded chunks.

`migration` is preferred over `stream` wherever it works, and the reason is not only speed. A
pull migration is rsync-based and reports progress natively; the stream is one unresumable HTTP
body through a control plane with 24 GB of free disk, so a failure at 90% has nothing to resume
from. `stream` exists because a topology where the two hypervisors cannot see each other is
real, not because it is a reasonable default.

important: the migration secrets LXD mints are per-operation and short lived. This is why the
feature does not require the two clusters to trust each other's certificates permanently, which
would be a standing security relationship between two customers' infrastructure.
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

# The streaming fallback holds a worker for the whole transfer and buffers in the control plane,
# so it is capped. A workload above this is told to fix the network path between the clusters and
# use the migration transport rather than being quietly pushed down the worse route.
STREAM_MAX_BYTES = 20 * 1024 * 1024 * 1024

# A transfer is not a request. These bound the socket, not the operation, and are deliberately
# far above the pooled client's (3, 8) because a multi-gigabyte read legitimately takes minutes.
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

    The destination hypervisor dereferences this URL from its own network. Trusting an absolute
    URL returned by a compromised or confused daemon would therefore turn migration into a
    hypervisor-side SSRF primitive. Only the connected source origin and the exact LXD operation
    path are accepted; query strings, fragments and user info have no valid migration meaning.
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
    """Bytes the instance's root disk currently uses, when the driver reports it.

    Used to refuse an oversized streaming transfer up front and to show the operator what the
    move will actually cost, rather than discovering it when the pool fills.
    """
    try:
        state = _metadata(
            client.api.instances[safe_segment(instance_name)].state.get(params=_project_params(project))
        ) or {}
        disks = state.get("disk") or {}
        total = sum(int(d.get("usage") or 0) for d in disks.values() if isinstance(d, dict))
        return total or None
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
    """Architectures the host can actually run.

    An instance cannot be copied to a host that cannot execute its binaries, and LXD refuses the
    attempt rather than translating anything. This fleet makes the case concrete: nuc2 is x86_64
    and pandora is aarch64, so offering one as a destination for the other would be an invitation
    to a failure the operator could not have predicted.
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
    """The transport to attempt first for a pair of clusters.

    Cross-cluster always attempts migration first. Whether the target can actually reach the
    source is not knowable in advance, so it is discovered by attempting and falling back, not
    by a probe that would only pretend to answer.
    """
    return TRANSPORT_COPY if source_cluster == target_cluster else TRANSPORT_MIGRATION


def is_unreachable_error(exc: Exception) -> bool:
    """Whether a failed migration means the target could not open the source.

    There is no LXD call that answers "can you reach this host", so reachability is not probed:
    it is learned from the attempt.

    note: this used to select a streaming fallback. That transport is no longer reachable, so the
    classification now only shapes the message: a failure on this link is the one an operator
    cannot diagnose from the raw text, because it is the link the control plane never uses itself.
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

    important: a copy must carry these explicitly rather than leaving LXD to infer them. A
    destination project with `limits.cpu` or `limits.disk` set, which is what every customer
    project here has, refuses an instance that does not declare its own, and does so during the
    pre-flight quota check before it has resolved anything from the source. The refusal reads as
    "instance has no limits.cpu config", which points at the copy rather than at the project.

    `volatile.*` is dropped deliberately: it holds per-instance identity such as generated MAC
    addresses, and reproducing it would put two instances with the same hardware identity on the
    same network.
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
) -> Optional[str]:
    """Copy an instance inside one cluster.

    Copy, never move: the source survives, so a botched destination is recoverable and the
    operator decides when to remove the original. A move is a different LXD call shape and is
    deliberately not offered rather than guessed at.

    LXD does this internally, so nothing crosses the network twice and no tarball exists. The
    destination project is a query parameter and the source project travels inside `source`,
    which is what the `container_copy_project` extension added.
    """
    definition = read_instance_definition(client, source_name, source_project)
    devices = _map_root_pool(definition["devices"], target_pool)

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

    # important: LXD answers with a RELATIVE path, and this URL is consumed by the TARGET host,
    # not by us. Handed over as-is the target has no host to resolve it against, accepts the
    # request, and the migration then quietly never happens: the operation is created, no bytes
    # move, and the instance simply never appears. Proven between nuc2 and nuc1, where the copy
    # was accepted with a 202 and seven minutes later the destination was still empty.
    #
    # note: this is the address the dashboard reaches the source on. A topology where the target
    # must use a different address than the control plane does would need that second address
    # recorded in the registry, which nothing does today.
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

    This is a real artifact occupying real pool space on someone's cluster. Whoever calls it owns
    deleting it on every path out, including the one where this process dies.
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
    adapter's 8 second read, which a multi-gigabyte download would otherwise trip immediately.
    The adapter only fills in a default when the caller supplies none, so this widens one call
    rather than weakening every LXD read in the process.
    """
    return source_client.api.instances[safe_segment(instance_name)].backups[safe_segment(backup_name)].export.get(
        params=_project_params(project),
        stream=True,
        timeout=(TRANSFER_CONNECT_TIMEOUT, TRANSFER_READ_TIMEOUT),
    )


def non_retrying_session(client):
    """A session for the import POST that will not repeat it.

    important: the pooled LXD session mounts a Retry that includes POST with a 5xx
    status_forcelist. Retrying an instance import means creating the instance twice, which for a
    multi-gigabyte transfer leaves two half-built instances and a filled pool. This borrows the
    pooled session's TLS material and connection pool but strips the retry policy.
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
