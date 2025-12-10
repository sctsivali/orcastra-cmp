"""
LXD connection management.
Handles client connections, pooling, and connection lifecycle.
"""

import logging
import time
from typing import Dict, Optional
import requests
import pylxd
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import get_settings, LXDNodeConfig
from .exceptions import LXDConnectionError

logger = logging.getLogger(__name__)


class LXDConnectionManager:
    """
    Manages LXD client connections with pooling and automatic refresh.
    """
    
    def __init__(self):
        self.settings = get_settings()
        self._clients: Dict[str, pylxd.Client] = {}
        self._node_configs: Dict[str, LXDNodeConfig] = {}
        # Track connection creation time for refresh: {"node_name": timestamp}
        self._connection_times: Dict[str, float] = {}
        # Connection refresh interval (30 minutes in seconds)
        self._connection_refresh_interval = 1800
        self._initialize_nodes()
    
    def _initialize_nodes(self) -> None:
        """Initialize node configurations."""
        self._node_configs = {
            self.settings.lxd_node1_name: self.settings.get_node1_config(),
            self.settings.lxd_node2_name: self.settings.get_node2_config(),
        }
    
    def _create_http_session(self):
        """
        Create HTTP session with proper timeout, keep-alive and retry configuration.
        """
        session = requests.Session()
        
        # Configure retry strategy
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"],
            raise_on_status=False
        )
        
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20,
            pool_block=False
        )
        
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        # Enable TCP keep-alive to prevent idle connection drops
        session.headers.update({
            'Connection': 'keep-alive',
            'Keep-Alive': 'timeout=600, max=1000'
        })
        
        return session
    
    def _should_refresh_connection(self, node_name: str) -> bool:
        """
        Check if connection should be refreshed based on age.
        """
        if node_name not in self._connection_times:
            return True
        
        connection_age = time.time() - self._connection_times[node_name]
        return connection_age > self._connection_refresh_interval
    
    def _validate_connection(self, client: pylxd.Client, node_name: str) -> bool:
        """
        Validate if connection is still alive with proper timeout.
        """
        try:
            # Make an actual API call with shorter timeout to verify connection quickly
            client.api.get(timeout=(3, 5))  # 3s connect, 5s read timeout
            return True
        except Exception as e:
            logger.warning(f"Connection validation failed for {node_name}: {e}")
            return False
    
    def get_client(self, node_name: str) -> pylxd.Client:
        """
        Get or create an LXD client for the specified node.
        Uses certificate-based authentication with optional trust password fallback.
        Implements connection caching with periodic refresh and validation.
        """
        if node_name not in self._node_configs:
            raise LXDConnectionError(f"Unknown node: {node_name}")
        
        # Check if we need to refresh the connection
        should_refresh = self._should_refresh_connection(node_name)
        
        # Return cached client if available, not expired, and still connected
        if node_name in self._clients and not should_refresh:
            if self._validate_connection(self._clients[node_name], node_name):
                return self._clients[node_name]
            else:
                # Connection validation failed, remove from cache and close
                logger.warning(f"Connection to {node_name} is stale, reconnecting...")
                try:
                    # Try to close the old connection gracefully
                    if hasattr(self._clients[node_name], '_api_session'):
                        self._clients[node_name]._api_session.close()
                except Exception as close_err:
                    logger.debug(f"Error closing stale connection: {close_err}")
                del self._clients[node_name]
                if node_name in self._connection_times:
                    del self._connection_times[node_name]
        elif should_refresh and node_name in self._clients:
            # Connection is too old, force refresh
            logger.info(f"Connection to {node_name} is old (>30min), refreshing...")
            try:
                if hasattr(self._clients[node_name], '_api_session'):
                    self._clients[node_name]._api_session.close()
            except Exception as close_err:
                logger.debug(f"Error closing old connection: {close_err}")
            del self._clients[node_name]
            if node_name in self._connection_times:
                del self._connection_times[node_name]
        
        config = self._node_configs[node_name]
        
        try:
            # Try certificate-based authentication first
            client = pylxd.Client(
                endpoint=config.endpoint,
                cert=(config.cert_path, config.key_path),
                verify=config.verify,
                timeout=(10, 30)  # 10s connect timeout, 30s read timeout
            )
            
            # Verify connection with timeout
            client.api.get(timeout=(5, 10))
            
            self._clients[node_name] = client
            self._connection_times[node_name] = time.time()
            logger.info(f"Connected to LXD node: {node_name} at {config.endpoint}")
            return client
            
        except Exception as e:
            # Try trust password if certificate fails
            trust_password = (
                self.settings.lxd_node1_trust_password 
                if node_name == self.settings.lxd_node1_name 
                else self.settings.lxd_node2_trust_password
            )
            
            if trust_password:
                try:
                    client = pylxd.Client(
                        endpoint=config.endpoint,
                        verify=config.verify,
                        timeout=(10, 30)
                    )
                    client.authenticate(trust_password)
                    client.api.get(timeout=(5, 10))
                    self._clients[node_name] = client
                    self._connection_times[node_name] = time.time()
                    logger.info(
                        f"Connected to LXD node via trust password: {node_name}"
                    )
                    return client
                except Exception as auth_error:
                    logger.error(
                        f"Trust password auth failed for {node_name}: {auth_error}"
                    )
            
            logger.error(f"Failed to connect to LXD node {node_name}: {e}")
            raise LXDConnectionError(
                f"Cannot connect to {node_name}: {str(e)}"
            )
    
    def get_node_names(self) -> list:
        """Get list of configured node names."""
        return list(self._node_configs.keys())
    
    def get_node_config(self, node_name: str) -> Optional[LXDNodeConfig]:
        """Get configuration for a specific node."""
        return self._node_configs.get(node_name)
    
    def cleanup_connection(self, node_name: str) -> None:
        """Cleanup a specific connection."""
        if node_name in self._clients:
            try:
                if hasattr(self._clients[node_name], '_api_session'):
                    self._clients[node_name]._api_session.close()
            except Exception as e:
                logger.debug(f"Error closing connection for {node_name}: {e}")
            del self._clients[node_name]
        if node_name in self._connection_times:
            del self._connection_times[node_name]
