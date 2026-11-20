"""PKCS#12 bundles for issued operator certificates: always password-protected."""

from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12


def create_pfx(cert_pem: bytes, key_pem: bytes, password: str, friendly_name: bytes,
               ca_pem: Optional[bytes] = None) -> bytes:
    if not password:
        raise ValueError("an operator certificate bundle needs a password")
    cert = x509.load_pem_x509_certificate(cert_pem)
    key = serialization.load_pem_private_key(key_pem, password=None)
    cas = [x509.load_pem_x509_certificate(ca_pem)] if ca_pem else None
    # important: PBESv2/AES via the builder, which every current browser and OS keystore imports;
    # the legacy RC2/3DES default is refused by OpenSSL 3 without the legacy provider.
    encryption = (
        serialization.PrivateFormat.PKCS12.encryption_builder()
        .kdf_rounds(50000)
        .key_cert_algorithm(pkcs12.PBES.PBESv2SHA256AndAES256CBC)
        .hmac_hash(hashes.SHA256())
        .build(password.encode())
    )
    return pkcs12.serialize_key_and_certificates(
        name=friendly_name, key=key, cert=cert, cas=cas, encryption_algorithm=encryption,
    )
