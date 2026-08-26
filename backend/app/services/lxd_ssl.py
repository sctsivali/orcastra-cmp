"""
SSL helpers for LXD WebSocket connections.
"""

import errno
import logging
import os
import ssl
import tempfile
from typing import Mapping

from app.services.lxd.cert_files import describe_disk_usage, staging_dir

logger = logging.getLogger(__name__)


def create_lxd_ssl_context(cert_data: Mapping[str, str]) -> ssl.SSLContext:
    """Create an LXD client SSL context without writing credentials to /tmp on Linux."""
    cert_pem = cert_data.get("client.crt")
    key_pem = cert_data.get("client.key")
    if not cert_pem or not key_pem:
        raise ValueError("LXD client certificate or key is missing")

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    _load_client_cert_chain(ssl_context, cert_pem, key_pem)
    return ssl_context


def _load_client_cert_chain(
    ssl_context: ssl.SSLContext,
    cert_pem: str,
    key_pem: str,
) -> None:
    if _supports_memfd():
        try:
            _load_cert_chain_from_memfd(ssl_context, cert_pem, key_pem)
            return
        except OSError as exc:
            logger.warning("In-memory LXD certificate load failed, using temp files: %s", exc)

    _load_cert_chain_from_tempfiles(ssl_context, cert_pem, key_pem)


def _supports_memfd() -> bool:
    return hasattr(os, "memfd_create") and os.path.isdir("/proc/self/fd")


def _load_cert_chain_from_memfd(
    ssl_context: ssl.SSLContext,
    cert_pem: str,
    key_pem: str,
) -> None:
    flags = getattr(os, "MFD_CLOEXEC", 0)
    cert_fd = os.memfd_create("orcastra-lxd-client-cert", flags)
    key_fd = os.memfd_create("orcastra-lxd-client-key", flags)

    try:
        os.write(cert_fd, cert_pem.encode("utf-8"))
        os.write(key_fd, key_pem.encode("utf-8"))
        os.lseek(cert_fd, 0, os.SEEK_SET)
        os.lseek(key_fd, 0, os.SEEK_SET)
        ssl_context.load_cert_chain(
            certfile=f"/proc/self/fd/{cert_fd}",
            keyfile=f"/proc/self/fd/{key_fd}",
        )
    finally:
        os.close(cert_fd)
        os.close(key_fd)


def _load_cert_chain_from_tempfiles(
    ssl_context: ssl.SSLContext,
    cert_pem: str,
    key_pem: str,
) -> None:
    try:
        # dir= is load-bearing: without it tempfile resolves through gettempdir(), which falls
        # through to /var/tmp when /tmp is full and would put the private key on disk.
        with tempfile.TemporaryDirectory(prefix="orcastra-lxd-ssl-", dir=staging_dir()) as cert_dir:
            cert_path = os.path.join(cert_dir, "client.crt")
            key_path = os.path.join(cert_dir, "client.key")

            _write_secure_file(cert_path, cert_pem)
            _write_secure_file(key_path, key_pem)
            ssl_context.load_cert_chain(cert_path, key_path)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise _build_temp_storage_error(exc) from exc
        raise


def _write_secure_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as file_handle:
        file_handle.write(content)
    os.chmod(path, 0o600)


def _build_temp_storage_error(exc: OSError) -> RuntimeError:
    usage = describe_disk_usage(staging_dir())
    return RuntimeError(
        "Backend temporary storage is full while preparing LXD TLS credentials. "
        f"{usage} Increase BACKEND_TMPFS_SIZE or clear stale files in the backend container."
    )
