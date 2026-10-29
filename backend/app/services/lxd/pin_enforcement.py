"""Server-certificate pin enforcement for the connection manager.

Split out of `connection` so the manager stays readable: deciding whether the certificate a cluster
presents is the one this console enrolled, recording a first pin or a public-CA renewal in the
cluster's Vault metadata, and holding a migration transport to an approved pin. The rules
themselves live in `services/lxd_certs/pinning.py`; this is where they meet Vault and the pool.
"""

import logging
import threading
import time
from typing import Dict, Optional
from urllib.parse import urlparse

from . import health
from .exceptions import LXDTransportPinError

logger = logging.getLogger(__name__)

_pin_locks_guard = threading.Lock()

# How long a refresh answers from memory: long enough to absorb a caller in a loop, short enough
# that a renewal is noticed on the next real attempt.
_REFRESH_REUSE_SECONDS = 15.0


class CertificatePinMixin:
    """Expects LXDConnectionManager's `_connection_info`, `_clients` and `cleanup_connection`."""

    def _pin_lock(self, cluster_id: str) -> threading.Lock:
        # note: not the rotation lock. Certificate rotation calls connect_with_vault_cert while
        # holding that one, and this lock is taken inside it.
        with _pin_locks_guard:
            locks: Dict[str, threading.Lock] = self.__dict__.setdefault("_pin_locks", {})
            return locks.setdefault(cluster_id, threading.Lock())

    def _last_known_pins(self) -> Dict[str, str]:
        return self.__dict__.setdefault("_last_pins", {})

    def _enforce_certificate_pin(
        self, cluster_id: str, endpoint: str
    ) -> Optional[str]:
        """Pin on first sight, accept a public-CA renewal, refuse any other change, and never turn
        a network fault into an alarm."""
        from app.core.config import get_settings

        if not get_settings().lxd_pin_server_certificate:
            return None

        from app.services.lxd_certs.pinning import (
            FINGERPRINT_FIELD,
            LXDCertificatePinError,
            decide_pin,
        )

        try:
            metadata = self._read_cluster_metadata(cluster_id)
        except Exception as exc:
            # Vault unreachable is not a certificate problem, and refusing here would make an
            # outage in one dependency look like an attack on another. A pin this worker already
            # saw still holds the socket, so only a cluster never seen here goes unpinned.
            remembered = self._last_known_pins().get(cluster_id)
            if remembered:
                logger.warning(
                    "Could not read the pin for %s (%s); holding it to the last pin seen", cluster_id, exc
                )
                return remembered
            logger.warning("Could not read the pin for %s (%s); connecting unpinned", cluster_id, exc)
            return None

        decision = decide_pin(cluster_id, endpoint, metadata)
        fingerprint = decision.fingerprint
        if decision.update:
            try:
                stored = self._write_cluster_pin(
                    cluster_id,
                    compared=(metadata or {}).get(FINGERPRINT_FIELD),
                    update=decision.update,
                    outcome=decision.outcome,
                    endpoint=endpoint,
                )
            except Exception as exc:
                # Failing to record it means the next connection decides again, which is harmless:
                # this one is still held to what it decided.
                logger.warning("Could not record the pin for %s: %s", cluster_id, exc)
            else:
                # important: when another worker or an Administrator recorded a different pin
                # first, the certificate this worker saw is not the pinned one, so it must not be
                # sent the client credential under its own decision.
                if stored and fingerprint and stored != fingerprint:
                    logger.error(
                        "Refusing %s: it presented %s… but %s… was pinned concurrently",
                        cluster_id, fingerprint[:16], stored[:16],
                    )
                    raise LXDCertificatePinError(
                        f"Cluster {cluster_id} presented a certificate other than the one pinned "
                        "for it. Re-pin the cluster if its certificate was rotated."
                    )
        if fingerprint:
            self._last_known_pins()[cluster_id] = fingerprint
        return fingerprint

    def refresh_pin(self, cluster_id: str) -> Optional[str]:
        """Read the cluster's certificate now and record or renew its pin, without connecting.

        The handshake that identifies the certificate sends no client credential, so this is safe
        to call for a cluster whose pin is missing or stale. Returns the fingerprint the cluster
        must present, or None when it cannot be read.

        note: answered from memory for a few seconds, and skipped for a cluster the breaker holds
        as down, so a caller in a loop cannot turn it into a stream of handshakes.
        """
        recent = self.__dict__.setdefault("_pin_refreshed", {})
        cached = recent.get(cluster_id)
        if cached and time.monotonic() - cached[0] < _REFRESH_REUSE_SECONDS:
            return cached[1]
        if health.is_tripped(cluster_id):
            return None
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
        recent[cluster_id] = (time.monotonic(), fingerprint)
        if fingerprint is None:
            return None
        from .connection import _client_adapter_fingerprint

        # A pooled client bound to a superseded pin, or to none at all, is replaced instead of
        # being trusted with the credential until its next failure.
        client = self._clients.get(cluster_id)
        if client is not None and _client_adapter_fingerprint(client) != fingerprint:
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
        # Uncached: a pin decision taken from a cached body could predate a re-pin.
        return (
            client.vault_read(f"{settings.vault_clusters_path}/{cluster_id}/metadata", use_cache=False)
            or {}
        )

    def _write_cluster_pin(
        self,
        cluster_id: str,
        *,
        compared: Optional[str],
        update: dict,
        outcome: str,
        endpoint: str,
    ) -> Optional[str]:
        """Record a pin decision unless the stored pin moved on; return the pin stored afterwards.

        A check-and-set write against the version just read, so neither another worker's renewal
        nor an Administrator's re-pin is overwritten by a decision taken from an older body.
        """
        from app.core.config import get_settings
        from app.core.vault_client import VaultCASConflict, get_vault_client
        from app.services.lxd_certs.pinning import FINGERPRINT_FIELD, TRUST_FIELD

        settings = get_settings()
        client = get_vault_client(settings.vault_addr, settings.vault_token)
        path = f"{settings.vault_clusters_path}/{cluster_id}/metadata"
        with self._pin_lock(cluster_id):
            current, version = client.vault_read_versioned(path)
            stored = current.get(FINGERPRINT_FIELD)
            if stored != compared:
                if stored != update.get(FINGERPRINT_FIELD, stored):
                    logger.warning(
                        "Not recording the %s pin for %s: the stored pin changed while it was decided",
                        outcome, cluster_id,
                    )
                return stored
            try:
                client.vault_kv_put(path, {**current, **update}, cas=version)
            except VaultCASConflict:
                latest, _ = client.vault_read_versioned(path)
                return latest.get(FINGERPRINT_FIELD)

        action = {
            "pinned": "cluster.server_certificate.pin",
            "renewed": "cluster.server_certificate.renew",
        }.get(outcome)
        new = update.get(FINGERPRINT_FIELD) or stored
        if action is not None:
            try:
                from app.services.activity_logger import get_activity_logger

                get_activity_logger().log_activity(
                    action=action,
                    target_type="cluster",
                    target_id=cluster_id,
                    target_host=endpoint,
                    result="success",
                    details={
                        "previous_fingerprint": (compared or "")[:16] or None,
                        "fingerprint": (new or "")[:16],
                        "trust": update.get(TRUST_FIELD) or current.get(TRUST_FIELD),
                    },
                )
            except Exception:
                pass  # never block a connection on audit failure
        return new
