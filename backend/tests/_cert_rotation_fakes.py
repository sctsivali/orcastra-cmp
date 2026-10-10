"""Fakes for certificate rotation and trust cleanup: real certificates, a dict Vault, SQLite rows."""

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.core.vault_client import VaultClientError
from app.models.trust_cleanup_models import LxdTrustCleanup
from tests._sqlite_session import sqlite_session

CLUSTERS = "secret/data/clusters"


def make_cert(cn: str = "nuc9.orcastra.io", days: int = 30) -> Tuple[str, str]:
    """A real self-signed certificate and key, PEM encoded."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Orcastra Test CA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


class FakeVault:
    def __init__(self) -> None:
        self.data: Dict[str, Dict[str, Any]] = {}
        self.writes: List[str] = []
        self.fail_writes_to: Optional[str] = None
        self.issued: List[Tuple[str, str]] = []

    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        if path not in self.data:
            raise VaultClientError(f"404 {path}")
        return dict(self.data[path])

    def vault_kv_put(self, path: str, data: Dict[str, Any]) -> bool:
        if self.fail_writes_to and path.endswith(self.fail_writes_to):
            raise VaultClientError(f"write refused {path}")
        self.writes.append(path)
        self.data[path] = dict(data)
        return True

    def vault_delete(self, path: str) -> bool:
        if path not in self.data:
            raise VaultClientError(f"404 {path}")
        del self.data[path]
        return True

    def generate_lxd_certificate(self, common_name: str, ttl: str) -> Dict[str, str]:
        cert, key = make_cert(common_name)
        self.issued.append((cert, key))
        return {"cert": cert, "key": key, "ca": "CA-PEM"}


class FakeCertManager:
    """The LXDCertificateManager surface rotation.py uses."""

    def __init__(self, cluster_id: str = "nuc9", *, cluster_type: str = "LXD") -> None:
        self.vault = FakeVault()
        self.clusters_path = CLUSTERS
        self.cluster_id = cluster_id
        self.trust_with_existing = True
        self.trust_with_password = False
        self.verified = True
        self.trust_calls: List[Tuple[str, str]] = []
        cert, key = make_cert(f"{cluster_id}.orcastra.io")
        self.current = (cert, key)
        self.vault.data[f"{CLUSTERS}/{cluster_id}/certs"] = {
            "client.crt": cert, "client.key": key, "ca.pem": "CA", "common_name": f"{cluster_id}.orcastra.io",
        }
        self.vault.data[f"{CLUSTERS}/{cluster_id}/metadata"] = {
            "endpoint": "https://10.0.0.9:8443", "type": cluster_type,
        }

    def path(self, leaf: str) -> str:
        return f"{CLUSTERS}/{self.cluster_id}/{leaf}"

    def add_pending(self, *, pending_cert_id: str = "pending-1", identity_name: str = "orca-nuc9-old") -> str:
        cert, key = make_cert(f"{self.cluster_id}.orcastra.io")
        self.vault.data[self.path("pending_certs")] = {
            "client.crt": cert, "client.key": key, "ca.pem": "CA",
            "common_name": f"{self.cluster_id}.orcastra.io",
            "pending_cert_id": pending_cert_id, "identity_name": identity_name,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        return cert

    def add_trust_with_existing_cert(self, cluster_id: str, new_cert_pem: str, cert_name: str, **_: Any) -> bool:
        self.trust_calls.append(("existing", cert_name))
        return self.trust_with_existing

    def add_trust_with_password(self, cluster_id: str, new_cert_pem: str, cert_name: str, trust_password: str) -> bool:
        self.trust_calls.append(("password", cert_name))
        return self.trust_with_password

    def verify_connection_with_cert(self, endpoint: str, cert_pem: str, key_pem: str) -> bool:
        return self.verified


class SessionMaker:
    """`async with session_maker() as db` over one shared SQLite facade, like the app's maker."""

    def __init__(self, db: Any = None) -> None:
        self.db = db or sqlite_session(LxdTrustCleanup)

    def __call__(self) -> "SessionMaker":
        return self

    async def __aenter__(self) -> Any:
        return self.db

    async def __aexit__(self, *_: Any) -> None:
        self.db.sync.rollback()


def rows(db: Any, cluster_id: Optional[str] = None) -> List[LxdTrustCleanup]:
    from sqlalchemy import select

    query = select(LxdTrustCleanup)
    if cluster_id:
        query = query.where(LxdTrustCleanup.cluster_id == cluster_id)
    db.sync.expire_all()
    return list(db.sync.execute(query).scalars().all())
