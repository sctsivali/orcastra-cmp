"""
LXD client connections: pooling, certificate pinning, the reachability breaker and reconnects.
"""

import logging
import time
import os
import atexit
import gc
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests
import pylxd
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import get_settings
from . import breaker_sync, cert_files, health, telemetry
from .cert_rotation import CertificateRotationMixin
from .pin_enforcement import CertificatePinMixin
from .exceptions import (
    LXDAuthorizationError,
    LXDConnectionError,
    LXDLocalStorageError,
    LXDTransportPinError,
)

logger = logging.getLogger(__name__)

# (connect, read) seconds, the timeout pylxd clients are constructed with below.
DEFAULT_LXD_TIMEOUT: Tuple[int, int] = (3, 8)


def normalize_server_fingerprint(value: Optional[str]) -> str:
    """Normalize the common SHA-256 display forms for an equality check."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace(":", "")


def _read_retries_enabled() -> bool:
    """Whether a read timeout should be retried by the pooled adapter.

    Off by default. Retrying a read timeout multiplies wall clock by the attempt count: against
    a peer that accepts TCP then goes silent, Retry(total=3) turns an 8s read timeout into 35s
    of thread time. A read timeout against LXD is rarely transient, and retrying a POST read
    timeout can execute an instance create twice.

    Set ORCASTRA_LXD_RETRY_READ=true to enable read retries without a rebuild.
    """
    return os.environ.get("ORCASTRA_LXD_RETRY_READ", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


class TimeoutHTTPAdapter(HTTPAdapter):
    """HTTPAdapter that applies a default timeout when the caller supplied none.

    important: this bounds every pylxd call in the process. pylxd only injects its configured
    timeout when a call goes through client.api.<verb> (_APINode.get does
    kwargs["timeout"] = kwargs.get("timeout", self._timeout)). A call on client.api.session
    directly has no default and would block forever against a silent peer.
    """

    def __init__(self, *args, default_timeout: Tuple[int, int] = DEFAULT_LXD_TIMEOUT, **kwargs):
        self.default_timeout = default_timeout
        super().__init__(*args, **kwargs)

    def send(self, request, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.default_timeout
        return super().send(request, *args, **kwargs)


class FingerprintHTTPAdapter(TimeoutHTTPAdapter):
    """Apply the registered server-certificate fingerprint on the real HTTP socket.

    The enrollment probe keeps an obviously changed certificate from receiving the client
    credential, but cannot close the gap between that probe and the next request. urllib3's
    ``assert_fingerprint`` repeats the comparison on every pooled connection, so it stays
    authoritative for the transport that carries the request.
    """

    def __init__(self, *args, server_fingerprint: Optional[str] = None, **kwargs):
        self.server_fingerprint = server_fingerprint or None
        self._required_server_fingerprint: Optional[str] = None
        super().__init__(*args, **kwargs)

    def require_server_fingerprint(self, expected_fingerprint: str) -> None:
        """Bind this adapter to an already approved migration fingerprint.

        The adapter's regular ``assert_fingerprint`` still validates the peer certificate on
        every new socket. This additional binding proves that the adapter was configured from
        the exact plan/job pin before any Migration Center request is allowed to use it.
        """
        expected = normalize_server_fingerprint(expected_fingerprint)
        configured = normalize_server_fingerprint(self.server_fingerprint)
        expected_is_sha256 = len(expected) == 64 and all(
            value in "0123456789abcdef" for value in expected
        )
        if not expected_is_sha256 or configured != expected:
            raise LXDTransportPinError(
                "The cluster HTTPS transport is not bound to the approved certificate."
            )
        self._required_server_fingerprint = expected

    def send(self, request, *args, **kwargs):
        required = self._required_server_fingerprint
        if required is not None:
            if normalize_server_fingerprint(self.server_fingerprint) != required:
                raise LXDTransportPinError(
                    "The cluster HTTPS transport certificate binding changed."
                )
            if urlparse(str(getattr(request, "url", ""))).scheme.lower() != "https":
                raise LXDTransportPinError(
                    "Migration Center refuses a non-HTTPS daemon request."
                )
        return super().send(request, *args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        if self.server_fingerprint:
            pool_kwargs["assert_fingerprint"] = self.server_fingerprint
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        if self.server_fingerprint:
            proxy_kwargs["assert_fingerprint"] = self.server_fingerprint
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def _reject_lxd_redirect(response, *args, **kwargs):
    """Reject redirects before requests can replay a credentialed mutation elsewhere."""
    del args, kwargs
    status_code = int(getattr(response, "status_code", 0) or 0)
    if 300 <= status_code < 400:
        response.close()
        raise LXDConnectionError(
            "The registered cluster endpoint returned a redirect, which is not allowed."
        )
    return response


def _install_lxd_response_guards(session: requests.Session) -> None:
    hooks = session.hooks.setdefault("response", [])
    if _reject_lxd_redirect not in hooks:
        hooks.append(_reject_lxd_redirect)


def require_https_session_fingerprint(
    session: requests.Session,
    endpoint: str,
    expected_fingerprint: str,
) -> FingerprintHTTPAdapter:
    """Fail closed unless the actual HTTPS adapter carries the approved pin."""
    parsed = urlparse(endpoint)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise LXDTransportPinError(
            "Migration Center requires a registered HTTPS cluster endpoint."
        )
    try:
        adapter = session.get_adapter(endpoint)
    except Exception as exc:
        raise LXDTransportPinError(
            "The cluster HTTPS session has no verifiable transport adapter."
        ) from exc
    if not isinstance(adapter, FingerprintHTTPAdapter):
        raise LXDTransportPinError(
            "The cluster HTTPS session does not use the fingerprint-pinning adapter."
        )
    adapter.require_server_fingerprint(expected_fingerprint)
    return adapter


def _client_adapter_fingerprint(client: Optional[pylxd.Client]) -> Optional[str]:
    """The server fingerprint a pooled client's HTTPS adapter asserts on every socket, if any."""
    api = getattr(client, "api", None)
    endpoint = getattr(api, "_api_endpoint", None)
    session = getattr(api, "session", None)
    if not isinstance(endpoint, str) or session is None:
        return None
    try:
        adapter = session.get_adapter(endpoint)
    except Exception:
        return None
    if not isinstance(adapter, FingerprintHTTPAdapter):
        return None
    return normalize_server_fingerprint(adapter.server_fingerprint) or None


def require_client_server_fingerprint(client: pylxd.Client, expected_fingerprint: str) -> None:
    """Bind a pylxd client's real session to a persisted migration fingerprint."""
    api = getattr(client, "api", None)
    endpoint = getattr(api, "_api_endpoint", None)
    session = getattr(api, "session", None)
    if not isinstance(endpoint, str) or not endpoint or session is None:
        raise LXDTransportPinError(
            "The cluster client does not expose a verifiable HTTPS session."
        )
    require_https_session_fingerprint(session, endpoint, expected_fingerprint)


class LXDConnectionManager(CertificateRotationMixin, CertificatePinMixin):
    """
    Manages LXD client connections with pooling and automatic reconnection.
    """
    
    # Connection pool settings for parallel operations
    POOL_CONNECTIONS = 50   # Number of connection pools to cache
    POOL_MAXSIZE = 100      # Maximum connections per pool (> ThreadPoolExecutor workers)
    
    def __init__(self):
        self.settings = get_settings()
        self._clients: Dict[str, pylxd.Client] = {}
        # Track connection creation time (for logging only, not TTL)
        self._connection_times: Dict[str, float] = {}
        # Full connection info, cert content included, so a reconnect does not read Vault again.
        # {"cluster_id": {"endpoint": str, "cert_content": str, "key_content": str, "verify": bool}}
        self._connection_info: Dict[str, Dict[str, str]] = {}
        # Track temp file paths for cleanup
        self._temp_cert_paths: Dict[str, Tuple[str, str]] = {}
        # Vault-registered cluster whitelist. Only these clusters are visible through
        # get_node_names(), so stale or unregistered nodes never reach API responses.
        self._registered_clusters: Optional[set] = None
        # Clusters this worker has tried to connect to since it started, whatever the outcome.
        # A whitelisted cluster missing here has not been tried yet (registered seconds ago,
        # connected by the next healer tick); one here without a client is known to be down.
        # The list routers cache a body naming a known-down cluster but never one naming an
        # untried cluster, because the cache is shared by every worker.
        self._attempted: Set[str] = set()
        # (count, first_at) of tolerated read timeouts of the validation probe, per cluster.
        # See _validate_connection; cleared whenever the client is evicted or rebuilt.
        self._slow_validations: Dict[str, Tuple[int, float]] = {}
        
        atexit.register(self._cleanup_all_temp_files)
        atexit.register(self._secure_cleanup_credentials)
    
    def _configure_client_session(
        self,
        client: pylxd.Client,
        server_fingerprint: Optional[str] = None,
        *,
        required_server_fingerprint: Optional[str] = None,
        cluster_id: Optional[str] = None,
    ) -> None:
        """
        Mount the pooled, fingerprint-pinned adapter and the response guards on the session.
        The larger pool prevents 'Connection pool is full' warnings during parallel operations.
        """
        try:
            # pylxd uses client.api.session internally
            if hasattr(client, 'api') and hasattr(client.api, 'session'):
                session = client.api.session
                
                retry_strategy = Retry(
                    total=3,
                    connect=0,
                    read=None if _read_retries_enabled() else 0,
                    backoff_factor=0.5,
                    status_forcelist=[429, 500, 502, 503, 504],
                    # important: never retry a mutating method. A 5xx or lost response is an
                    # unknown outcome, not proof that LXD did not accept the create, state
                    # change, restore or delete. Durable callers must reconcile the operation
                    # and resource before deciding whether another submission is safe.
                    allowed_methods=["HEAD", "GET", "OPTIONS"],
                    raise_on_status=False
                )

                adapter = FingerprintHTTPAdapter(
                    max_retries=retry_strategy,
                    pool_connections=self.POOL_CONNECTIONS,
                    pool_maxsize=self.POOL_MAXSIZE,
                    pool_block=False,  # Don't block, just create new connection
                    server_fingerprint=server_fingerprint,
                )
                if required_server_fingerprint:
                    adapter.require_server_fingerprint(required_server_fingerprint)
                
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                _install_lxd_response_guards(session)
                telemetry.install_slow_request_log(session, cluster_id)
                logger.debug(f"Configured client session with pool_maxsize={self.POOL_MAXSIZE}")
            if required_server_fingerprint:
                require_client_server_fingerprint(
                    client, required_server_fingerprint
                )
        except LXDTransportPinError:
            raise
        except Exception as e:
            if required_server_fingerprint:
                raise LXDTransportPinError(
                    "The cluster HTTPS session could not be bound to the approved certificate."
                ) from e
            logger.debug(f"Could not configure client session: {e}")
    
    def _create_http_session(
        self,
        server_fingerprint: Optional[str] = None,
        *,
        required_server_fingerprint: Optional[str] = None,
    ):
        """Create HTTP session with retry configuration."""
        session = requests.Session()
        
        retry_strategy = Retry(
            total=3,
            connect=0,
            read=None if _read_retries_enabled() else 0,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            # Mutation submissions are not replayed. See the pooled-client guard above.
            allowed_methods=["HEAD", "GET", "OPTIONS"],
            raise_on_status=False
        )

        adapter = FingerprintHTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=self.POOL_CONNECTIONS,
            pool_maxsize=self.POOL_MAXSIZE,
            pool_block=False,
            server_fingerprint=server_fingerprint,
        )
        if required_server_fingerprint:
            adapter.require_server_fingerprint(required_server_fingerprint)
        
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({
            'Connection': 'keep-alive',
            'Keep-Alive': 'timeout=600, max=1000'
        })
        _install_lxd_response_guards(session)
        
        return session
    
    # A probe that times out on the read has completed TCP and TLS: the daemon is there and
    # busy, so the first such timeout keeps the pooled client. A second read timeout at least
    # SLOW_VALIDATION_SPACING seconds after the first means a wedged daemon and fails
    # validation; a burst of parallel callers hitting one slow moment counts as one. Connect,
    # TLS and untrusted answers always fail validation.
    SLOW_VALIDATION_TOLERANCE = 2
    SLOW_VALIDATION_SPACING = 5.0

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
                    # important: pylxd captures host_info once in __init__ and only rewrites it
                    # inside authenticate(), and this pool never expires a client, so without
                    # this every reader of client.host_info would see the server version, auth
                    # and environment from the first connect. Refreshing it from this live /1.0
                    # read is what pylxd does after authenticating.
                    metadata = data.get("metadata")
                    if metadata:
                        client.host_info = metadata
                    self._slow_validations.pop(node_name, None)
                    return True
                else:
                    logger.warning(f"Connection to {node_name} not trusted (auth={auth_status})")
                    return False
            return True
        except Exception as e:
            from app.services.lxd.fanout import is_read_timeout

            if is_read_timeout(e):
                now = time.time()
                count, first_at = self._slow_validations.get(node_name, (0, now))
                if count == 0 or (now - first_at) < self.SLOW_VALIDATION_SPACING:
                    # first slow probe, or one of a burst landing on the same slow moment
                    self._slow_validations[node_name] = (max(count, 1), first_at if count else now)
                    logger.info(
                        "Validation of %s timed out on the read; TCP and TLS completed, keeping "
                        "the pooled client (evicts on a second timeout %.0fs or more later)",
                        node_name, self.SLOW_VALIDATION_SPACING,
                    )
                    return True
                self._slow_validations.pop(node_name, None)
            else:
                self._slow_validations.pop(node_name, None)
            logger.warning(f"Connection validation failed for {node_name}: {e}")
            return False
    
    def _create_temp_cert_files(self, cluster_id: str, cert_content: str, key_content: str) -> Tuple[str, str]:
        """
        Write the cluster's certificate to disk for pylxd, at 0600.

        important: the path is a pure function of the cluster id, so concurrent callers converge
        on one pair of files. Randomly named pairs recorded in the single slot below would orphan
        every pair but the last and fill the tmpfs. See cert_files for the mechanics.
        """
        cert_path, key_path = cert_files.materialize(cluster_id, cert_content, key_content)
        self._temp_cert_paths[cluster_id] = (cert_path, key_path)
        logger.debug(f"Materialized cert files for {cluster_id}")
        return cert_path, key_path
    
    def _cleanup_temp_cert_files(self, cluster_id: str) -> None:
        """
        Clean up temporary certificate files.
        Overwrites file content before deletion.
        """
        # important: claim the entry atomically with pop. `_temp_cert_paths` is a plain dict
        # with no lock and get_client runs in a thread pool, so a membership test followed by
        # `del` lets two threads pass and the loser raise KeyError inside _auto_reconnect's
        # failure handler (a 500 for an unreachable cluster). pop hands the paths to one caller.
        paths = self._temp_cert_paths.pop(cluster_id, None)
        if paths is None:
            return

        cert_files.discard(*paths)
        logger.debug(f"Cleaned temp cert files for {cluster_id}")
    
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
        verify: bool = False,
        force: bool = False,
        expected_server_fingerprint: Optional[str] = None,
    ) -> Optional[pylxd.Client]:
        """
        Create LXD client connection using certificate content from Vault.

        Returns None when the cluster is currently tripped in the breaker, unless ``force``.

        important: this is the second way to connect to a cluster, and the expensive one (TLS
        handshake plus a verifying probe, usually after a Vault read). Callers fall back here
        when get_client returns None, so gating only get_client would route every request into
        the costlier path. Both have to be gated or neither.

        ``force`` is for the flows that must reach a cluster the breaker considers dead:
        registration, auth checks and certificate rotation. They repair a broken cluster, so
        refusing them would make the breaker self-sealing.
        """
        self._attempted.add(cluster_id)
        if not force and health.is_tripped(cluster_id):
            logger.debug(
                "Skipping connect to %s: recently unreachable (%s)",
                cluster_id, health.get_reason(cluster_id),
            )
            return None
        dial_started = time.monotonic()
        try:
            # Keep the full connection info for auto-reconnect
            self._connection_info[cluster_id] = {
                "endpoint": endpoint,
                "cert_content": cert_content,
                "key_content": key_content,
                "verify": verify,
            }
            
            # No unlink before the write. The path is stable per cluster and the write lands
            # atomically, so removing it first would only open a window where a thread already
            # inside a handshake finds no file at all.
            cert_path, key_path = self._create_temp_cert_files(cluster_id, cert_content, key_content)

            # important: BEFORE the client is built, so a substituted endpoint never receives the
            # client certificate. LXD has no CA, so `verify=False` means no server
            # authentication at all; the pin replaces it: trust on first use, refuse on change.
            # services/lxd_certs/pinning.py explains why the operator does not type a fingerprint.
            server_fingerprint = (
                self._enforce_expected_certificate_pin(
                    cluster_id, endpoint, expected_server_fingerprint
                )
                if expected_server_fingerprint
                else self._enforce_certificate_pin(cluster_id, endpoint)
            )
            self._connection_info[cluster_id]["server_fingerprint"] = (
                server_fingerprint or ""
            )

            session = self._create_http_session(
                server_fingerprint,
                required_server_fingerprint=expected_server_fingerprint,
            )
            session.cert = (cert_path, key_path)
            session.verify = verify

            # Create LXD client with faster timeout for quick failure detection
            client = pylxd.Client(
                endpoint=endpoint,
                cert=(cert_path, key_path),
                verify=verify,
                timeout=(3, 8),
                session=session,
            )
            
            # Verify connection and authentication
            response = client.api.get(timeout=(2, 5))
            proven = False
            if response.status_code == 200:
                data = response.json()
                auth_status = data.get("metadata", {}).get("auth", "")
                if auth_status != "trusted":
                    raise LXDAuthorizationError(
                        f"Certificate not trusted by {cluster_id} (auth={auth_status})"
                    )
                proven = True
            
            # Configure session with larger connection pool for parallel operations
            self._configure_client_session(
                client,
                server_fingerprint,
                required_server_fingerprint=expected_server_fingerprint,
                cluster_id=cluster_id,
            )
            
            self._clients[cluster_id] = client
            self._connection_times[cluster_id] = time.time()
            self._slow_validations.pop(cluster_id, None)
            health.clear(cluster_id)
            if proven:
                # The other workers may still hold a trip for this cluster; see breaker_sync.
                breaker_sync.announce_proven(cluster_id)
            logger.info(
                f"Connected to cluster {cluster_id} using Vault certificate",
                extra=telemetry.dial_fields(cluster_id, dial_started, "ok"),
            )

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
                    
        except LXDTransportPinError:
            self._connection_info.pop(cluster_id, None)
            raise
        except LXDAuthorizationError:
            # Raised above for an untrusted certificate: the peer answered and refused it, and
            # keeps refusing until the certificate is rotated, so recording it spares later
            # requests the handshake. Kept narrow: a transport failure recorded as unauthorized
            # would show an offline cluster as Pending Trust for the breaker TTL.
            #
            # important: drop the cached credentials too, as the generic handler below does. The
            # background healer checks has_connection_info before the breaker (metrics_collector),
            # so a retained entry stops it from ever reaching Vault. Evicting costs one Vault read.
            self._connection_info.pop(cluster_id, None)
            health.record_failure(
                cluster_id, "certificate not trusted", kind=health.KIND_UNAUTHORIZED
            )
            raise
        except LXDLocalStorageError:
            # important: above the generic handler and not recorded. The peer was never
            # contacted, so record_failure here would mark a healthy cluster offline for the
            # breaker TTL, and a full tmpfs would read as a fleet-wide outage.
            logger.error(f"Cannot prepare TLS credentials for {cluster_id}: backend storage")
            raise
        except Exception as e:
            if cluster_id in self._connection_info:
                del self._connection_info[cluster_id]
            health.record_failure(cluster_id, f"connect failed: {type(e).__name__}")
            # Logged at WARNING: an offline cluster is expected operational noise, and anything
            # alerting on ERROR would page for a host the operator already knows is down.
            logger.warning(
                f"Failed to connect to cluster {cluster_id}: {e}",
                extra=telemetry.dial_fields(cluster_id, dial_started, "offline"),
            )
            raise LXDConnectionError(f"Cannot connect to {cluster_id}: {str(e)}")
    
    def peek_client(self, cluster_id: str) -> Optional[pylxd.Client]:
        """The pooled client as it is: no validation round trip, no connect.

        For reading what the host already said about itself (pylxd's cached `host_info`), never
        for sending a request; get_client is the way to a client that is known to answer.
        """
        return self._clients.get(cluster_id)

    def get_client(
        self,
        cluster_id: str,
        *,
        expected_server_fingerprint: Optional[str] = None,
    ) -> Optional[pylxd.Client]:
        """
        Get cached LXD client for the specified cluster.
        
        Reconnection:
        - If the client exists and validates, return it
        - If validation fails, auto-reconnect using stored credentials
        - No TTL-based refresh: connections persist while valid

        Returns None if the cluster is not in the current user's org scope, or if the cluster
        recently failed to connect.
        """
        # Org-scope filter: deny access to clusters outside the caller's org. Callers turn None
        # into 503 "Could not connect", so the refusal is logged to tell it from an outage.
        # verify_cluster_access answers 403 first at every cluster-taking handler
        # (test_cluster_scope_ratchet.py), so reaching here means a caller skipped it.
        from app.core.org_scope import get_current_allowed_clusters
        allowed = get_current_allowed_clusters()
        if allowed is not None and cluster_id not in allowed:
            logger.debug(
                "Refusing %s: outside the caller's organization scope", cluster_id
            )
            return None

        # important: the breaker belongs here, at the chokepoint every caller goes through. This
        # method revalidates with a live request and may rebuild the client, which costs seconds
        # against an unreachable peer; most callers await it inline in `async def` handlers, so
        # without this each request to a dead cluster stalls the event loop. Every caller
        # already handles None.
        if health.is_tripped(cluster_id):
            logger.debug(
                "Skipping %s: recently unreachable (%s)", cluster_id, health.get_reason(cluster_id)
            )
            return None

        # important: one read of the pool. Validation is a live round trip and the pool is shared by
        # every thread in this worker, so a concurrent cleanup_connection between a check and a
        # second `self._clients[cluster_id]` would raise KeyError on a client that just answered.
        pooled = self._clients.get(cluster_id)
        if pooled is not None and expected_server_fingerprint:
            cached_pin = _client_adapter_fingerprint(pooled)
            if cached_pin != normalize_server_fingerprint(expected_server_fingerprint):
                # A client pooled before a public-CA renewal holds the superseded pin, and one
                # pooled before the first pin holds none. The reconnect below verifies the live
                # certificate against the expected one before any client credential is sent, so
                # dropping the stale client is safe.
                logger.info("Reconnecting %s: its pooled client predates the current pin", cluster_id)
                self.cleanup_connection(cluster_id)
                pooled = None

        if pooled is not None:
            if expected_server_fingerprint:
                require_client_server_fingerprint(pooled, expected_server_fingerprint)
            if self._validate_connection(pooled, cluster_id):
                return pooled
            else:
                logger.warning(f"Connection to {cluster_id} failed validation, reconnecting...")
                self.cleanup_connection(cluster_id)
                return (
                    self._auto_reconnect(cluster_id)
                    if expected_server_fingerprint is None
                    else self._auto_reconnect(
                        cluster_id,
                        expected_server_fingerprint=expected_server_fingerprint,
                    )
                )
        else:
            # Not pooled: reconnect from stored connection info, if any
            return (
                self._auto_reconnect(cluster_id)
                if expected_server_fingerprint is None
                else self._auto_reconnect(
                    cluster_id,
                    expected_server_fingerprint=expected_server_fingerprint,
                )
            )

    def get_cached_client(self, cluster_id: str) -> Optional[pylxd.Client]:
        """Return an already connected client without probing or reconnecting.

        Used by request paths whose own scoped LXD read is the liveness probe and whose wall
        budget cannot also contain ``get_client`` validation/reconnect calls. Scope and breaker
        checks remain mandatory even though this is a memory-only lookup.
        """
        from app.core.org_scope import get_current_allowed_clusters

        allowed = get_current_allowed_clusters()
        if allowed is not None and cluster_id not in allowed:
            return None
        if health.is_tripped(cluster_id):
            return None
        return self._clients.get(cluster_id)

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
            server_fingerprint = info.get("server_fingerprint") or None
            session = self._create_http_session(server_fingerprint)
            session.cert = (cert_path, key_path)
            session.verify = info.get("verify", False)
            return pylxd.Client(
                endpoint=info["endpoint"],
                cert=(cert_path, key_path),
                verify=info.get("verify", False),
                timeout=timeout,
                project=project or "default",
                session=session,
            )
        except Exception as e:
            logger.error(
                f"Failed to build project-scoped client for {cluster_id}/{project}: {e}"
            )
            return None

    def _auto_reconnect(
        self,
        cluster_id: str,
        *,
        expected_server_fingerprint: Optional[str] = None,
    ) -> Optional[pylxd.Client]:
        """
        Reconnect using the connection info stored in memory.

        Memory only, except one Vault read when the cluster refuses the held certificate
        (see cert_rotation: another worker may have rotated it).
        """
        if cluster_id not in self._connection_info:
            # important: do NOT trip the breaker here. An empty in-process cache is the normal
            # state for a cluster registered after this worker booted, or one whose info a connect
            # error cleared. The repair is the Vault path in ensure_connection and the background
            # healer, both gated by this breaker, so tripping here would block them. Transport
            # failures below still record.
            logger.debug(f"No stored connection info for {cluster_id}, cannot auto-reconnect")
            return None
        
        dial_started = time.monotonic()
        try:
            info = self._connection_info[cluster_id]
            
            # Reconnect using stored credentials
            cert_path, key_path = self._create_temp_cert_files(
                cluster_id, 
                info["cert_content"], 
                info["key_content"]
            )
            
            server_fingerprint = (
                self._enforce_expected_certificate_pin(
                    cluster_id,
                    info["endpoint"],
                    expected_server_fingerprint,
                )
                if expected_server_fingerprint
                else (
                    self._enforce_certificate_pin(cluster_id, info["endpoint"])
                    or info.get("server_fingerprint")
                    or None
                )
            )
            info["server_fingerprint"] = server_fingerprint or ""
            session = self._create_http_session(
                server_fingerprint,
                required_server_fingerprint=expected_server_fingerprint,
            )
            session.cert = (cert_path, key_path)
            session.verify = info.get("verify", False)
            client = pylxd.Client(
                endpoint=info["endpoint"],
                cert=(cert_path, key_path),
                verify=info.get("verify", False),
                timeout=(3, 8),
                session=session,
            )
            
            # Verify authentication. pylxd.Client() has just read /1.0 under the (3, 8)
            # budget and holds the body in host_info; a second probe under a tighter budget
            # would give a busy daemon one more chance to look absent.
            data = {"metadata": getattr(client, "host_info", None) or {}}
            if not data["metadata"]:
                response = client.api.get(timeout=(3, 8))
                data = response.json() if response.status_code == 200 else {}
            if data:
                if data.get("metadata", {}).get("auth") != "trusted":
                    logger.warning(f"Reconnect failed: cert not trusted by {cluster_id}")
                    # A certificate rotated by another worker is one Vault read away; see
                    # cert_rotation. Same certificate or no answer from Vault: recorded below.
                    rotated = self._try_rotated_certificate(
                        cluster_id, info, expected_server_fingerprint=expected_server_fingerprint
                    )
                    if rotated is not None:
                        return rotated
                    # important: evict the cached credentials before recording. The client is
                    # rebuilt from _connection_info, so a refused certificate left there is
                    # re-presented and refused on every later attempt. It would also block the
                    # repair: the background healer checks has_connection_info before the breaker
                    # (metrics_collector), and ensure_connection is gated by the breaker tripped
                    # below. pop, not test-then-delete: these dicts are mutated from a thread pool.
                    self._connection_info.pop(cluster_id, None)
                    # Recorded rather than cleared. Callers derive reachability from
                    # KIND_UNAUTHORIZED, and clearing here would report a cluster that answered
                    # and refused the certificate as plain Offline. See test_connection_breaker's
                    # test_a_generic_failure_never_downgrades_an_authorization_one.
                    health.record_failure(
                        cluster_id, "certificate not trusted", kind=health.KIND_UNAUTHORIZED
                    )
                    return None

            # Same session setup as connect_with_vault_cert (pool size, retry policy, default
            # timeout), so a reconnected client runs the same HTTP stack as a fresh one.
            self._configure_client_session(
                client,
                server_fingerprint,
                required_server_fingerprint=expected_server_fingerprint,
                cluster_id=cluster_id,
            )
            self._clients[cluster_id] = client
            self._connection_times[cluster_id] = time.time()
            self._slow_validations.pop(cluster_id, None)
            health.clear(cluster_id)
            logger.info(
                f"Auto-reconnected to {cluster_id}",
                extra=telemetry.dial_fields(cluster_id, dial_started, "ok"),
            )

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
            
        except LXDTransportPinError:
            raise
        except LXDLocalStorageError:
            # See connect_with_vault_cert: not the cluster's fault, so it must not be recorded
            # against it. Raised rather than returned as None because None reads downstream as
            # "cluster unreachable".
            logger.error(f"Cannot prepare TLS credentials for {cluster_id}: backend storage")
            raise
        except Exception as e:
            logger.warning(
                f"Auto-reconnect to {cluster_id} failed: {e}",
                extra=telemetry.dial_fields(cluster_id, dial_started, "offline"),
            )
            health.record_failure(cluster_id, f"reconnect failed: {type(e).__name__}")

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
        # Every caller passes the whole fleet, and the 30 s sweep reaches every worker, so this
        # is where a worker learns a cluster was deregistered while its breaker still held it.
        health.forget_unregistered(self._registered_clusters)
        logger.debug(f"Synced registered clusters whitelist: {self._registered_clusters}")

    def register_cluster(self, cluster_id: str) -> None:
        """Add a single cluster to the registered whitelist.

        None means "unfiltered" (the boot sync failed). Turning it into a one-element set here
        would hide every other connected cluster in this worker.
        """
        if self._registered_clusters is not None:
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
        """True when this worker holds stored connection info for a cluster."""
        return cluster_id in self._connection_info

    def has_attempted(self, cluster_id: str) -> bool:
        """True once this worker has tried to connect to the cluster, whatever came of it."""
        return cluster_id in self._attempted
    
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
        # the tolerance describes the client that was just evicted, not the next one
        self._slow_validations.pop(cluster_id, None)
        # Keep _connection_info for auto-reconnect
    
    def remove_connection(self, cluster_id: str) -> None:
        """
        Completely remove a connection including all stored info.
        Use this when a cluster is deleted or deregistered.
        """
        self.cleanup_connection(cluster_id)
        
        self.unregister_cluster(cluster_id)
        
        # Clear credentials from memory
        if cluster_id in self._connection_info:
            info = self._connection_info[cluster_id]
            if "cert_content" in info:
                info["cert_content"] = ""
            if "key_content" in info:
                info["key_content"] = ""
            del self._connection_info[cluster_id]
        
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
