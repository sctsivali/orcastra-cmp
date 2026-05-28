import errno
import os
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.services.lxd_ssl import create_lxd_ssl_context


def build_test_cert_data() -> dict[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "orcastra-test"),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return {"client.crt": cert_pem, "client.key": key_pem}


class LXDSSLContextTests(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(os, "memfd_create") and os.path.isdir("/proc/self/fd"),
        "requires Linux memfd support",
    )
    def test_loads_cert_chain_from_memory_without_temp_directory(self) -> None:
        cert_data = build_test_cert_data()

        with mock.patch.object(
            tempfile,
            "TemporaryDirectory",
            side_effect=AssertionError("temp directory fallback should not run"),
        ):
            ssl_context = create_lxd_ssl_context(cert_data)

        self.assertIsInstance(ssl_context, ssl.SSLContext)

    def test_temp_storage_error_is_actionable(self) -> None:
        with mock.patch("app.services.lxd_ssl._supports_memfd", return_value=False):
            with mock.patch.object(
                tempfile,
                "TemporaryDirectory",
                side_effect=OSError(errno.ENOSPC, "No space left on device"),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    create_lxd_ssl_context({"client.crt": "cert", "client.key": "key"})

        message = str(raised.exception)
        self.assertIn("Backend temporary storage is full", message)
        self.assertIn("BACKEND_TMPFS_SIZE", message)


if __name__ == "__main__":
    unittest.main()
