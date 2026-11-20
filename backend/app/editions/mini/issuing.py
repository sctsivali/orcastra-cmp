"""Issuing an operator certificate: Vault PKI, then a password-protected PKCS#12 bundle.

The certificate comes from the PKI role the deployment already uses (VAULT_PKI_ROLE). The private
key exists only in the bundle handed to the requester once; the server keeps the certificate's
fingerprint, serial and expiry, never the key or the bundle's password.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, status
from fastapi.responses import Response

from app.core.config import get_settings
from app.editions.mini import identities
from app.editions.mini.client_cert import PresentedCertificate, parse_pem
from app.editions.mini.pkcs12 import create_pfx
from app.editions.mini.settings import mini_settings

_LABEL = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True)
class IssuedBundle:
    certificate: PresentedCertificate
    pfx: bytes
    password: str
    filename: str


def common_name_for(email: str, label: Optional[str] = None) -> str:
    base = _LABEL.sub("-", (label or email.split("@", 1)[0]).lower()).strip("-")[:40] or "operator"
    # A short random suffix keeps two certificates of one person, or two people named alike,
    # apart in a certificate list; identity is never read from it.
    return f"{base}-{secrets.token_hex(3)}.{mini_settings().client_cert_cn_suffix}"


async def issue_bundle(email: str, *, ttl_days: Optional[int] = None, label: Optional[str] = None) -> IssuedBundle:
    settings, ms = get_settings(), mini_settings()
    if not settings.vault_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Vault is not enabled; certificates cannot be issued")
    days = min(ttl_days or ms.client_cert_ttl_days, ms.client_cert_ttl_days)
    cn = common_name_for(email, label)

    def _issue():
        from app.core.vault_client import get_vault_client

        client = get_vault_client(settings.vault_addr, settings.vault_token)
        return client.generate_lxd_certificate(cn, ttl=f"{days * 24}h", role=ms.vault_pki_role)

    try:
        material = await asyncio.wait_for(asyncio.to_thread(_issue), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Vault did not answer; try again shortly")
    except Exception:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Vault refused the certificate; check that it is unsealed (Host Health)")
    cert_pem, key_pem = material["cert"].encode(), material["key"].encode()
    parsed = parse_pem(cert_pem)
    if parsed is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Vault returned an unreadable certificate")
    password = identities.new_password()
    pfx = create_pfx(cert_pem, key_pem, password, friendly_name=email.encode()[:64],
                     ca_pem=(material.get("ca") or "").encode() or None)
    return IssuedBundle(parsed, pfx, password, filename=f"{cn.split('.', 1)[0]}.p12")


def bundle_response(bundle: IssuedBundle, *, operator_uuid: str) -> Response:
    """The .p12 as a one-time download; the password travels in a header, never in a URL or log."""
    return Response(
        content=bundle.pfx,
        media_type="application/x-pkcs12",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
            "X-P12-Password": bundle.password,
            "X-Operator-Uuid": operator_uuid,
            "X-Certificate-Fingerprint": bundle.certificate.fingerprint,
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )
