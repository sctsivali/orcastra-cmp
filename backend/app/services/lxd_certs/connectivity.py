"""Connecting to a cluster and observing whether it answers and trusts the dashboard."""

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
        """Connect to LXD with the cluster's certificate from Vault.

        With ``connection_manager`` the call delegates to it for pooling; otherwise it builds a
        standalone client. Returns None while the cluster is tripped in the breaker, unless
        ``force``.

        important: the breaker is checked before the Vault reads, two HTTP round trips paid
        before the LXD handshake starts. Callers reach this after get_client returned None, so
        without the gate a tripped cluster would cost more per request.

        ``force`` is for registration, auth checks and certificate rotation: the operations
        that repair a broken cluster and must stay reachable while the breaker is tripped.
        """
        if not force and health.is_tripped(cluster_id):
            logger.debug(
                "Skipping Vault connect to %s: recently unreachable (%s)",
                cluster_id, health.get_reason(cluster_id),
            )
            return None
        try:
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")
            cert_data = self.get_cluster_certificate(cluster_id)

            endpoint = normalize_endpoint(metadata["endpoint"])
            cert_content = cert_data["client.crt"]
            key_content = cert_data["client.key"]

            if connection_manager is not None:
                return connection_manager.connect_with_vault_cert(
                    cluster_id=cluster_id,
                    endpoint=endpoint,
                    cert_content=cert_content,
                    key_content=key_content,
                    verify=False,
                    force=force,
                )
            
            # Standalone client for one-off operations, outside the connection pool.
            # important: these files must outlive this function: pylxd reopens them on every
            # later request, so deleting them would break the client just returned. The
            # materializer derives the path from the cluster id, which bounds the tmpfs at one
            # pair per cluster.
            cert_path, key_path = cert_files.materialize(cluster_id, cert_content, key_content)
            
            # important: without an explicit timeout pylxd stores _timeout=None, so its own
            # constructor probe and every later client.api call on this standalone client
            # block forever against a peer that accepts TCP then goes silent. Same value as
            # the pooled clients LXDConnectionManager builds.
            client = Client(
                endpoint=endpoint,
                cert=(cert_path, key_path),
                verify=False,
                timeout=DEFAULT_LXD_TIMEOUT
            )

            logger.info(f"Created standalone connection to {cluster_id}")
            return client

        except LXDLocalStorageError:
            # important: keep this above the generic handler. Rewritten as "Connection failed",
            # a full backend /tmp would reach the operator as a cluster problem and read as
            # "Not Authenticated Yet" against a healthy hypervisor.
            logger.error(f"Cannot prepare TLS credentials for {cluster_id}: backend storage")
            raise
        except Exception as e:
            # Logged at WARNING: an offline cluster is expected (see connection.py), and the
            # caller still gets the exception to act on.
            logger.warning(f"Failed to connect to cluster {cluster_id}: {e}")
            raise LXDCertManagerError(f"Connection failed: {str(e)}")
    
    def verify_connection(self, cluster_id: str, use_breaker: bool = True) -> Tuple[bool, str]:
        """Whether the certificate is trusted, and why not when it is not.

        Thin wrapper over probe_connection for callers that want the bool.
        """
        # The storage fault is absorbed here. probe_connection raises it so a single-cluster
        # endpoint can answer 503, but this wrapper runs inside per-cluster loops that build
        # warning lists, and letting it escape would drop the whole list.
        try:
            probe = self.probe_connection(cluster_id, use_breaker=use_breaker)
        except LXDLocalStorageError:
            return False, "Could not verify the connection to this cluster."
        return probe.trusted, probe.message

    def probe_connection(self, cluster_id: str, use_breaker: bool = True) -> ConnectionProbe:
        """Probe an LXD cluster: did it answer, and does it trust the dashboard's certificate.

        Trust is tested against a resource that requires authentication. Metadata and
        certificate are read from Vault on every call, and the TLS files go through the shared
        per-cluster materializer. Raises LXDLocalStorageError when the backend cannot write them.
        """
        # important: shared circuit breaker. A cluster that failed to connect within the TTL
        # window is skipped, so a dead cluster does not cost the full connect timeout on every
        # dashboard fan-out. User-initiated checks pass use_breaker=False to force a fresh
        # probe.
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
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")
            cert_data = self.get_cluster_certificate(cluster_id)

            endpoint = normalize_endpoint(metadata["endpoint"])

            # important: use the shared materializer. It writes the same bounded pair per
            # cluster as the connection manager, and it raises LXDLocalStorageError on a full
            # disk, which lets the handler below tell "the backend could not write" from "the
            # cluster did not answer". A raw OSError would land in the generic branch, and the
            # caller would derive OFFLINE for a healthy hypervisor.
            cert_path, key_path = cert_files.materialize(
                cluster_id, cert_data["client.crt"], cert_data["client.key"]
            )
            
            # `/1.0/instances` requires a trusted client; `/1.0` does not always.
            test_url = f"{endpoint}/1.0/instances"

            # Fail fast: 3 s connect budget, 8 s read. Retry once on a timeout, never on a
            # refused or DNS failure, so an unreachable host does not cost 45 s on every
            # dashboard fan-out request.
            last_error = None
            response = None
            # important: held to the registered certificate pin when there is one, like every
            # other connection to the cluster (connection.FingerprintHTTPAdapter). Unpinned,
            # requests.get with verify=False presents the client certificate to whatever answers
            # the endpoint. With no pin yet (a cluster never connected) the probe stays unpinned,
            # since a probe must not mint one.
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
                        # important: SSLError subclasses ConnectionError, so without this it would
                        # be recorded as an offline host below. A refused handshake, pin mismatch
                        # included, is a certificate problem: the outer SSLError branch says so.
                        raise
                    except requests.exceptions.Timeout as e:
                        last_error = e
                        logger.warning(f"Timeout on attempt {attempt + 1} for {cluster_id}")
                        continue
                    except requests.exceptions.ConnectionError as e:
                        last_error = e
                        logger.warning(f"Connection error for {cluster_id}: {e}")
                        break  # unreachable host: do not retry
            finally:
                if session is not None:
                    session.close()

            if response is None:
                # Logged at WARNING: the retries above already logged this host at WARNING, and
                # an offline cluster does not belong in ERROR.
                logger.warning(f"Connection failed for {cluster_id}: {last_error}")
                record_failure(cluster_id, "Cannot connect to LXD endpoint. Check if server is running.")
                return ConnectionProbe(False, False, "Cannot connect to LXD endpoint. Check if server is running.")
            
            # An HTTP response means the host is reachable: clear any breaker trip.
            clear(cluster_id)

            if response.status_code == 200:
                logger.info(f"Certificate trusted and authenticated for {cluster_id}")
                return ConnectionProbe(True, True, "Certificate trusted and authenticated")
            elif response.status_code == 403:
                logger.warning(f"Certificate not trusted for {cluster_id}")
                return ConnectionProbe(
                    True, False, "Certificate not trusted by hypervisor. Add trust via CLI."
                )
            else:
                logger.warning(f"Unexpected status {response.status_code} for {cluster_id}")
                return ConnectionProbe(True, False, f"Unexpected response: HTTP {response.status_code}")
            
        except requests.exceptions.SSLError as e:
            logger.error(f"SSL error for {cluster_id}: {e}")
            # important: keep this branch above the ConnectionError one, which SSLError
            # subclasses. reachable=True: the socket answered and refused the handshake, so the
            # host is up and the trust problem needs fixing.
            return ConnectionProbe(True, False, "Certificate error. Verify certificate is valid.")
        except requests.exceptions.ConnectionError as e:
            # Logged at WARNING: refused or unresolvable is an offline cluster, and the breaker
            # below is what the product acts on. The SSL branch logs at ERROR because an
            # untrusted or expired certificate does not clear itself when the host comes back.
            logger.warning(f"Connection error for {cluster_id}: {e}")
            record_failure(cluster_id, "Cannot connect to LXD endpoint. Check if server is running.")
            return ConnectionProbe(False, False, "Cannot connect to LXD endpoint. Check if server is running.")
        except requests.exceptions.Timeout as e:
            logger.warning(f"Timeout for {cluster_id}: {e}")
            record_failure(cluster_id, "Connection timeout. Check network connectivity.")
            return ConnectionProbe(False, False, "Connection timeout. Check network connectivity.")
        except LXDLocalStorageError:
            # important: raise it instead of returning a probe. Every ConnectionProbe is a claim
            # about the cluster, and the peer was never contacted. reachable=None would send
            # derive_status down its "default to OFFLINE" path and label a healthy hypervisor
            # down. The breaker is left untouched.
            logger.error(f"Cannot prepare TLS credentials for {cluster_id}: backend storage")
            raise
        except Exception as e:
            # important: no raw exception text in the message. It reaches ClusterInfo.error_message
            # on /clusters, which every role down to tenant can read, and a Vault failure here
            # carries the Vault base URL and the KV path to the cluster's certificates.
            logger.error(f"Verification failed for {cluster_id}: {e}")
            return ConnectionProbe(None, False, "Could not verify the connection to this cluster.")
