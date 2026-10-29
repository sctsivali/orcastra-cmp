"""Server-certificate pin enforcement for the connection manager.

Split out of `connection` so the manager stays readable: deciding whether the certificate a cluster
presents is the one this console enrolled, recording a first pin or a public-CA renewal in the
cluster's Vault metadata, and holding a migration transport to an approved pin. The rules
themselves live in `services/lxd_certs/pinning.py`; this is where they meet Vault and the pool.
"""

import logging
import threading
from typing import Dict, Optional
from urllib.parse import urlparse

from .exceptions import LXDTransportPinError

logger = logging.getLogger(__name__)

_pin_locks_guard = threading.Lock()


class CertificatePinMixin:
    """Expects LXDConnectionManager's `_connection_info`, `_clients` and `cleanup_connection`."""

    def _pin_lock(self, cluster_id: str) -> threading.Lock:
        # note: not the rotation lock. Certificate rotation calls connect_with_vault_cert while
        # holding that one, and this lock is taken inside it.
        with _pin_locks_guard:
            locks: Dict[str, threading.Lock] = self.__dict__.setdefault("_pin_locks", {})
            return locks.setdefault(cluster_id, threading.Lock())

    def _enforce_certificate_pin(
        self, cluster_id: str, endpoint: str
    ) -> Optional[str]:
        """Pin on first sight, accept a public-CA renewal, refuse any other change, and never turn
        a network fault into an alarm."""
        from app.core.config import get_settings

        if not get_settings().lxd_pin_server_certificate:
            return None

        from app.services.lxd_certs.pinning import FINGERPRINT_FIELD, decide_pin

        try:
            metadata = self._read_cluster_metadata(cluster_id)
        except Exception as exc:
            # Vault unreachable is not a certificate problem, and refusing here would make an
            # outage in one dependency look like an attack on another.
            logger.warning("Could not read the pin for %s (%s); connecting unpinned", cluster_id, exc)
            return None

        decision = decide_pin(cluster_id, endpoint, metadata)
        if decision.update:
            try:
                self._write_cluster_pin(
                    cluster_id,
                    compared=(metadata or {}).get(FINGERPRINT_FIELD),
                    update=decision.update,
                    outcome=decision.outcome,
                    endpoint=endpoint,
                )
            except Exception as exc:
                # Failing to record it means the next connection decides again, which is harmless.
                logger.warning("Could not record the pin for %s: %s", cluster_id, exc)
        return decision.fingerprint

    def refresh_pin(self, cluster_id: str) -> Optional[str]:
        """Read the cluster's certificate now and record or renew its pin, without connecting.

        The handshake that identifies the certificate sends no client credential, so this is safe
        to call for a cluster whose pin is missing or stale. Returns the fingerprint the cluster
        must present, or None when it cannot be read.
        """
        info = self._connection_info.get(cluster_id) or {}
        endpoint = info.get("endpoint")
        if not endpoint:
            try:
                endpoint = (self._read_cluster_metadata(cluster_id) or {}).get("endpoint")
            except Exception as exc:
                logger.warning("Could not read the endpoint of %s: %s", cluster_id, exc)
                return None
        if not endpoint:
            return None
        fingerprint = self._enforce_certificate_pin(cluster_id, endpoint)
        if fingerprint is None:
            return None
        from .connection import _client_adapter_fingerprint

        # A client pooled against a superseded pin fails its next socket; drop it now instead.
        client = self._clients.get(cluster_id)
        adapter_pin = _client_adapter_fingerprint(client) if client is not None else None
        if adapter_pin and adapter_pin != fingerprint:
            self.cleanup_connection(cluster_id)
        return fingerprint

    def _enforce_expected_certificate_pin(
        self,
        cluster_id: str,
        endpoint: str,
        expected_fingerprint: str,
    ) -> str:
        """Verify a persisted migration pin before presenting the client certificate."""
        from app.services.lxd_certs.pinning import fetch_server_fingerprint

        from .connection import normalize_server_fingerprint

        expected = normalize_server_fingerprint(expected_fingerprint)
        if len(expected) != 64 or any(value not in "0123456789abcdef" for value in expected):
            raise LXDTransportPinError(
                "The approved cluster certificate fingerprint is invalid."
            )
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
        live = fetch_server_fingerprint(endpoint)
        if live is None:
            raise LXDTransportPinError(
                "The cluster certificate could not be verified against the approved fingerprint."
            )
        if normalize_server_fingerprint(live) != expected:
            logger.error(
                "Migration transport certificate mismatch for %s; refusing the connection",
                cluster_id,
            )
            raise LXDTransportPinError(
                "The cluster certificate no longer matches the approved fingerprint."
            )
        return expected

    def _read_cluster_metadata(self, cluster_id: str) -> dict:
        from app.core.config import get_settings
        from app.core.vault_client import get_vault_client

        settings = get_settings()
        if not (settings.vault_enabled and settings.vault_token):
            return {}
        client = get_vault_client(settings.vault_addr, settings.vault_token)
        return client.vault_read(f"{settings.vault_clusters_path}/{cluster_id}/metadata") or {}

    def _write_cluster_pin(
        self,
        cluster_id: str,
        *,
        compared: Optional[str],
        update: dict,
        outcome: str,
        endpoint: str,
    ) -> None:
        from app.core.config import get_settings
        from app.core.vault_client import get_vault_client
        from app.services.lxd_certs.pinning import FINGERPRINT_FIELD, TRUST_FIELD

        settings = get_settings()
        client = get_vault_client(settings.vault_addr, settings.vault_token)
        path = f"{settings.vault_clusters_path}/{cluster_id}/metadata"
        with self._pin_lock(cluster_id):
            # Read-modify-write, like every other metadata writer here, so nothing else is dropped,
            # and uncached, because the cached body may predate a re-pin another worker just wrote.
            current = client.vault_read(path, use_cache=False) or {}
            stored = current.get(FINGERPRINT_FIELD)
            if stored != compared:
                if stored == update.get(FINGERPRINT_FIELD, stored):
                    return  # another worker recorded the same decision first
                logger.warning(
                    "Not recording the %s pin for %s: the stored pin changed while it was decided",
                    outcome, cluster_id,
                )
                return
            client.vault_kv_put(path, {**current, **update})

        action = {
            "pinned": "cluster.server_certificate.pin",
            "renewed": "cluster.server_certificate.renew",
        }.get(outcome)
        if action is None:
            return
        try:
            from app.services.activity_logger import get_activity_logger

            new = update.get(FINGERPRINT_FIELD) or ""
            get_activity_logger().log_activity(
                action=action,
                target_type="cluster",
                target_id=cluster_id,
                target_host=endpoint,
                result="success",
                details={
                    "previous_fingerprint": (compared or "")[:16] or None,
                    "fingerprint": new[:16],
                    "trust": update.get(TRUST_FIELD) or current.get(TRUST_FIELD),
                },
            )
        except Exception:
            pass  # never block a connection on audit failure
