"""API shapes for certificate fingerprints and superseded trust entries.

schemas.py re-exports SupersededTrust because CertificateInfo carries it.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.core.cert_fingerprint import is_lxd_fingerprint


class SupersededTrust(BaseModel):
    """One trust entry of a certificate this dashboard replaced on the cluster."""

    fingerprint: str = Field(..., description="LXD fingerprint (lowercase sha256 hex)")
    reason: str = Field(..., description="What replaced it: renew, verify, regenerate, cancel, ...")
    state: str = Field(..., description="pending | failed | removed | cancelled")
    identity_name: Optional[str] = Field(None, description="Trust entry name, when known")
    superseded_at: datetime
    due_at: Optional[datetime] = Field(None, description="When the next removal attempt is allowed")
    removed_at: Optional[datetime] = None
    already_gone: bool = Field(False, description="The cluster no longer had the entry")
    attempts: int = 0
    last_error: Optional[str] = None
    manual_command: Optional[str] = Field(
        None, description="CLI line that removes the entry by hand, while it is not removed"
    )


class PendingCertificateResponse(BaseModel):
    """A renewal waiting for its certificate to be trusted, so Verify can be resumed later."""

    cluster_id: str
    pending_cert_id: Optional[str] = None
    fingerprint: Optional[str] = Field(None, description="LXD fingerprint of the pending certificate")
    certificate: str = Field(..., description="Pending certificate PEM (public, never the key)")
    cli_command: str = Field(..., description="Command that adds the pending certificate to trust")
    identity_name: Optional[str] = None
    created_at: Optional[datetime] = None


class TrustCleanupRequest(BaseModel):
    """Remove superseded trust entries now. Without a fingerprint, every open entry of the cluster."""

    fingerprint: Optional[str] = Field(None, description="One superseded fingerprint to remove")

    @field_validator("fingerprint")
    @classmethod
    def _full_fingerprint(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip().lower()
        if not is_lxd_fingerprint(value):
            raise ValueError("fingerprint must be the full 64 character LXD fingerprint")
        return value


class TrustCleanupResponse(BaseModel):
    cluster_id: str
    message: str
    outcomes: dict = Field(default_factory=dict, description="fingerprint -> outcome")
    superseded: List[SupersededTrust] = Field(default_factory=list)
