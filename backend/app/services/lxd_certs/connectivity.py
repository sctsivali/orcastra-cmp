"""Connecting to a cluster and observing whether it answers and trusts us."""

import logging
from typing import Optional, Tuple

import requests
from pylxd import Client

from app.core.url_safety import normalize_endpoint
from app.services.lxd import cert_files, health
from app.services.lxd.connection import DEFAULT_LXD_TIMEOUT, FingerprintHTTPAdapter
from app.services.lxd.exceptions import LXDLocalStorageError

from .pinning import PinState
from .types import ConnectionProbe, LXDCertManagerError

logger = logging.getLogger(__name__)


class ConnectivityMixin:
    """Expects ``self.vault``, ``self.clusters_path`` and the storage mixin's readers."""

    def connect_with_vault_cert(
        self, cluster_id: str, connection_manager=None, force: bool = False
    ) -> Optional[Client]:
        """
        Connect to LXD using certificate from Vault.
        
        If connection_manager is provided, delegates to it for proper
        connection pooling and lifecycle management. Otherwise creates standalone client.

        Returns None when the cluster is currently tripped in the breaker, unless ``force``.

        important: checked here, before the Vault reads, because those are two HTTP round
        trips paid before the LXD handshake even starts. Callers reach this path when
        get_client already returned None, so without this gate a dead cluster costs MORE per
        request after the breaker trips, not less.

        ``force`` is for registration, auth checks and certificate rotation: the operations
        that repair a broken cluster, which would be unreachable if the breaker sealed them.
        """
        if not force and health.is_tripped(cluster_id):
            logger.debug(
                "Skipping Vault connect to %s: recently unreachable (%s)",
                cluster_id, health.get_reason(cluster_id),
            )
            return None
        try:
            # Get metadata and certificate from Vault
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")
            cert_data = self.get_cluster_certificate(cluster_id)
            
            endpoint = normalize_endpoint(metadata["endpoint"])
            cert_content = cert_data["client.crt"]
            key_content = cert_data["client.key"]
            
            # Use connection_manager if provided (proper pooling)
            if connection_manager is not None:
                return connection_manager.connect_with_vault_cert(
                    cluster_id=cluster_id,
                    endpoint=endpoint,
                    cert_content=cert_content,
                    key_content=key_content,
                    verify=False,
                    force=force,
                )
            
            # Fallback: Create standalone client (for one-off operations)
            # Note: This path doesn't benefit from connection pooling
            # important: these files must outlive this function - pylxd opens them again on every
            # later request, so a finally block that deleted them would break the client it just
            # returned. That constraint is why this used to mkstemp a fresh randomly named pair
            # per call and never clean up, which is an unbounded leak into a 256 MiB tmpfs. The
            # materializer keeps the files alive exactly as long as pylxd needs while bounding
            # them at one pair per cluster, because the path is derived from the cluster id.
            cert_path, key_path = cert_files.materialize(cluster_id, cert_content, key_content)
            
            # important: without an explicit timeout pylxd stores _timeout=None, so its own
            # constructor probe and every later client.api call on this standalone client
            # block forever against a peer that accepts TCP then goes silent. Matches the
            # value LXDConnectionManager builds pooled clients with.
            client = Client(
                endpoint=endpoint,
                cert=(cert_path, key_path),
                verify=False,
                timeout=DEFAULT_LXD_TIMEOUT
            )

            logger.info(f"Created standalone connection to {cluster_id}")
            return client

        except LXDLocalStorageError:
            # important: above the generic handler on purpose. Rewritten as "Connection failed"
            # this reached the operator as a cluster problem, which is how a full backend /tmp
            # came to be read as "Not Authenticated Yet" against a healthy hypervisor.
            logger.error(f"Cannot prepare TLS credentials for {cluster_id}: backend storage")
            raise
        except Exception as e:
            # important: WARNING, not ERROR. See connection.py: an offline cluster is
            # expected, and the caller still gets the exception to act on.
            logger.warning(f"Failed to connect to cluster {cluster_id}: {e}")
            raise LXDCertManagerError(f"Connection failed: {str(e)}")
    
    def verify_connection(self, cluster_id: str, use_breaker: bool = True) -> Tuple[bool, str]:
        """Whether the certificate is trusted, and why not when it is not.

        Thin wrapper over probe_connection, kept because five call sites want the bool.
        """
        # note: the storage fault is absorbed here on purpose. probe_connection raises it so a
        # single-cluster endpoint can answer 503, but this wrapper is called inside per-cluster
        # loops that build warning lists, and letting it escape would drop the whole list.
        try:
            probe = self.probe_connection(cluster_id, use_breaker=use_breaker)
        except LXDLocalStorageError:
            return False, "Could not verify the connection to this cluster."
        return probe.trusted, probe.message

    def probe_connection(self, cluster_id: str, use_breaker: bool = True) -> ConnectionProbe:
        """
        Probe an LXD cluster: did it answer, and does it trust our certificate.
        
        Tests trust by attempting to access protected resources that require authentication.
        
        Uses temporary files that are immediately cleaned up after verification.
        Certificate data is fetched fresh from Vault each time.
        """
        # important: shared circuit breaker - skip clusters that failed to connect
        # within the TTL window so a dead cluster does not cost the full connect
        # timeout on every dashboard fan-out. User-initiated checks pass
        # use_breaker=False to force a fresh probe.
        from app.services.lxd.health import (
            KIND_UNAUTHORIZED,
            clear,
            get_kind,
            get_reason,
            is_tripped,
            record_failure,
        )
        if use_breaker and is_tripped(cluster_id):
            return ConnectionProbe(
                reachable=get_kind(cluster_id) == KIND_UNAUTHORIZED,
                trusted=False,
                message=get_reason(cluster_id) or "Cluster recently unreachable - retry shortly",
            )

        try:
            # Get cluster metadata and certificate fresh from Vault
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")
            cert_data = self.get_cluster_certificate(cluster_id)
            
            endpoint = normalize_endpoint(metadata["endpoint"])
            
            # important: the shared materializer, not a per-probe mkstemp. Two reasons. It is
            # the same bounded pair the connection manager uses, so probing a cluster no longer
            # writes a file per call. And it raises LXDLocalStorageError on a full disk, which is
            # what lets the handler below tell "the backend could not write" apart from "the
            # cluster did not answer". A raw OSError here fell into the generic branch and the
            # caller derived OFFLINE from it, so a healthy hypervisor read as down.
            cert_path, key_path = cert_files.materialize(
                cluster_id, cert_data["client.crt"], cert_data["client.key"]
            )
            
            # Test authenticated endpoint that requires trust
            # `/1.0/instances` requires authentication, `/1.0` doesn't always
            test_url = f"{endpoint}/1.0/instances"
            
            # Fail fast: short connect budget (3s) + bounded read (8s). Retry once
            # on a timeout, but never retry a refused/DNS failure - an unreachable
            # host must not cost 45s on every dashboard fan-out request.
            last_error = None
            response = None
            # important: held to the registered certificate pin when there is one, like every
            # other connection to the cluster (connection.FingerprintHTTPAdapter). A bare
            # requests.get with verify=False presented our client certificate to whatever
            # answered the endpoint. No pin yet (a cluster never connected): unchanged, since a
            # probe must not be the thing that mints one.
            get = requests.get
            session = None
            pin = PinState.from_metadata(metadata)
            if pin is not None and pin.fingerprint:
                session = requests.Session()
                session.mount("https://", FingerprintHTTPAdapter(server_fingerprint=pin.fingerprint))
                get = session.get
            try:
                for attempt in range(2):
                    try:
                        response = get(
                            test_url,
                            cert=(cert_path, key_path),
                            verify=False,
                            timeout=(3, 8)
                        )
                        break  # got an HTTP response, exit retry loop
                    except requests.exceptions.SSLError:
                        # important: SSLError subclasses ConnectionError, so without this it was
                        # recorded as an offline host below. A refused handshake, a pin mismatch
                        # included, is a certificate problem: the outer SSLError branch says so.
                        raise
                    except requests.exceptions.Timeout as e:
                        last_error = e
                        logger.warning(f"Timeout on attempt {attempt + 1} for {cluster_id}")
                        continue
                    except requests.exceptions.ConnectionError as e:
                        last_error = e
                        logger.warning(f"Connection error for {cluster_id}: {e}")
                        break  # unreachable host - do not retry
            finally:
                if session is not None:
                    session.close()

            if response is None:
                # important: WARNING, not ERROR. Reached only after the retries above have
                # already logged the same host at WARNING; escalating the summary line for
                # a cluster that is merely offline is what makes ERROR unreadable.
                logger.warning(f"Connection failed for {cluster_id}: {last_error}")
                record_failure(cluster_id, "Cannot connect to LXD endpoint. Check if server is running.")
                return ConnectionProbe(False, False, "Cannot connect to LXD endpoint. Check if server is running.")
            
            # Host is reachable (we got an HTTP response) - clear any breaker trip.
            clear(cluster_id)

            # Check response status
            if response.status_code == 200:
                # Successfully authenticated
                logger.info(f"Certificate trusted and authenticated for {cluster_id}")
                return ConnectionProbe(True, True, "Certificate trusted and authenticated")
            elif response.status_code == 403:
                # Certificate not trusted
                logger.warning(f"Certificate not trusted for {cluster_id}")
                return ConnectionProbe(
                    True, False, "Certificate not trusted by hypervisor. Add trust via CLI."
                )
            else:
                logger.warning(f"Unexpected status {response.status_code} for {cluster_id}")
                return ConnectionProbe(True, False, f"Unexpected response: HTTP {response.status_code}")
            
        except requests.exceptions.SSLError as e:
            logger.error(f"SSL error for {cluster_id}: {e}")
            # note: reachable=True. A TLS failure means the socket was answered and the
            # handshake was refused, which is a trust problem somebody has to fix, not a
            # host that is down. requests.SSLError subclasses ConnectionError, so this
            # branch must stay above the ConnectionError one below.
            return ConnectionProbe(True, False, "Certificate error. Verify certificate is valid.")
        except requests.exceptions.ConnectionError as e:
            # important: WARNING. Refused or unresolvable is an offline cluster, and the
            # breaker below is what the product acts on. The SSL branch above stays at
            # ERROR on purpose: an untrusted or expired certificate is a state somebody
            # has to fix, not one that clears itself when the host comes back.
            logger.warning(f"Connection error for {cluster_id}: {e}")
            record_failure(cluster_id, "Cannot connect to LXD endpoint. Check if server is running.")
            return ConnectionProbe(False, False, "Cannot connect to LXD endpoint. Check if server is running.")
        except requests.exceptions.Timeout as e:
            logger.warning(f"Timeout for {cluster_id}: {e}")
            record_failure(cluster_id, "Connection timeout. Check network connectivity.")
            return ConnectionProbe(False, False, "Connection timeout. Check network connectivity.")
        except LXDLocalStorageError:
            # important: raised, not folded into a probe result. Every ConnectionProbe this
            # function can return is a claim about the cluster, and there is no true claim to
            # make: the peer was never contacted. Returning reachable=None sent derive_status
            # down its "default to OFFLINE" path, which is how a full backend tmpfs came to
            # label healthy hypervisors down. The breaker is deliberately left untouched.
            logger.error(f"Cannot prepare TLS credentials for {cluster_id}: backend storage")
            raise
        except Exception as e:
            # important: no raw exception text in the message. It reaches ClusterInfo.error_message
            # on /clusters, which every role down to tenant can read, and a Vault failure here
            # carries the Vault base URL and the KV path to the cluster's certificates.
            logger.error(f"Verification failed for {cluster_id}: {e}")
            return ConnectionProbe(None, False, "Could not verify the connection to this cluster.")
