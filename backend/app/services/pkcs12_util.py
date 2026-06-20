"""PKCS#12 (.p12/.pfx) bundling helpers.

Shared by the LXD identity-key feature (my_keys) and the mini-build client-cert
identity issuance. Kept in one place so cert/key bundling logic is not duplicated.
"""

from typing import Optional

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12


def create_pfx(
    cert_pem: bytes,
    key_pem: bytes,
    password: str = "",
    friendly_name: bytes = b"orcastra-key",
    ca_pem: Optional[bytes] = None,
) -> bytes:
    """Bundle a PEM cert + key (and optional CA chain) into a PKCS#12 blob.

    When ``password`` is non-empty the key is wrapped with BestAvailableEncryption;
    callers handling user identity certs should always pass a password.
    """
    cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
    private_key = serialization.load_pem_private_key(
        key_pem, password=None, backend=default_backend()
    )

    cas = None
    if ca_pem:
        cas = [x509.load_pem_x509_certificate(ca_pem, default_backend())]

    encryption = (
        serialization.BestAvailableEncryption(password.encode())
        if password
        else serialization.NoEncryption()
    )

    return pkcs12.serialize_key_and_certificates(
        name=friendly_name,
        key=private_key,
        cert=cert,
        cas=cas,
        encryption_algorithm=encryption,
    )


def cert_fingerprint_sha256(cert_pem: bytes) -> str:
    """SHA-256 fingerprint (hex) of a PEM certificate's DER bytes.

    Single source of truth for the fingerprint algorithm so nginx, the cert-login
    endpoint, and the identity store all agree.
    """
    cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
    return cert.fingerprint(hashes.SHA256()).hex()
