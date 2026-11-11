"""The LXD service: one interface for instance, project, image and resource operations."""

import asyncio
import logging
import time
from typing import Callable, List, Tuple, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import (
    VMInfo,
    ImageInfo,
    NodeResources,
    NodeStatus,
    InstanceMetrics,
)
from app.core.blocking import run_blocking
from .availability import require_client
from .connection import LXDConnectionManager
from .gpu import GpuInventory, build_gpu_inventory
from .lazy_client import connect_or_get_client
from .node_resources import resources_for_host
from .project_usage import ProjectUsage, read_project_usage
from .freshness import read_resources
from .resources import ResourceManager
from .instance_metrics import read_instance_metrics
from .instance import FleetInventory, InstanceManager
from .projects import ProjectManager
from .images import ImageManager, ImageStoreListing
from app.core.type_utils import normalize_node_type, resolve_clustered
from .exceptions import LXDConnectionError, LXDLocalStorageError
from app.services.lxd_cert_manager import LXDCertificateManager
from app.core.vault_client import VaultClient
from app.core.config import get_settings
from app.services.lxd.project_scope import list_instance_records

logger = logging.getLogger(__name__)

_blocking_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="lxd_blocking")

# important: instance metrics get their own pool. The node-status sweep submits to
# _blocking_executor through a bare run_in_executor that run_blocking cannot count, so in a
# shared pool a sweep could starve the five-second poll until it timed out on a healthy
# instance. The per-cluster cap keeps one slow cluster from holding the whole pool.
_instance_metrics_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="instance_metrics")
_INSTANCE_METRICS_TIMEOUT_SECONDS = 12.0
_INSTANCE_METRICS_MAX_IN_FLIGHT = 10
_INSTANCE_METRICS_MAX_IN_FLIGHT_PER_CLUSTER = 3

# Failed clusters are skipped through the shared breaker (app.services.lxd.health), so status
# checks, instance and project fan-outs and warning sweeps all skip the same dead cluster.


class LXDService:
    """LXD operations across every registered cluster: instances, projects, images, resources."""
    
    def __init__(self):
        self.connection_manager = LXDConnectionManager()
        self.resource_manager = ResourceManager()
        settings = get_settings()
        vault_client = VaultClient(settings.vault_addr, settings.vault_token)
        self.cert_manager = LXDCertificateManager(vault_client)
        self.instance_manager = InstanceManager(self.connection_manager, self.resource_manager, self.cert_manager)
        self.project_manager = ProjectManager(self.connection_manager, self.cert_manager)
        self.image_manager = ImageManager(self.connection_manager, self.cert_manager)
        
        # Seed the connection manager's whitelist from Vault, so its get_node_names lists only
        # registered clusters.
        try:
            registered = self.cert_manager.list_clusters()
            self.connection_manager.sync_registered_clusters(registered)
        except Exception as e:
            logger.warning(f"Could not sync registered clusters at init: {e}")
    
    def ensure_connection(
        self,
        cluster_id: str,
        *,
        expected_server_fingerprint: str | None = None,
        force: bool = False,
    ) -> bool:
        """Connect a cluster from Vault unless this worker already holds a client.

        False for a cluster outside the caller's org scope, one the breaker holds, or one that
        did not connect. `force` dials past the breaker without lifting it first, for the
        leader's dead-cluster probe (breaker_sync): every other reader keeps skipping the
        cluster while it runs.
        """
        # Never connect a cluster outside the caller's org scope.
        from app.core.org_scope import get_current_allowed_clusters
        from app.services.lxd import health
        from app.services.lxd.exceptions import (
            LXDAuthorizationError,
            LXDTransportPinError,
        )
        allowed = get_current_allowed_clusters()
        if allowed is not None and cluster_id not in allowed:
            return False

        client = (
            self.connection_manager.get_client(cluster_id)
            if expected_server_fingerprint is None
            else self.connection_manager.get_client(
                cluster_id,
                expected_server_fingerprint=expected_server_fingerprint,
            )
        )
        if client:
            return True

        # important: skip a cluster the breaker holds. This is the lazy-connect path of the
        # project and instance fan-outs; without the check each one pays the dead cluster's
        # connect timeout again.
        if not force and health.is_tripped(cluster_id):
            return False

        try:
            # The /metadata read, not get_cluster_info: that helper also reads <cluster>/secrets
            # for a has_trust_password flag this path never uses, and only /metadata is on the
            # Vault read cache allowlist (core/vault_client.py CACHEABLE_PATH_SUFFIXES).
            # Certificates are never cached.
            metadata = self.cert_manager.vault.vault_read(
                f"{get_settings().vault_clusters_path}/{cluster_id}/metadata"
            )
            cert_data = self.cert_manager.get_cluster_certificate(cluster_id)
            
            connect_kwargs = {
                "cluster_id": cluster_id,
                "endpoint": metadata["endpoint"],
                "cert_content": cert_data["client.crt"],
                "key_content": cert_data["client.key"],
                "verify": False,
            }
            if expected_server_fingerprint is not None:
                connect_kwargs["expected_server_fingerprint"] = (
                    expected_server_fingerprint
                )
            if force:
                connect_kwargs["force"] = True
            connected = self.connection_manager.connect_with_vault_cert(
                **connect_kwargs
            )
            # None means the breaker refused the connect: it must neither clear the breaker nor
            # count as connected.
            if connected is None:
                return False
            logger.info(f"Lazy-connected to cluster: {cluster_id}")
            health.clear(cluster_id)
            return True
        except LXDTransportPinError:
            raise
        except LXDAuthorizationError as e:
            # important: the host answered and refused our certificate. Keep this above the broad
            # handler, which records a generic failure: that would overwrite the "unauthorized"
            # kind connect_with_vault_cert recorded and show a reachable cluster as Offline.
            logger.warning(f"Cluster {cluster_id} refused our certificate: {e}")
            health.record_failure(cluster_id, str(e), kind=health.KIND_UNAUTHORIZED)
            return False
        except LXDLocalStorageError:
            # important: keep this narrow and above the broad handler. The backend's own disk
            # failed and the peer was never contacted; recording a failure would mark every
            # cluster offline at once, and the breaker would then suppress the retries.
            raise
        except Exception as e:
            from app.core import vault_token

            if vault_token.store_failed(e, vault_only=True):
                # important: Vault could not serve this cluster's credentials (sealed, down, or
                # refusing the dashboard's token) and the cluster was never contacted. Recorded
                # as a cluster failure it would trip every cluster at once, and the breaker would
                # hold them down after Vault recovers. The Vault notice reports the outage.
                logger.warning(f"Cluster {cluster_id} credentials could not be read from Vault: {e}")
                return False
            # WARNING rather than ERROR: an offline cluster is expected, and the breaker
            # records it.
            logger.warning(f"Failed to connect cluster {cluster_id}: {e}")
            health.record_failure(cluster_id, str(e))
            return False
    
    def get_client_with_reconnect(self, cluster_id: str):
        """The pooled client, reconnecting through ensure_connection when there is none."""
        client = self.connection_manager.get_client(cluster_id)
        if client is None:
            logger.info(f"Connection to {cluster_id} is stale, attempting reconnect...")
            # ensure_connection stores the new client in connection_manager._clients.
            if self.ensure_connection(cluster_id):
                client = self.connection_manager.get_client(cluster_id)
                logger.info(f"Reconnected to {cluster_id} successfully")
            else:
                logger.warning(f"Reconnect failed for {cluster_id}")
                client = None
        return client
    
    # Node operations
    def get_node_names(self) -> List[str]:
        """Registered cluster names from Vault, filtered to the caller's org scope."""
        from app.core.org_scope import apply_cluster_filter
        return apply_cluster_filter(self.cert_manager.list_clusters())
    
    def check_nodes_status(self) -> List[NodeStatus]:
        """Status of every node, one at a time. Deprecated: use check_nodes_status_async."""
        statuses = []
        settings = get_settings()
        node_names = self.get_node_names()

        for node_name in node_names:
            status = self._check_single_node_status(node_name, settings)
            statuses.append(status)
        
        return statuses
    
    def check_node_status(self, node_name: str) -> NodeStatus:
        """Probe one node and return its status, resources included.

        For callers outside the fan-out that need one node rather than the whole fleet.
        """
        return self._check_single_node_status(node_name, get_settings())

    def _read_registry(self, node_name: str, settings) -> Dict[str, Any]:
        """What registration recorded about a cluster, none of which depends on it answering.

        Endpoint, display name, type and the clustered flag all come from Vault, so a cluster
        that does not answer is still named and typed on every picker and card, and the clusters
        table and the node card agree on whether an offline host is clustered.

        `type` is normalised only when one was recorded: normalize_node_type(None) answers "lxd",
        which would label every unknown cluster as LXD.
        """
        try:
            metadata = self.cert_manager.vault.vault_read(
                f"{settings.vault_clusters_path}/{node_name}/metadata"
            )
        except Exception:
            return {
                "endpoint": "unknown",
                "display_name": None,
                "node_type": None,
                "clustered": None,
            }
        registered_type = metadata.get("type")
        stored_clustered = metadata.get("clustered")
        return {
            "endpoint": metadata.get("endpoint", "unknown"),
            "display_name": metadata.get("display_name") or None,
            "node_type": normalize_node_type(registered_type) if registered_type else None,
            # A stored value is only ever written by check-auth, after a trusted answer, so an
            # absent key means unknown rather than standalone. Anything that is not a bool is
            # not a reading.
            "clustered": stored_clustered if isinstance(stored_clustered, bool) else None,
        }

    def _check_single_node_status(self, node_name: str, settings) -> NodeStatus:
        """Check status for a single node (internal helper)."""
        from app.services.lxd import fanout, health

        # important: read the registration before the breaker check, so a dead cluster still
        # reports its endpoint, name and type. The breaker exists to skip an 8s connect, not a
        # Vault read.
        registry = self._read_registry(node_name, settings)
        endpoint = registry["endpoint"]

        # Skip clusters the shared breaker marked as recently failed.
        if health.is_tripped(node_name):
            logger.debug(f"Skipping {node_name} - recently failed (cached)")
            # The breaker remembers WHY it tripped, so a cluster that answered and refused our
            # certificate keeps reading as untrusted instead of degrading to "offline" for the
            # 60s the breaker holds.
            return NodeStatus(
                name=node_name,
                endpoint=endpoint,
                display_name=registry["display_name"],
                node_type=registry["node_type"],
                clustered=registry["clustered"],
                connected=False,
                reachable=health.get_kind(node_name) == health.KIND_UNAUTHORIZED,
                error=health.get_reason(node_name) or "Node recently failed - retry later"
            )

        # A lapsed trip with no pooled client: this worker knows the cluster is down, and the
        # healer pays the redial rather than this read (see fanout.known_down).
        down = fanout.known_down(node_name, getattr(self, "connection_manager", None))
        if down is not None:
            return NodeStatus(
                name=node_name,
                endpoint=endpoint,
                display_name=registry["display_name"],
                node_type=registry["node_type"],
                clustered=registry["clustered"],
                connected=False,
                reachable=down == health.KIND_UNAUTHORIZED,
                error=fanout.down_text(down),
            )

        try:
            
            client = self.get_client_with_reconnect(node_name)
            if client:
                status = self.resource_manager.check_node_status(
                    client, node_name, endpoint, stored_clustered=registry["clustered"]
                )
                return status
            else:
                # important: record nothing here. get_client has already recorded why it
                # failed (an untrusted certificate records KIND_UNAUTHORIZED), and a missing
                # client is no evidence the cluster failed: this worker may never have connected
                # to it. Recording would trip the breaker for every reader. Only a path that
                # reached the peer records; this response just reports the cluster offline.
                kind = health.get_kind(node_name) or health.KIND_OFFLINE
                return NodeStatus(
                    name=node_name,
                    endpoint=endpoint,
                    display_name=registry["display_name"],
                    node_type=registry["node_type"],
                    clustered=registry["clustered"],
                    connected=False,
                    reachable=kind == health.KIND_UNAUTHORIZED,
                    error=health.get_reason(node_name) or "Client not connected"
                )
        except LXDLocalStorageError:
            # The backend's own disk failed (see ensure_connection), so nothing is recorded or
            # reported about the node.
            raise
        except Exception as e:
            logger.warning(f"Node {node_name} health check failed: {e}")
            kind = health.get_kind(node_name)
            if kind is None:
                health.record_failure(node_name, str(e))
                kind = health.KIND_OFFLINE
            return NodeStatus(
                name=node_name,
                endpoint=endpoint,
                display_name=registry["display_name"],
                node_type=registry["node_type"],
                connected=False,
                reachable=kind == health.KIND_UNAUTHORIZED,
                error=str(e)
            )
    
    async def check_nodes_status_async(self) -> List[NodeStatus]:
        """Check connection status for all nodes in parallel (async version)."""
        settings = get_settings()
        node_names = self.get_node_names()
        
        if not node_names:
            return []
        
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(
                _blocking_executor,
                self._check_single_node_status,
                node_name,
                settings
            )
            for node_name in node_names
        ]
        
        statuses = await asyncio.gather(*tasks, return_exceptions=True)
        
        # A check that raised still yields an offline status for its node.
        result = []
        for i, status in enumerate(statuses):
            if isinstance(status, Exception):
                logger.warning(f"Node check failed for {node_names[i]}: {status}")
                result.append(NodeStatus(
                    name=node_names[i],
                    endpoint="unknown",
                    connected=False,
                    reachable=False,
                    error=str(status)
                ))
            else:
                result.append(status)
        
        return result
    
    def get_node_resources(self, node_name: str) -> Dict[str, Any]:
        """Resources of one node, aggregated over its members when the host is clustered.

        require_client turns a breaker-tripped cluster into a 503 instead of a None dereference.
        """
        client = require_client(self.connection_manager.get_client(node_name), node_name)
        data = self.resource_manager.get_node_resources(client, node_name)
        # A clustered host's body is one member's. The project quota pre-flight and the server
        # resources route read this dict, so it carries the same cluster aggregate as the card,
        # built in one place (resources_for_host).
        if resolve_clustered(client=client):
            return resources_for_host(
                client, node_name, data, GpuInventory.empty(), True
            ).model_dump()
        return data

    def get_node_resources_detail(self, node_name: str) -> NodeResources:
        """Full NodeResources for one node, GPU inventory included.

        Backs GET /nodes/{node}/resources, so the GPU fields come from a real inventory read
        rather than their defaults.
        """
        client = require_client(connect_or_get_client(self, node_name), node_name)
        # One live read for both consumers: two would double the round trips and let the memory
        # figure and the GPU inventory disagree about the same host.
        live = read_resources(client, node_name)
        data = self.resource_manager.get_node_resources(client, node_name, resources=live)
        gpu = GpuInventory.empty()
        try:
            # recursion=1: build_gpu_inventory reads location, status and the expanded
            # config off each instance, which at recursion=0 is a request apiece.
            gpu = build_gpu_inventory(live.body, list_instance_records(client)[0])
        except Exception as e:
            logger.debug(f"Could not read GPU inventory for {node_name}: {e}")
        return resources_for_host(
            client, node_name, data, gpu, resolve_clustered(client=client)
        )

    def get_node_gpus(self, node_name: str) -> GpuInventory:
        """List the GPUs on a specific node, with their class and allocation state."""
        client = require_client(connect_or_get_client(self, node_name), node_name)
        return self.resource_manager.get_node_gpus(client, node_name)
    
    # Instance/VM operations
    async def list_all_vms(self) -> Tuple[List[VMInfo], List[str]]:
        """Every instance in every cluster and project, as (instances, connected clusters)."""
        return await self.instance_manager.list_all_vms()

    async def list_all_vms_with_failures(
        self,
    ) -> Tuple[List[VMInfo], List[str], List[str]]:
        """List instances and identify clusters whose project inventory was incomplete."""
        return await self.instance_manager.list_all_vms_with_failures()

    async def list_node_instances(self, node_name: str) -> List[VMInfo]:
        """One cluster's instances across every project, without a fleet fan-out."""
        return await self.instance_manager.list_node_instances(node_name)

    async def read_fleet_inventory(self) -> FleetInventory:
        """The monitoring sweep's fleet read; clusters that run out of time are listed."""
        return await self.instance_manager.read_fleet_inventory()

    async def list_vms_in_project_with_failures(
        self,
        node_name: str,
        project: str,
    ) -> Tuple[List[VMInfo], List[str], List[str]]:
        """List instances in one exact cluster/project and preserve inventory failure detail."""
        return await self.instance_manager.list_vms_in_project_with_failures(
            node_name,
            project,
        )
    
    async def get_vm(
        self,
        name: str,
        host: str,
        project: Optional[str] = None,
        project_filter: Optional[Callable[[str], bool]] = None,
    ) -> Optional[VMInfo]:
        """Get a specific VM by name and host, narrowed to a project when one is known."""
        if project_filter is None:
            return await self.instance_manager.get_vm(name, host, project)
        return await self.instance_manager.get_vm(
            name, host, project, project_filter=project_filter
        )
    
    async def start_vm(
        self,
        name: str,
        host: str,
        force: bool = False,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Start a VM (searches all projects)."""
        return await self.instance_manager.start_vm(name, host, force, project)
    
    async def stop_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        timeout: int = 30,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Stop a VM (searches all projects)."""
        return await self.instance_manager.stop_vm(name, host, force, timeout, project)
    
    async def restart_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        timeout: int = 30,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Restart a VM (searches all projects)."""
        return await self.instance_manager.restart_vm(name, host, force, timeout, project)
    
    async def delete_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        project: Optional[str] = None,
    ) -> str:
        """Delete a VM and return the project it was deleted from."""
        return await self.instance_manager.delete_vm(name, host, force, project)

    async def get_rebuild_preflight(self, name: str, host: str, project: Optional[str] = None):
        """Status + snapshots + config/attached-volumes snapshot for a rebuild."""
        return await self.instance_manager.get_rebuild_preflight(name, host, project)

    async def rebuild_instance(
        self,
        name: str,
        host: str,
        source: dict,
        project: Optional[str] = None,
        cloud_init: Optional[str] = None,
        wait_timeout: int = 1800,
    ) -> str:
        """Rebuild an instance's root disk from `source` (preserves identity/config/volumes)."""
        return await self.instance_manager.rebuild_instance(
            name, host, source, project, cloud_init, wait_timeout
        )

    async def create_vm(
        self,
        name: str,
        host: str,
        image: str,
        instance_type: str = "virtual-machine",
        profiles: Optional[List[str]] = None,
        config: Optional[Dict[str, str]] = None,
        cloud_init: Optional[str] = None,
        devices: Optional[Dict[str, Dict[str, str]]] = None,
        start_on_create: bool = True,
        project: str = "default",
    ) -> VMInfo:
        """Create a new VM (blocking). Stable contract for `POST /vms` (Orcahub)."""
        return await self.instance_manager.create_vm(
            name=name,
            host=host,
            image=image,
            instance_type=instance_type,
            profiles=profiles,
            config=config,
            cloud_init=cloud_init,
            devices=devices,
            start_on_create=start_on_create,
            project=project,
        )

    async def create_instance(
        self,
        name: str,
        host: str,
        image: Optional[str] = None,
        instance_type: str = "virtual-machine",
        profiles: Optional[List[str]] = None,
        config: Optional[Dict[str, str]] = None,
        cloud_init: Optional[str] = None,
        devices: Optional[Dict[str, Dict[str, str]]] = None,
        start_on_create: bool = True,
        project: str = "default",
        image_server: Optional[str] = None,
        image_protocol: Optional[str] = None,
        description: Optional[str] = None,
        source_type: str = "image",
        job_id: Optional[str] = None,
    ) -> Optional[str]:
        """Start creating a new instance (non-blocking); returns the operation id.

        Backs the dashboard's `POST /instances`. See InstanceManager.create_instance.
        `source_type="none"` creates a blank VM (empty root disk, no OS).
        """
        return await self.instance_manager.create_instance(
            name, host, image, instance_type, profiles, config,
            cloud_init, devices, start_on_create, project,
            image_server, image_protocol, description, source_type, job_id,
        )

    async def fail_create_job(
        self,
        job_id: str,
        *,
        error: str,
        error_code: str,
    ) -> None:
        await self.instance_manager.fail_create_job(
            job_id,
            error=error,
            error_code=error_code,
        )

    async def recover_create_operation_jobs(self) -> int:
        return await self.instance_manager.recover_create_operation_jobs()

    # Image operations
    async def list_images(self, host: str, project: Optional[str] = None) -> ImageStoreListing:
        """The images `project` can use on a host (its own store or the shared default one),
        or every store on the host when no project is named."""
        return await self.image_manager.list_images(host, project)

    # The image methods below are sync: pylxd blocks and a delete waits on an LXD operation, so
    # the router offloads them with an explicit timeout instead of this layer hiding the block.
    def image_store_of(self, host: str, project: Optional[str] = None) -> str:
        """The store `project` reads on a host, which is what a caller must be allowed to see."""
        return self.image_manager.image_store_of(host, project)

    def image_stores_of(self, host: str, projects) -> Dict[str, str]:
        """The store each named project reads on a host, from one read of the project configs."""
        return self.image_manager.image_stores_of(host, projects)

    def get_image(
        self, host: str, fingerprint: str, project: Optional[str] = None
    ) -> Optional[ImageInfo]:
        """Read one image's metadata from the store `project` reads. None when it is not there."""
        return self.image_manager.get_image(host, fingerprint, project)

    def delete_image(self, host: str, fingerprint: str, project: Optional[str] = None) -> bool:
        """Delete an image from the store `project` reads. False when it was already gone."""
        return self.image_manager.delete_image(host, fingerprint, project)
    
    # Project operations
    async def list_projects(self, host: str) -> List[Dict[str, Any]]:
        """List all projects on a specific LXD host."""
        # ProjectManager owns the bounded direct collection read. Ensuring a connection here first
        # would add Vault and network I/O synchronously on the request event loop.
        return await self.project_manager.list_projects(host)

    async def get_project_usage(self, host: str, project: str) -> ProjectUsage:
        """How much of each of `project`'s limits is allocated (see services/lxd/project_usage)."""
        return await asyncio.to_thread(self._read_project_usage_sync, host, project)

    def _read_project_usage_sync(self, host: str, project: str) -> ProjectUsage:
        # The client lookup revalidates the pooled connection with a live request, so it runs on
        # this thread with the read, never inline on the loop.
        self.ensure_connection(host)
        client = require_client(self.connection_manager.get_client(host), host)
        return read_project_usage(client, project)
    
    async def create_project(
        self, 
        host: str, 
        name: str, 
        description: Optional[str] = None,
        config: Optional[Dict[str, str]] = None,
        max_cpu: Optional[int] = None,
        max_ram: Optional[str] = None,
        max_storage: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new project on a specific LXD host."""
        return await self.project_manager.create_project(
            host, name, description, config, max_cpu, max_ram, max_storage
        )
    
    async def update_project(
        self, 
        host: str, 
        project_name: str, 
        description: Optional[str] = None,
        max_cpu: Optional[int] = None,
        max_ram: Optional[str] = None,
        max_storage: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update an existing project's description and resource limits."""
        return await self.project_manager.update_project(
            host, project_name, description, max_cpu, max_ram, max_storage
        )
    
    async def delete_project(self, host: str, name: str) -> None:
        """Delete a project from a specific LXD host."""
        return await self.project_manager.delete_project(host, name)

    async def project_exists(self, host: str, name: str) -> bool:
        """Exact read of one project; raises LXDOperationUnknownError when it cannot tell."""
        return await self.project_manager.project_exists(host, name)
    
    # Metrics operations
    def _get_instance_metrics_blocking(
        self, 
        host: str, 
        instance_name: str,
        project: str = "default"
    ) -> InstanceMetrics:
        """Read one metrics sample. Must run on the instance metrics executor."""
        # important: connect_or_get_client, because connection_manager.get_client is memory-only
        # and answers None on a worker that has never connected to this cluster, so the same poll
        # would answer 200 or 503 depending on the worker.
        client = connect_or_get_client(self, host)
        # important: a None client must not reach find_instance_in_projects, which swallows the
        # AttributeError and returns (None, None); the router would then answer 404 "not found"
        # for an instance whose cluster is only offline.
        if client is None:
            raise LXDConnectionError(f"Could not connect to {host}")
        # Two seconds under the wall budget, so a slow /1.0/metrics read is dropped before
        # run_blocking gives up on the whole sample.
        deadline = time.monotonic() + _INSTANCE_METRICS_TIMEOUT_SECONDS - 2.0
        return read_instance_metrics(client, host, instance_name, project, deadline=deadline)

    async def get_instance_metrics(
        self,
        host: str,
        instance_name: str,
        project: str = "default",
    ) -> InstanceMetrics:
        """Get real-time metrics without blocking the API event loop.

        The pooled pylxd client has its own connect/read socket timeouts. ``run_blocking`` adds
        the wall-clock budget and an in-flight cap so five-second UI polling cannot build an
        unbounded queue when a cluster is degraded.
        """
        return await run_blocking(
            self._get_instance_metrics_blocking,
            host,
            instance_name,
            project,
            timeout=_INSTANCE_METRICS_TIMEOUT_SECONDS,
            label=f"instance.metrics:{host}",
            executor=_instance_metrics_executor,
            max_in_flight=_INSTANCE_METRICS_MAX_IN_FLIGHT,
            concurrency_key=host,
            max_in_flight_per_key=_INSTANCE_METRICS_MAX_IN_FLIGHT_PER_CLUSTER,
        )


_lxd_service: Optional[LXDService] = None


def get_lxd_service() -> LXDService:
    """Get or create the LXD service singleton."""
    global _lxd_service
    if _lxd_service is None:
        _lxd_service = LXDService()
    return _lxd_service
