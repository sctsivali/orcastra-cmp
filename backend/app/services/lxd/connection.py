"""
LXD connection management.
Handles client connections, pooling, and connection lifecycle.
"""

import logging
import time
import tempfile
import os
import atexit
import gc
from typing import Dict, List, Optional, Tuple
import requests
import pylxd
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import get_settings
from .exceptions import LXDConnectionError

logger = logging.getLogger(__name__)


class LXDConnectionManager:
    """
    Manages LXD client connections with pooling and automatic reconnection.
    """
    
    # Connection pool settings for parallel operations (Enterprise scale)
    POOL_CONNECTIONS = 50   # Number of connection pools to cache
    POOL_MAXSIZE = 100      # Maximum connections per pool (> ThreadPoolExecutor workers)
    
    def __init__(self):
        self.settings = get_settings()
        self._clients: Dict[str, pylxd.Client] = {}
        # Track connection creation time (for logging only, not TTL)
        self._connection_times: Dict[str, float] = {}
        # Store FULL connection info in memory (including cert content)
        # This allows reconnection without hitting Vault repeatedly
        # {"cluster_id": {"endpoint": str, "cert_content": str, "key_content": str, "verify": bool}}
        self._connection_info: Dict[str, Dict[str, str]] = {}
        # Track temp file paths for cleanup
        self._temp_cert_paths: Dict[str, Tuple[str, str]] = {}
        # Vault-registered cluster whitelist - only these clusters
        # should be visible via get_node_names(). Prevents stale or
        # unregistered nodes from leaking into API responses.
        self._registered_clusters: Optional[set] = None
        
        # Register cleanup on exit
        atexit.register(self._cleanup_all_temp_files)
        atexit.register(self._secure_cleanup_credentials)
    
    def _configure_client_session(self, client: pylxd.Client) -> None:
        """
        Configure pylxd client's session with larger connection pool.
        This prevents 'Connection pool is full' warnings during parallel operations.
        """
        try:
            # pylxd uses client.api.session internally
            if hasattr(client, 'api') and hasattr(client.api, 'session'):
                session = client.api.session
                
                retry_strategy = Retry(
                    total=3,
                    connect=0,
                    backoff_factor=0.5,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"],
                    raise_on_status=False
                )
                
                adapter = HTTPAdapter(
                    max_retries=retry_strategy,
                    pool_connections=self.POOL_CONNECTIONS,
                    pool_maxsize=self.POOL_MAXSIZE,
                    pool_block=False  # Don't block, just create new connection
                )
                
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                logger.debug(f"Configured client session with pool_maxsize={self.POOL_MAXSIZE}")
        except Exception as e:
            logger.debug(f"Could not configure client session: {e}")
    
    def _create_http_session(self):
        """Create HTTP session with retry configuration."""
        session = requests.Session()
        
        retry_strategy = Retry(
            total=3,
            connect=0,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"],
            raise_on_status=False
        )
        
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=self.POOL_CONNECTIONS,
            pool_maxsize=self.POOL_MAXSIZE,
            pool_block=False
        )
        
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({
            'Connection': 'keep-alive',
            'Keep-Alive': 'timeout=600, max=1000'
        })
        
        return session
    
    def _validate_connection(self, client: pylxd.Client, node_name: str) -> bool:
        """
        Validate if connection is still alive and authenticated.
        """
        try:
            response = client.api.get(timeout=(3, 5))
            if response.status_code == 200:
                data = response.json()
                auth_status = data.get("metadata", {}).get("auth", "")
                if auth_status == "trusted":
                    return True
                else:
                    logger.warning(f"Connection to {node_name} not trusted (auth={auth_status})")
                    return False
            return True
        except Exception as e:
            logger.warning(f"Connection validation failed for {node_name}: {e}")
            return False
    
    def _create_temp_cert_files(self, cluster_id: str, cert_content: str, key_content: str) -> Tuple[str, str]:
        """
        Create temporary certificate files for pylxd client.
        Files are created with 0600 permissions.
        """
        cert_fd, cert_path = tempfile.mkstemp(suffix='.crt', prefix=f'lxd_{cluster_id}_')
        key_fd, key_path = tempfile.mkstemp(suffix='.key', prefix=f'lxd_{cluster_id}_')
        
        try:
            os.write(cert_fd, cert_content.encode('utf-8'))
            os.close(cert_fd)
            os.chmod(cert_path, 0o600)
            
            os.write(key_fd, key_content.encode('utf-8'))
            os.close(key_fd)
            os.chmod(key_path, 0o600)
            
            self._temp_cert_paths[cluster_id] = (cert_path, key_path)
            logger.debug(f"Created temp cert files for {cluster_id}")
            return cert_path, key_path
            
        except Exception as e:
            try:
                os.close(cert_fd)
                os.close(key_fd)
                os.unlink(cert_path)
                os.unlink(key_path)
            except:
                pass
            raise e
    
    def _cleanup_temp_cert_files(self, cluster_id: str) -> None:
        """
        Clean up temporary certificate files.
        Overwrites file content before deletion.
        """
        if cluster_id in self._temp_cert_paths:
            cert_path, key_path = self._temp_cert_paths[cluster_id]
            try:
                for path in [cert_path, key_path]:
                    if os.path.exists(path):
                        try:
                            size = os.path.getsize(path)
                            with open(path, 'wb') as f:
                                f.write(b'\x00' * size)
                                f.flush()
                                os.fsync(f.fileno())
                        except Exception:
                            pass
                        os.unlink(path)
                logger.debug(f"Cleaned temp cert files for {cluster_id}")
            except Exception as e:
                logger.debug(f"Error cleaning temp files for {cluster_id}: {e}")
            del self._temp_cert_paths[cluster_id]
    
    def _cleanup_all_temp_files(self) -> None:
        """Clean up all temporary certificate files (called on exit)."""
        for cluster_id in list(self._temp_cert_paths.keys()):
            self._cleanup_temp_cert_files(cluster_id)
    
    def _secure_cleanup_credentials(self) -> None:
        """Clear all credential data from memory on exit."""
        for cluster_id in list(self._connection_info.keys()):
            try:
                info = self._connection_info[cluster_id]
                if "cert_content" in info:
                    info["cert_content"] = ""
                if "key_content" in info:
                    info["key_content"] = ""
            except Exception:
                pass
        self._connection_info.clear()
        gc.collect()
        logger.debug("Cleared all credentials from memory")
    
    def connect_with_vault_cert(
        self,
        cluster_id: str,
        endpoint: str,
        cert_content: str,
        key_content: str,
        verify: bool = False
    ) -> pylxd.Client:
        """
        Create LXD client connection using certificate content from Vault.
        """
        try:
            # Store FULL connection info in memory (for auto-reconnect)
            self._connection_info[cluster_id] = {
                "endpoint": endpoint,
                "cert_content": cert_content,
                "key_content": key_content,
                "verify": verify
            }
            
            # Clean up existing temp files before creating new ones
            self._cleanup_temp_cert_files(cluster_id)
            
            # Create temp cert files (pylxd requires file paths)
            cert_path, key_path = self._create_temp_cert_files(cluster_id, cert_content, key_content)
            
            # Create LXD client with faster timeout for quick failure detection
            client = pylxd.Client(
                endpoint=endpoint,
                cert=(cert_path, key_path),
                verify=verify,
                timeout=(3, 8)
            )
            
            # Verify connection and authentication
            response = client.api.get(timeout=(2, 5))
            if response.status_code == 200:
                data = response.json()
                auth_status = data.get("metadata", {}).get("auth", "")
                if auth_status != "trusted":
                    self._cleanup_temp_cert_files(cluster_id)
                    raise LXDConnectionError(f"Certificate not trusted by {cluster_id} (auth={auth_status})")
            
            # Configure session with larger connection pool for parallel operations
            self._configure_client_session(client)
            
            # Cache the client
            self._clients[cluster_id] = client
            self._connection_times[cluster_id] = time.time()
            logger.info(f"Connected to cluster {cluster_id} using Vault certificate")

            # audit: connection established
            try:
                from app.services.activity_logger import get_activity_logger
                get_activity_logger().log_activity(
                    action="lxd.connection.establish",
                    target_type="cluster",
                    target_id=cluster_id,
                    target_host=endpoint,
                    result="success",
                    details={"endpoint": endpoint},
                )
            except Exception:
                pass  # never block connection on audit failure

            return client
                    
        except LXDConnectionError:
            raise
        except Exception as e:
            self._cleanup_temp_cert_files(cluster_id)
            if cluster_id in self._connection_info:
                del self._connection_info[cluster_id]
            logger.error(f"Failed to connect to cluster {cluster_id}: {e}")
            raise LXDConnectionError(f"Cannot connect to {cluster_id}: {str(e)}")
    
    def get_client(self, cluster_id: str) -> Optional[pylxd.Client]:
        """
        Get cached LXD client for the specified cluster.
        
        RECONNECTION LOGIC:
        - If client exists and is valid, return it
        - If validation fails, attempt auto-reconnect using stored credentials
        - NO TTL-based refresh (connections persist indefinitely while valid)

        Returns None if the cluster is not in the current user's org scope.
        """
        # org-scope filter: deny access to clusters outside user's org
        from app.core.org_scope import get_current_allowed_clusters
        allowed = get_current_allowed_clusters()
        if allowed is not None and cluster_id not in allowed:
            return None

        if cluster_id in self._clients:
            # Validate connection is still working
            if self._validate_connection(self._clients[cluster_id], cluster_id):
                return self._clients[cluster_id]
            else:
                # Connection failed validation, try reconnect
                logger.warning(f"Connection to {cluster_id} failed validation, reconnecting...")
                self.cleanup_connection(cluster_id)
                return self._auto_reconnect(cluster_id)
        else:
            # Not in cache, try auto-reconnect if we have connection info
            return self._auto_reconnect(cluster_id)

    def get_project_client(self, cluster_id: str, project: str, timeout=(3, 8)) -> Optional[pylxd.Client]:
        """
        Build a short-lived pylxd client scoped to a specific LXD project,
        reusing the cluster's cached endpoint + certificate files.

        pylxd binds the project at the client level (Instance.create has no
        per-call project arg), so creating an instance in a non-default project
        needs a project-scoped client. The result is meant to be used once and
        discarded; the pooled default-project client is left untouched.

        Call get_client() first so the cached connection info + temp cert files
        exist. Returns None if they don't.
        """
        info = self._connection_info.get(cluster_id)
        paths = self._temp_cert_paths.get(cluster_id)
        if not info or not paths:
            logger.warning(
                f"No cached connection info for {cluster_id}; cannot build project-scoped client"
            )
            return None
        cert_path, key_path = paths
        try:
            return pylxd.Client(
                endpoint=info["endpoint"],
                cert=(cert_path, key_path),
                verify=info.get("verify", False),
                timeout=timeout,
                project=project or "default",
            )
        except Exception as e:
            logger.error(
                f"Failed to build project-scoped client for {cluster_id}/{project}: {e}"
            )
            return None

    def _auto_reconnect(self, cluster_id: str) -> Optional[pylxd.Client]:
        """
        Attempt to automatically reconnect using stored connection info.
        
        FLOW:
        1. Check if connection info exists in memory
        2. Use stored cert content to reconnect
        3. NO Vault or Redis calls here (memory only)
        """
        if cluster_id not in self._connection_info:
            logger.debug(f"No stored connection info for {cluster_id}, cannot auto-reconnect")
            return None
        
        try:
            info = self._connection_info[cluster_id]
            
            # Reconnect using stored credentials
            self._cleanup_temp_cert_files(cluster_id)
            cert_path, key_path = self._create_temp_cert_files(
                cluster_id, 
                info["cert_content"], 
                info["key_content"]
            )
            
            client = pylxd.Client(
                endpoint=info["endpoint"],
                cert=(cert_path, key_path),
                verify=info.get("verify", False),
                timeout=(3, 8)
            )
            
            # Verify authentication
            response = client.api.get(timeout=(2, 5))
            if response.status_code == 200:
                data = response.json()
                if data.get("metadata", {}).get("auth") != "trusted":
                    logger.warning(f"Reconnect failed: cert not trusted by {cluster_id}")
                    self._cleanup_temp_cert_files(cluster_id)
                    return None
            
            self._clients[cluster_id] = client
            self._connection_times[cluster_id] = time.time()
            logger.info(f"Auto-reconnected to {cluster_id}")

            # audit auto-reconnect events
            try:
                from app.services.activity_logger import get_activity_logger
                activity_logger = get_activity_logger()
                activity_logger.log_activity(
                    action="lxd.connection.reconnect",
                    target_type="cluster",
                    target_id=cluster_id,
                    result="success",
                    details={"endpoint": info["endpoint"], "method": "auto_reconnect"},
                )
            except Exception:
                pass

            return client
            
        except Exception as e:
            logger.warning(f"Auto-reconnect to {cluster_id} failed: {e}")
            self._cleanup_temp_cert_files(cluster_id)

            # audit failed reconnect
            try:
                from app.services.activity_logger import get_activity_logger
                activity_logger = get_activity_logger()
                activity_logger.log_activity(
                    action="lxd.connection.reconnect",
                    target_type="cluster",
                    target_id=cluster_id,
                    result="failure",
                    error_code=type(e).__name__,
                    error_message=str(e),
                )
            except Exception:
                pass

            return None
    
    def sync_registered_clusters(self, cluster_ids: List[str]) -> None:
        """Update the Vault-registered cluster whitelist.

        Called at startup and after cluster registration/deregistration
        to keep the in-memory whitelist in sync with Vault.
        """
        self._registered_clusters = set(cluster_ids)
        logger.debug(f"Synced registered clusters whitelist: {self._registered_clusters}")

    def register_cluster(self, cluster_id: str) -> None:
        """Add a single cluster to the registered whitelist."""
        if self._registered_clusters is None:
            self._registered_clusters = set()
        self._registered_clusters.add(cluster_id)

    def unregister_cluster(self, cluster_id: str) -> None:
        """Remove a single cluster from the registered whitelist."""
        if self._registered_clusters is not None:
            self._registered_clusters.discard(cluster_id)

    def get_node_names(self) -> list:
        """Get list of active cluster IDs that are both connected AND registered.

        Only returns nodes that exist in Vault's registered cluster list
        to prevent stale/unregistered nodes from appearing in API responses.
        Filtered by org-scope context variable when set.
        """
        from app.core.org_scope import apply_cluster_filter
        connected = list(self._clients.keys())
        # intersect with Vault-registered whitelist when available
        if self._registered_clusters is not None:
            connected = [cid for cid in connected if cid in self._registered_clusters]
        return apply_cluster_filter(connected)
    
    def has_connection_info(self, cluster_id: str) -> bool:
        """Check if we have stored connection info for a cluster."""
        return cluster_id in self._connection_info
    
    def cleanup_connection(self, cluster_id: str) -> None:
        """
        Cleanup connection client only (preserve connection info for reconnect).
        """
        if cluster_id in self._clients:
            try:
                if hasattr(self._clients[cluster_id], '_api_session'):
                    self._clients[cluster_id]._api_session.close()
            except Exception as e:
                logger.debug(f"Error closing connection for {cluster_id}: {e}")
            del self._clients[cluster_id]
        if cluster_id in self._connection_times:
            del self._connection_times[cluster_id]
        # Keep _connection_info for auto-reconnect
    
    def remove_connection(self, cluster_id: str) -> None:
        """
        Completely remove a connection including all stored info.
        Use this when a cluster is deleted or deregistered.
        """
        self.cleanup_connection(cluster_id)
        
        # Remove from registered whitelist
        self.unregister_cluster(cluster_id)
        
        # Clear credentials from memory
        if cluster_id in self._connection_info:
            info = self._connection_info[cluster_id]
            if "cert_content" in info:
                info["cert_content"] = ""
            if "key_content" in info:
                info["key_content"] = ""
            del self._connection_info[cluster_id]
        
        # Clean up temp files
        self._cleanup_temp_cert_files(cluster_id)

        # audit: connection removed
        try:
            from app.services.activity_logger import get_activity_logger
            get_activity_logger().log_activity(
                action="lxd.connection.remove",
                target_type="cluster",
                target_id=cluster_id,
                result="success",
            )
        except Exception:
            pass  # never block removal on audit failure

        logger.info(f"Completely removed connection for {cluster_id}")
