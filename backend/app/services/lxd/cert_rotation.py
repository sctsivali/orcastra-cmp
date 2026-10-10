"""How a worker follows a client certificate that was rotated somewhere else.

Each of the four workers keeps its own pooled client per cluster, built from the certificate it
read when it connected, and nothing tells the other three when a renewal or regenerate replaces it.
Before trust cleanup existed that never mattered: the old certificate stayed trusted forever. Once
its trust entry is removed, a worker still presenting it is refused, and the refusal branch used to
evict the credentials and trip the breaker, so that worker answered 503 and showed the cluster as
Pending Trust for a minute while it had a perfectly good certificate one Vault read away.

Now the refusal branch reads the active certificate once. If it differs from the one just refused,
the worker connects with it and the request carries on. If it is the same certificate, or Vault
cannot say, everything happens exactly as before.
"""

import logging
import threading
from typing import Dict, Optional, Tuple

from app.core.cert_fingerprint import lxd_fingerprint

from . import health
from .exceptions import LXDAuthorizationError, LXDLocalStorageError, LXDTransportPinError

logger = logging.getLogger(__name__)

_locks_guard = threading.Lock()


def _fingerprint_or_none(pem: Optional[str]) -> Optional[str]:
    try:
        return lxd_fingerprint(pem) if pem else None
    except ValueError:
        return None


class CertificateRotationMixin:
    """Expects LXDConnectionManager's `_connection_info`, `_clients` and `connect_with_vault_cert`."""

    def _rotation_lock(self, cluster_id: str) -> threading.Lock:
        with _locks_guard:
            locks: Dict[str, threading.Lock] = self.__dict__.setdefault("_rotation_locks", {})
            return locks.setdefault(cluster_id, threading.Lock())

    def active_certificate_fingerprint(self, cluster_id: str) -> Optional[str]:
        """The fingerprint of the certificate this worker presents to the cluster, if connected."""
        info = self._connection_info.get(cluster_id) or {}
        return _fingerprint_or_none(info.get("cert_content"))

    def _read_active_certificate(self, cluster_id: str) -> Optional[Tuple[str, str]]:
        """The current certificate and key from Vault, uncached, or None when Vault cannot say."""
        from app.core.config import get_settings
        from app.core.vault_client import get_vault_client

        settings = get_settings()
        if not (settings.vault_enabled and settings.vault_token):
            return None
        try:
            client = get_vault_client(settings.vault_addr, settings.vault_token)
            certs = client.vault_read(f"{settings.vault_clusters_path}/{cluster_id}/certs") or {}
        except Exception as exc:  # noqa: BLE001 - an unreadable Vault keeps today's behaviour
            logger.info("Could not read the active certificate for %s: %s", cluster_id, exc)
            return None
        cert, key = certs.get("client.crt"), certs.get("client.key")
        return (cert, key) if cert and key else None

    def _try_rotated_certificate(
        self,
        cluster_id: str,
        refused: Dict[str, str],
        *,
        expected_server_fingerprint: Optional[str] = None,
    ):
        """Reconnect with the Vault certificate when it is not the one the cluster just refused.

        Returns the new client, or None to let the caller record the refusal as it always has.
        One thread per cluster does the Vault read; the others find its client when they get in.
        """
        refused_fp = _fingerprint_or_none(refused.get("cert_content"))
        with self._rotation_lock(cluster_id):
            held = self.active_certificate_fingerprint(cluster_id)
            if cluster_id in self._clients and held and held != refused_fp:
                return self._clients[cluster_id]
            active = self._read_active_certificate(cluster_id)
            if active is None or _fingerprint_or_none(active[0]) in (None, refused_fp):
                return None
            logger.warning(
                "Certificate for %s was rotated elsewhere; reconnecting with the current one", cluster_id
            )
            try:
                return self.connect_with_vault_cert(
                    cluster_id,
                    refused["endpoint"],
                    active[0],
                    active[1],
                    verify=bool(refused.get("verify", False)),
                    force=True,
                    expected_server_fingerprint=expected_server_fingerprint,
                )
            except (LXDTransportPinError, LXDLocalStorageError):
                raise
            except LXDAuthorizationError:
                # connect_with_vault_cert already dropped the info and recorded it unauthorized.
                return None
            except Exception as exc:  # noqa: BLE001
                # The host answered a moment ago and refused us, so this stays unauthorized rather
                # than being downgraded to offline by the generic handler's record.
                logger.warning("Reconnect to %s with the current certificate failed: %s", cluster_id, exc)
                health.record_failure(
                    cluster_id, "certificate not trusted", kind=health.KIND_UNAUTHORIZED
                )
                return None

    def adopt_vault_certificate(self, cluster_id: str, endpoint: str, cert: str, key: str):
        """Switch this worker to a certificate already proven trusted, and keep the cluster listed.

        note: verify used to call remove_connection first, which also drops the cluster from this
        worker's registered whitelist, so it vanished from every list page served by this worker
        until the next healer pass. Connecting over the existing entry replaces the client in place.
        """
        verify = bool((self._connection_info.get(cluster_id) or {}).get("verify", False))
        try:
            client = self.connect_with_vault_cert(
                cluster_id, endpoint, cert, key, verify=verify, force=True
            )
        except Exception as exc:  # noqa: BLE001 - the request path heals a worker this misses
            logger.warning("Could not switch %s to the current certificate: %s", cluster_id, exc)
            return None
        self.register_cluster(cluster_id)
        return client
