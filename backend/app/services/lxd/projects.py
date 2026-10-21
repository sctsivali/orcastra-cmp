"""
Project management operations.
Handles create, update, delete, and list operations for LXD projects.
"""

import asyncio
import logging
import uuid
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor
from pylxd.exceptions import LXDAPIException, NotFound
from requests import exceptions as requests_exceptions

from app.core.blocking import (
    BlockingPoolFull,
    BlockingTimeout,
    in_flight_for_key,
    run_blocking,
)
from app.core.lxd_path import safe_segment
from app.services.lxd.utils import wait_for_lxd_operation, write_timeout

from . import health
from .exceptions import (
    LXDConnectionError,
    LXDLocalUnavailableError,
    LXDOperationError,
    LXDOperationUnknownError,
)
from .connection import LXDConnectionManager
from .audit import audit_operation

logger = logging.getLogger(__name__)

# A project listing is one whole-cluster read. Keep it off the event loop, start every admitted
# call immediately, and outlive the raw request's 3s connect + 20s read socket budget by a margin.
_PROJECT_LIST_WORKERS = 10
_PROJECT_LIST_TIMEOUT_SECONDS = 30.0
_PROJECT_LIST_READ_TIMEOUT = (3, 20)
_PROJECT_WRITE_WAIT_SECONDS = 60
_PROJECT_WRITE_WALL_SECONDS = 240.0
_PROJECT_WRITE_MAX_IN_FLIGHT_PER_CLUSTER = 1
_PROJECT_READ_TIMEOUT = (5, 25)
_projects_executor = ThreadPoolExecutor(
    max_workers=_PROJECT_LIST_WORKERS, thread_name_prefix="lxd_projects_"
)
_UNCHANGED = object()
_CREATE_CORRELATION_KEY = "user.orcastra.creation_id"


def _public_project_record(project: Dict[str, Any]) -> Dict[str, Any]:
    """Remove the control-plane correlation marker from API-facing project records."""
    result = dict(project)
    config = dict(result.get("config") or {})
    config.pop(_CREATE_CORRELATION_KEY, None)
    result["config"] = config
    return result


def _create_config(
    config: Optional[Dict[str, str]],
    max_cpu: Optional[int],
    max_ram: Optional[str],
    max_storage: Optional[str],
    *,
    creation_id: Optional[str] = None,
) -> Dict[str, str]:
    project_config = dict(config or {
        "features.images": "true",
        "features.profiles": "true",
        "features.storage.volumes": "true",
        "features.networks": "true",
    })
    if max_cpu is not None:
        project_config["limits.cpu"] = str(max_cpu)
    if max_ram is not None:
        project_config["limits.memory"] = max_ram
    if max_storage is not None:
        project_config["limits.disk"] = max_storage
    if creation_id is not None:
        # This non-secret server marker is the proof that an ambiguous POST created this exact
        # project. Payload equality alone could grant another organization's pre-existing
        # project when a late duplicate-name refusal was lost to a caller wall timeout.
        project_config[_CREATE_CORRELATION_KEY] = creation_id
    return project_config


def _create_readback_matches(
    project: Dict[str, Any],
    *,
    name: str,
    description: Optional[str],
    expected_config: Dict[str, str],
) -> bool:
    actual_config = project.get("config")
    return (
        project.get("name") == name
        and project.get("description", "") == (description or "")
        and isinstance(actual_config, dict)
        and all(actual_config.get(key) == value for key, value in expected_config.items())
    )


def _update_readback_matches(
    project: Dict[str, Any],
    *,
    description: Optional[str],
    max_cpu: Optional[int],
    max_ram: Optional[str],
    max_storage: Optional[str],
) -> bool:
    if description is not None and project.get("description", "") != description:
        return False
    config = project.get("config")
    if not isinstance(config, dict):
        return False
    expected = (
        (
            "limits.cpu",
            None if max_cpu == 0 else str(max_cpu) if max_cpu is not None else _UNCHANGED,
        ),
        (
            "limits.memory",
            None if max_ram in ("", "0") else max_ram if max_ram is not None else _UNCHANGED,
        ),
        (
            "limits.disk",
            (
                None
                if max_storage in ("", "0")
                else max_storage if max_storage is not None else _UNCHANGED
            ),
        ),
    )
    for key, value in expected:
        if value is _UNCHANGED:
            continue
        if value is None:
            if key in config:
                return False
        elif config.get(key) != value:
            return False
    return True


def _definitive_lxd_refusal(exc: LXDAPIException, phase: str) -> bool:
    """Whether LXD supplied a terminal verdict rather than a transport/wait ambiguity."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if phase == "submitting" and isinstance(status_code, int) and 400 <= status_code < 500:
        return True
    try:
        metadata = response.json().get("metadata")
    except Exception:
        return False
    return isinstance(metadata, dict) and metadata.get("status") in {"Failure", "Cancelled"}


async def _drain_project_worker(host: str) -> None:
    """Keep the ownership lock held until a timed-out/cancelled worker really exits."""
    while in_flight_for_key(_projects_executor, host):
        try:
            await asyncio.shield(asyncio.sleep(0.05))
        except asyncio.CancelledError:
            # Repeated cancellation cannot let a submitted worker outlive the ownership lock.
            continue


async def _run_project_blocking(
    function: Any,
    *args: Any,
    host: str,
    label: str,
) -> Any:
    try:
        return await run_blocking(
            function,
            *args,
            timeout=_PROJECT_WRITE_WALL_SECONDS,
            label=f"project.{label}[{host}]",
            executor=_projects_executor,
            max_in_flight=_PROJECT_LIST_WORKERS,
            concurrency_key=host,
            max_in_flight_per_key=_PROJECT_WRITE_MAX_IN_FLIGHT_PER_CLUSTER,
        )
    except (BlockingTimeout, asyncio.CancelledError):
        # run_blocking cannot cancel a thread. Every worker call below has a socket timeout;
        # drain that bounded worker before the caller may release cluster ownership.
        await _drain_project_worker(host)
        raise


class ProjectManager:
    """
    Manages LXD project operations.
    """
    
    def __init__(self, connection_manager: LXDConnectionManager, cert_manager=None):
        self.connection_manager = connection_manager
        self.cert_manager = cert_manager
    
    def _get_client_with_reconnect(self, node_name: str):
        """Get client with automatic reconnection if stale."""
        client = self.connection_manager.get_client(node_name)
        if client is None and self.cert_manager:
            logger.info(f"Connection to {node_name} is stale, attempting reconnect...")
            try:
                # Pass connection_manager for proper pooling
                client = self.cert_manager.connect_with_vault_cert(
                    node_name,
                    connection_manager=self.connection_manager
                )
                logger.info(f"Reconnected to {node_name} successfully")
            except Exception as e:
                logger.warning(f"Reconnect failed for {node_name}: {e}")
                client = None
        return client
    
    async def list_projects(self, host: str) -> List[Dict[str, Any]]:
        """List all projects on a host without an unbounded worker wait."""
        try:
            return await run_blocking(
                self._list_projects_sync,
                host,
                timeout=_PROJECT_LIST_TIMEOUT_SECONDS,
                label=f"project.list[{host}]",
                executor=_projects_executor,
                max_in_flight=_PROJECT_LIST_WORKERS,
                concurrency_key=host,
                max_in_flight_per_key=_PROJECT_WRITE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
        except (BlockingTimeout, BlockingPoolFull) as exc:
            raise LXDLocalUnavailableError(
                f"Project list for {host} is temporarily unavailable"
            ) from exc
    
    def _list_projects_sync(self, host: str) -> List[Dict[str, Any]]:
        """Synchronous implementation of list_projects."""
        try:
            # The collection request below is the liveness proof. A preliminary get_client()
            # validation/reconnect chain plus the collection read could outlive this worker's
            # 30-second wall cap and retain its admission slot after the caller left.
            # A genuinely cold worker is repaired by the background healer; callers receive a
            # typed local-unavailable result that fleet routes must never cache as a dead cluster.
            client = self.connection_manager.get_cached_client(host)
            
            if client is None:
                # get_cached_client() also returns None for a known remote outage. Preserve
                # that distinction: fleet reads may safely render a host this worker last saw
                # fail as partial, while a genuinely cold local worker must fail the whole response
                # so it cannot cache a false remote outage.
                if health.known_unreachable(host):
                    raise LXDConnectionError(
                        f"Connection to {host} is temporarily unavailable"
                    )
                raise LXDLocalUnavailableError(
                    f"Project list for {host} is temporarily unavailable"
                )
            
            # pylxd's collection manager starts at recursion=0, then lazily syncs every
            # Project attribute below. Ask LXD/Incus for the complete records directly so the
            # list is one socket-bounded request regardless of project count.
            response = client.api.projects.get(
                params={"recursion": 1}, timeout=_PROJECT_LIST_READ_TIMEOUT
            )
            projects = response.json().get("metadata") or []
            
            project_list = []
            for project in projects:
                if not isinstance(project, dict):
                    raise ValueError("Project collection did not return recursive records")

                config = dict(project.get("config") or {})
                config.pop(_CREATE_CORRELATION_KEY, None)
                tags_str = config.get("user.tags", "")
                tags = [tag.strip() for tag in tags_str.split(",") if tag.strip()] if tags_str else []
                
                project_info = {
                    "name": project.get("name") or "",
                    "description": project.get("description") or "",
                    "config": config,
                    "used_by": project.get("used_by") or [],
                    "host": host,
                    "tags": tags,
                }
                project_list.append(project_info)
            
            logger.debug(f"Listed {len(project_list)} projects from {host}")
            return project_list
            
        except LXDConnectionError:
            raise
        except Exception as e:
            logger.error(f"Error listing projects from {host}: {e}")
            raise LXDOperationError(f"Failed to list projects: {str(e)}")

    def _read_project_record(
        self,
        client: Any,
        host: str,
        name: str,
    ) -> Dict[str, Any]:
        response = client.api.projects[safe_segment(name)].get(
            timeout=_PROJECT_READ_TIMEOUT
        )
        project = response.json().get("metadata") or {}
        if not isinstance(project, dict) or not project.get("name"):
            raise LXDOperationError("Project read returned an incomplete record")
        return {
            "name": project["name"],
            "description": project.get("description") or "",
            "config": project.get("config") or {},
            "used_by": project.get("used_by") or [],
            "host": host,
        }
    
    @audit_operation(
        "lxd.project.create", "project",
        target_id_param="name",
        extract_details=lambda args, kwargs, result: {
            "description": kwargs.get("description"),
            "max_cpu": kwargs.get("max_cpu"),
            "max_ram": kwargs.get("max_ram"),
            "max_storage": kwargs.get("max_storage"),
        }
    )
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
        """Create a project without blocking the request loop or growing an unbounded queue."""
        creation_id = uuid.uuid4().hex
        try:
            project = await _run_project_blocking(
                self._create_project_sync,
                host,
                name,
                description,
                config,
                max_cpu,
                max_ram,
                max_storage,
                creation_id,
                host=host,
                label="create",
            )
        except BlockingPoolFull as exc:
            raise LXDLocalUnavailableError(
                f"Project create for {host} is temporarily unavailable"
            ) from exc
        except (BlockingTimeout, LXDOperationUnknownError) as exc:
            await _drain_project_worker(host)
            logger.warning(
                "Project create outcome requires exact reconciliation for %s on %s (%s)",
                name,
                host,
                type(exc).__name__,
            )
            project = await self._reconcile_created_project(
                host,
                name,
                description,
                config,
                max_cpu,
                max_ram,
                max_storage,
                creation_id,
            )
        return _public_project_record(project)

    async def _reconcile_created_project(
        self,
        host: str,
        name: str,
        description: Optional[str],
        config: Optional[Dict[str, str]],
        max_cpu: Optional[int],
        max_ram: Optional[str],
        max_storage: Optional[str],
        creation_id: str,
    ) -> Dict[str, Any]:
        try:
            return await _run_project_blocking(
                self._reconcile_created_project_sync,
                host,
                name,
                description,
                config,
                max_cpu,
                max_ram,
                max_storage,
                creation_id,
                host=host,
                label="create-reconcile",
            )
        except (
            BlockingTimeout,
            BlockingPoolFull,
            LXDConnectionError,
            LXDOperationError,
        ) as exc:
            raise LXDOperationUnknownError("create", name) from exc

    def _reconcile_created_project_sync(
        self,
        host: str,
        name: str,
        description: Optional[str],
        config: Optional[Dict[str, str]],
        max_cpu: Optional[int],
        max_ram: Optional[str],
        max_storage: Optional[str],
        creation_id: str,
    ) -> Dict[str, Any]:
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            project = self._read_project_record(client, host, name)
            if not _create_readback_matches(
                project,
                name=name,
                description=description,
                expected_config=_create_config(
                    config,
                    max_cpu,
                    max_ram,
                    max_storage,
                    creation_id=creation_id,
                ),
            ):
                raise LXDOperationUnknownError("create", name)
            return project
        except LXDConnectionError:
            raise
        except LXDOperationUnknownError:
            raise
        except Exception as exc:
            raise LXDOperationUnknownError("create", name) from exc

    def _create_project_sync(
        self,
        host: str,
        name: str,
        description: Optional[str],
        config: Optional[Dict[str, str]],
        max_cpu: Optional[int],
        max_ram: Optional[str],
        max_storage: Optional[str],
        creation_id: str,
    ) -> Dict[str, Any]:
        phase = "connecting"
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")

            project_config = _create_config(
                config,
                max_cpu,
                max_ram,
                max_storage,
                creation_id=creation_id,
            )

            project_data = {
                "name": name,
                "description": description or "",
                "config": project_config
            }

            phase = "submitting"
            response = client.api.projects.post(
                json=project_data,
                timeout=write_timeout(_PROJECT_WRITE_WAIT_SECONDS),
            )
            phase = "waiting"
            wait_for_lxd_operation(
                client,
                response,
                timeout=_PROJECT_WRITE_WAIT_SECONDS,
            )
            phase = "verifying"
            project = self._read_project_record(client, host, name)
            if not _create_readback_matches(
                project,
                name=name,
                description=description,
                expected_config=project_config,
            ):
                raise LXDOperationUnknownError("create", name)

            logger.info(f"Created project '{name}' on {host}")
            return project

        except LXDOperationUnknownError:
            raise
        except LXDConnectionError:
            raise
        except LXDAPIException as e:
            logger.error(f"LXD API error creating project '{name}' on {host}: {e}")
            if phase == "verifying":
                raise LXDOperationUnknownError("create", name) from e
            if phase in {"submitting", "waiting"} and not _definitive_lxd_refusal(e, phase):
                raise LXDOperationUnknownError("create", name) from e
            raise LXDOperationError(f"Failed to create project: {str(e)}")
        except requests_exceptions.RequestException as e:
            if phase in {"submitting", "waiting", "verifying"}:
                raise LXDOperationUnknownError("create", name) from e
            raise LXDConnectionError(f"Could not connect to {host}") from e
        except Exception as e:
            logger.error(f"Error creating project '{name}' on {host}: {e}")
            if phase in {"submitting", "waiting", "verifying"}:
                raise LXDOperationUnknownError("create", name) from e
            raise LXDOperationError(f"Failed to create project: {str(e)}")
    
    @audit_operation(
        "lxd.project.update", "project",
        target_id_param="project_name",
        extract_details=lambda args, kwargs, result: {
            "description": kwargs.get("description"),
            "max_cpu": kwargs.get("max_cpu"),
            "max_ram": kwargs.get("max_ram"),
            "max_storage": kwargs.get("max_storage"),
        }
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
        """Update a project on the bounded project executor."""
        try:
            project = await _run_project_blocking(
                self._update_project_sync,
                host,
                project_name,
                description,
                max_cpu,
                max_ram,
                max_storage,
                host=host,
                label="update",
            )
        except BlockingPoolFull as exc:
            raise LXDLocalUnavailableError(
                f"Project update for {host} is temporarily unavailable"
            ) from exc
        except (BlockingTimeout, LXDOperationUnknownError) as exc:
            await _drain_project_worker(host)
            logger.warning(
                "Project update outcome requires exact reconciliation for %s on %s (%s)",
                project_name,
                host,
                type(exc).__name__,
            )
            project = await self._reconcile_updated_project(
                host,
                project_name,
                description,
                max_cpu,
                max_ram,
                max_storage,
            )
        return _public_project_record(project)

    async def _reconcile_updated_project(
        self,
        host: str,
        project_name: str,
        description: Optional[str],
        max_cpu: Optional[int],
        max_ram: Optional[str],
        max_storage: Optional[str],
    ) -> Dict[str, Any]:
        try:
            return await _run_project_blocking(
                self._reconcile_updated_project_sync,
                host,
                project_name,
                description,
                max_cpu,
                max_ram,
                max_storage,
                host=host,
                label="update-reconcile",
            )
        except (
            BlockingTimeout,
            BlockingPoolFull,
            LXDConnectionError,
            LXDOperationError,
        ) as exc:
            raise LXDOperationUnknownError("update", project_name) from exc

    def _reconcile_updated_project_sync(
        self,
        host: str,
        project_name: str,
        description: Optional[str],
        max_cpu: Optional[int],
        max_ram: Optional[str],
        max_storage: Optional[str],
    ) -> Dict[str, Any]:
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            project = self._read_project_record(client, host, project_name)
            if not _update_readback_matches(
                project,
                description=description,
                max_cpu=max_cpu,
                max_ram=max_ram,
                max_storage=max_storage,
            ):
                raise LXDOperationUnknownError("update", project_name)
            return project
        except LXDConnectionError:
            raise
        except LXDOperationUnknownError:
            raise
        except Exception as exc:
            raise LXDOperationUnknownError("update", project_name) from exc

    def _update_project_sync(
        self,
        host: str,
        project_name: str,
        description: Optional[str],
        max_cpu: Optional[int],
        max_ram: Optional[str],
        max_storage: Optional[str],
    ) -> Dict[str, Any]:
        phase = "connecting"
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")

            phase = "reading"
            project = self._read_project_record(client, host, project_name)
            project_description = (
                description if description is not None else project["description"]
            )
            project_config = dict(project["config"])
            if max_cpu is not None:
                if max_cpu == 0:
                    project_config.pop("limits.cpu", None)
                else:
                    project_config["limits.cpu"] = str(max_cpu)
            if max_ram is not None:
                if max_ram == "0" or max_ram == "":
                    project_config.pop("limits.memory", None)
                else:
                    project_config["limits.memory"] = max_ram
            if max_storage is not None:
                if max_storage == "0" or max_storage == "":
                    project_config.pop("limits.disk", None)
                else:
                    project_config["limits.disk"] = max_storage

            phase = "submitting"
            response = client.api.projects[safe_segment(project_name)].put(
                json={
                    "description": project_description,
                    "config": project_config,
                },
                timeout=write_timeout(_PROJECT_WRITE_WAIT_SECONDS),
            )
            phase = "waiting"
            wait_for_lxd_operation(
                client,
                response,
                timeout=_PROJECT_WRITE_WAIT_SECONDS,
            )
            phase = "verifying"
            project = self._read_project_record(client, host, project_name)
            if not _update_readback_matches(
                project,
                description=description,
                max_cpu=max_cpu,
                max_ram=max_ram,
                max_storage=max_storage,
            ):
                raise LXDOperationUnknownError("update", project_name)

            logger.info(f"Updated project '{project_name}' on {host}")
            return project

        except LXDOperationUnknownError:
            raise
        except LXDConnectionError:
            raise
        except NotFound:
            logger.error(f"Project '{project_name}' not found on {host}")
            raise LXDOperationError(f"Project '{project_name}' not found")
        except LXDAPIException as e:
            logger.error(f"LXD API error updating project '{project_name}' on {host}: {e}")
            if phase == "verifying":
                raise LXDOperationUnknownError("update", project_name) from e
            if phase in {"submitting", "waiting"} and not _definitive_lxd_refusal(e, phase):
                raise LXDOperationUnknownError("update", project_name) from e
            raise LXDOperationError(f"Failed to update project: {str(e)}")
        except requests_exceptions.RequestException as e:
            if phase in {"submitting", "waiting", "verifying"}:
                raise LXDOperationUnknownError("update", project_name) from e
            raise LXDConnectionError(f"Could not connect to {host}") from e
        except Exception as e:
            logger.error(f"Error updating project '{project_name}' on {host}: {e}")
            if phase in {"submitting", "waiting", "verifying"}:
                raise LXDOperationUnknownError("update", project_name) from e
            raise LXDOperationError(f"Failed to update project: {str(e)}")
    
    @audit_operation("lxd.project.delete", "project", target_id_param="name")
    async def delete_project(self, host: str, name: str) -> None:
        """Delete a project from a specific LXD host."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            
            # get the project first
            project = client.projects.get(safe_segment(name))
            
            # delete the project
            project.delete()
            
            logger.info(f"Deleted project '{name}' from {host}")
            
        except LXDConnectionError:
            raise
        except NotFound:
            logger.error(f"Project '{name}' not found on {host}")
            raise LXDOperationError(f"Project '{name}' not found")
        except LXDAPIException as e:
            logger.error(f"LXD API error deleting project '{name}' from {host}: {e}")
            raise LXDOperationError(f"Failed to delete project: {str(e)}")
        except Exception as e:
            logger.error(f"Error deleting project '{name}' from {host}: {e}")
            raise LXDOperationError(f"Failed to delete project: {str(e)}")
