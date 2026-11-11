"""How long a cluster's client certificate lasts, and the Vault request that issues it.

An operator chooses at registration, regenerate or renew. `default` is the deployment's
VAULT_CERT_TTL, exactly what every certificate got before the choice existed, so a client that
sends nothing issues as it always did. `longest` runs until the issuing CA expires.

The real expiry is read back from the issued certificate and reported: Vault caps a ttl above the
role's max_ttl with only a warning, and an operator must not be told a date nobody issued.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from app.core.config import get_settings
from app.core.vault_client import VaultClientError
from app.models.schemas import CertificateLifetime, CertificateLifetimeOption, CertificateLifetimeOptions
from app.services.lxd_certs.inspect import parse_certificate_pem

CA_PATH = "pki_int/cert/ca"
# `longest` stops this far short of the CA's end, so a clock a moment ahead of Vault's does not turn
# it into a request beyond the CA, which Vault refuses.
LONGEST_MARGIN = timedelta(hours=1)
# A CA this close to its end leaves nothing worth calling the longest lifetime.
LONGEST_FLOOR = timedelta(days=1)
# Vault counts from its own clock; a shortfall within this is not a shortened certificate.
SHORTFALL_TOLERANCE = timedelta(minutes=10)

_PERIOD_LABELS = {
    CertificateLifetime.ONE_MONTH: "1 month",
    CertificateLifetime.TWO_MONTHS: "2 months",
    CertificateLifetime.THREE_MONTHS: "3 months",
    CertificateLifetime.FOUR_MONTHS: "4 months",
    CertificateLifetime.FIVE_MONTHS: "5 months",
    CertificateLifetime.SIX_MONTHS: "6 months",
    CertificateLifetime.ONE_YEAR: "1 year",
}
_DURATION = re.compile(r"(\d+)([smhd])")
_UNIT = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


class LifetimeRefused(Exception):
    """A lifetime that cannot be issued, with the status and sentence the operator gets."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class LifetimePlan:
    lifetime: CertificateLifetime
    # Exactly one of {"ttl": ...} or {"not_after": ...}: Vault refuses a request with both.
    params: Dict[str, str]
    requested_until: Optional[datetime]


@dataclass(frozen=True)
class IssuedLifetime:
    lifetime: CertificateLifetime
    expires_at: Optional[datetime]
    shortened: bool


def parse_duration(text: Any) -> Optional[timedelta]:
    """A Vault duration (``720h``, ``30d``, ``1h30m``, ``2592000``) as a timedelta; None if not one."""
    value = str(text or "").strip()
    if value.isdigit():
        return timedelta(seconds=int(value))
    parts = _DURATION.findall(value)
    if not parts or "".join(n + u for n, u in parts) != value:
        return None
    total = timedelta()
    for number, unit in parts:
        total += timedelta(**{_UNIT[unit]: int(number)})
    return total if total > timedelta() else None


def _day(moment: datetime) -> str:
    return f"{moment:%b} {moment.day}, {moment.year}"


def _span(duration: timedelta) -> str:
    days = duration.days
    if days >= 365 and days % 365 == 0:
        years = days // 365
        return f"{years} year" if years == 1 else f"{years} years"
    if days >= 1:
        return f"{days} day" if days == 1 else f"{days} days"
    hours = int(duration.total_seconds() // 3600)
    return f"{hours} hour" if hours == 1 else f"{hours} hours"


def ca_not_after(vault: Any) -> datetime:
    """Blocking. When the CA that signs cluster certificates expires."""
    try:
        pem = (vault.vault_read(CA_PATH, use_cache=False) or {}).get("certificate")
    except VaultClientError as exc:
        raise LifetimeRefused(
            503, "The certificate authority could not be read. Try again in a moment, or choose a period.",
        ) from exc
    end = parse_certificate_pem(pem).get("not_after") if isinstance(pem, str) and pem else None
    if not isinstance(end, datetime):
        raise LifetimeRefused(
            503, "The certificate authority could not be read. Try again in a moment, or choose a period.",
        )
    return end


def resolve(
    choice: Optional[CertificateLifetime],
    stored: Any,
    fallback: CertificateLifetime = CertificateLifetime.DEFAULT,
) -> CertificateLifetime:
    """The lifetime chosen now, else the one the cluster last got, else ``fallback``."""
    if choice is not None:
        return choice
    try:
        return CertificateLifetime(stored)
    except ValueError:
        return fallback


def plan(lifetime: CertificateLifetime, vault: Any, *, now: Optional[datetime] = None) -> LifetimePlan:
    """Blocking. The Vault parameters for ``lifetime``, or LifetimeRefused before anything is written."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    if lifetime is CertificateLifetime.DEFAULT:
        # Today's request, byte for byte, and no CA read on its path.
        duration = parse_duration(settings.vault_cert_ttl)
        return LifetimePlan(lifetime, {"ttl": settings.vault_cert_ttl}, now + duration if duration else None)
    if lifetime is CertificateLifetime.LONGEST:
        if not settings.cluster_cert_allow_longest:
            raise LifetimeRefused(422, "This deployment does not offer the longest certificate lifetime. Choose a period.")
        end = ca_not_after(vault) - LONGEST_MARGIN
        if end - now < LONGEST_FLOOR:
            raise LifetimeRefused(
                409, f"The certificate authority expires on {_day(end)}. Renew it, then choose the longest lifetime.",
            )
        return LifetimePlan(lifetime, {"not_after": end.strftime("%Y-%m-%dT%H:%M:%SZ")}, end)
    until = now + (parse_duration(lifetime.value) or timedelta())
    try:
        ca_end: Optional[datetime] = ca_not_after(vault)
    except LifetimeRefused:
        # Only `longest` needs the CA; for a period an unreadable CA leaves the decision to Vault.
        ca_end = None
    if ca_end is not None and until > ca_end:
        raise LifetimeRefused(
            422,
            f"The certificate authority expires on {_day(ca_end)}, before this period ends. "
            "Choose a shorter period or the longest lifetime.",
        )
    return LifetimePlan(lifetime, {"ttl": lifetime.value}, until)


def issue(vault: Any, common_name: str, lifetime_plan: LifetimePlan) -> Tuple[Dict[str, str], IssuedLifetime]:
    """Blocking. Issue the certificate and read its real expiry back from it."""
    issued = vault.generate_lxd_certificate(common_name=common_name, **lifetime_plan.params)
    expires = parse_certificate_pem(issued["cert"]).get("not_after")
    expires = expires if isinstance(expires, datetime) else None
    until = lifetime_plan.requested_until
    shortened = bool(expires and until and expires < until - SHORTFALL_TOLERANCE)
    return issued, IssuedLifetime(lifetime_plan.lifetime, expires, shortened)


def response_fields(issued: IssuedLifetime) -> Dict[str, Any]:
    """What a response says about the issued certificate's lifetime."""
    return {
        "certificate_lifetime": issued.lifetime,
        "certificate_expires_at": issued.expires_at,
        "lifetime_shortened": issued.shortened,
    }


def audit_fields(issued: IssuedLifetime) -> Dict[str, Any]:
    """What the activity log records: the lifetime asked for and the expiry Vault issued."""
    return {
        "certificate_lifetime": issued.lifetime.value,
        "certificate_expires_at": issued.expires_at.isoformat() if issued.expires_at else None,
        "lifetime_shortened": issued.shortened,
    }


def shortened_notice(issued: IssuedLifetime) -> str:
    until = _day(issued.expires_at) if issued.expires_at else "an earlier date"
    return (
        f"Vault issued it until {until}, shorter than the lifetime chosen: its PKI role caps how long "
        "a certificate may last."
    )


def options(vault: Any, *, now: Optional[datetime] = None) -> CertificateLifetimeOptions:
    """Blocking. What a lifetime picker offers, each with the date it would reach if issued now."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    try:
        ca_end: Optional[datetime] = ca_not_after(vault)
        ca_problem: Optional[str] = None
    except LifetimeRefused as exc:
        ca_end, ca_problem = None, exc.detail
    default_duration = parse_duration(settings.vault_cert_ttl)
    offered = [CertificateLifetimeOption(
        value=CertificateLifetime.DEFAULT,
        label=f"Default ({_span(default_duration)})" if default_duration else "Default",
        expires_at=now + default_duration if default_duration else None,
    )]
    # A period as long as the default is still offered: `default` follows the deployment's setting
    # when the cluster is renewed later, a period does not, and a stored period must stay selectable.
    for period, label in _PERIOD_LABELS.items():
        until = now + (parse_duration(period.value) or timedelta())
        fits = ca_end is None or until <= ca_end
        offered.append(CertificateLifetimeOption(
            value=period, label=label, expires_at=until, available=fits,
            reason=None if fits else f"The certificate authority expires on {_day(ca_end)}, before this period ends.",
        ))
    if settings.cluster_cert_allow_longest:
        if ca_end is None:
            offered.append(CertificateLifetimeOption(
                value=CertificateLifetime.LONGEST, label="Longest", available=False, reason=ca_problem,
            ))
        else:
            end = ca_end - LONGEST_MARGIN
            usable = end - now >= LONGEST_FLOOR
            offered.append(CertificateLifetimeOption(
                value=CertificateLifetime.LONGEST, label="Longest", expires_at=end, available=usable,
                reason=None if usable else f"The certificate authority expires on {_day(end)}. Renew it first.",
            ))
    return CertificateLifetimeOptions(default=CertificateLifetime.DEFAULT, options=offered)
