"""
Request security helpers for scanner detection and trusted client IP handling.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qsl, unquote

from starlette.requests import Request


DEFAULT_TRUSTED_PROXY_CIDRS = (
    "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "::1/128,fc00::/7,fe80::/10"
)
LOOPBACK_PROXY_CIDRS = "127.0.0.0/8,::1/128"

CLIENT_IP_HEADERS = (
    "x-orcastra-client-ip",
    "cf-connecting-ip",
    "true-client-ip",
    "x-real-ip",
    "x-forwarded-for",
    "forwarded",
)

TRAVERSAL_TARGETS = (
    "/etc/passwd",
    "/etc/shadow",
    "/proc/self/environ",
    "/proc/self/cmdline",
    "c:/windows/win.ini",
    "windows/win.ini",
)

SENSITIVE_SEGMENTS = {
    ".aws",
    ".azure",
    ".bzr",
    ".config",
    ".docker",
    ".dockerenv",
    ".env",
    ".git",
    ".hg",
    ".netrc",
    ".npmrc",
    ".runtimeconfig.json",
    ".ssh",
    ".svn",
    "adminer.php",
    "composer.json",
    "composer.lock",
    "docker-compose.override.yml",
    "docker-compose.prod.yml",
    "docker-compose.yml",
    "dockerfile",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "package-lock.json",
    "phpinfo.php",
    "server-status",
    "web.config",
}

SENSITIVE_PREFIXES = (
    "/adminer",
    "/actuator",
    "/cgi-bin",
    "/debug",
    "/horizon",
    "/laravel",
    "/phpmyadmin",
    "/telescope",
    "/vendor/phpunit",
    "/wp-admin",
    "/wp-content",
    "/wp-includes",
    "/wp-login",
)

SENSITIVE_SUFFIXES = (
    ".bak",
    ".backup",
    ".conf",
    ".config",
    ".dump",
    ".ini",
    ".old",
    ".orig",
    ".sql",
    ".sqlite",
    ".swp",
    ".tar",
    ".tgz",
)

RISKY_QUERY_KEYS = {
    "dir",
    "directory",
    "download",
    "file",
    "file_path",
    "filepath",
    "filename",
    "include",
    "page",
    "path",
    "template",
}


@dataclass(frozen=True)
class SecurityDecision:
    """Decision returned by scanner and traversal inspection."""

    status_code: int
    reason: str


@dataclass(frozen=True)
class ClientIpResolution:
    """Resolved client IP plus proxy metadata for logs and rate limiting."""

    client_ip: str
    direct_ip: str
    source: str
    trusted_proxy: bool


def decode_repeatedly(value: str, rounds: int = 3) -> str:
    """Decode URL escapes a few times to catch double-encoded probes."""
    decoded = value
    for _ in range(rounds):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def normalize_request_part(value: str) -> str:
    """Normalize a request path or query value for security matching."""
    return decode_repeatedly(value).replace("\\", "/").lower()


def has_path_traversal(value: str) -> bool:
    """Detect traversal markers and sensitive local file targets."""
    normalized = normalize_request_part(value)
    if "\x00" in normalized or "%00" in value.lower():
        return True

    segments = [segment for segment in normalized.split("/") if segment]
    if ".." in segments:
        return True

    compact = normalized.replace("//", "/")
    return any(target in compact for target in TRAVERSAL_TARGETS)


def _segments(path: str) -> list[str]:
    return [segment for segment in path.strip("/").split("/") if segment]


def is_sensitive_probe(value: str) -> bool:
    """Detect common secret, VCS, framework, and backup-file probes."""
    normalized = normalize_request_part(value).split("?", 1)[0]
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"

    path_segments = _segments(normalized)
    if any(segment in SENSITIVE_SEGMENTS for segment in path_segments):
        return True

    if any(normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in SENSITIVE_PREFIXES):
        return True

    return any(normalized.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)


def _has_risky_query_probe(query: str) -> bool:
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key.lower() not in RISKY_QUERY_KEYS:
            continue
        if has_path_traversal(value) or is_sensitive_probe(value):
            return True
    return False


def inspect_request_target(method: str, path: str, query: str = "") -> SecurityDecision | None:
    """Classify obvious scanner and traversal requests before routing."""
    normalized_path = normalize_request_part(path)

    if method.upper() not in {"GET", "HEAD", "OPTIONS"} and normalized_path in {"", "/"}:
        return SecurityDecision(status_code=405, reason="root_method_probe")

    if has_path_traversal(path) or _has_risky_query_probe(query):
        return SecurityDecision(status_code=404, reason="path_traversal")

    if is_sensitive_probe(path) or _has_risky_query_probe(query):
        return SecurityDecision(status_code=404, reason="sensitive_path_probe")

    return None


def _clean_ip_candidate(value: str | None) -> str | None:
    if not value:
        return None

    candidate = value.strip().strip('"')
    if not candidate or candidate.lower() == "unknown" or candidate.startswith("_"):
        return None

    if candidate.startswith("["):
        end_index = candidate.find("]")
        return candidate[1:end_index] if end_index > 0 else None

    if candidate.count(":") == 1 and "." in candidate.rsplit(":", 1)[0]:
        return candidate.rsplit(":", 1)[0]

    return candidate


def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = _clean_ip_candidate(value)
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _trusted_networks(cidr_text: str | None) -> Iterable[ipaddress._BaseNetwork]:
    # important: a configured list replaces the default rather than adding to it. Added, the
    # default's private ranges stayed trusted whatever was set, so a caller on a private network
    # reaching the backend port directly could name any client address (RC5 acceptance FU-6).
    # Loopback is always trusted: it can only be this host.
    cidr_sources = [LOOPBACK_PROXY_CIDRS, cidr_text or DEFAULT_TRUSTED_PROXY_CIDRS]

    seen_cidrs: set[str] = set()
    for cidrs in cidr_sources:
        for raw_cidr in cidrs.split(","):
            raw_cidr = raw_cidr.strip()
            if not raw_cidr or raw_cidr in seen_cidrs:
                continue
            seen_cidrs.add(raw_cidr)
            try:
                yield ipaddress.ip_network(raw_cidr, strict=False)
            except ValueError:
                continue


def _is_trusted_proxy(ip_value: str | None, cidr_text: str | None) -> bool:
    direct_ip = _parse_ip(ip_value)
    if not direct_ip:
        return False
    return any(direct_ip in network for network in _trusted_networks(cidr_text))


def own_address() -> str | None:
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return None


def untrusted_own_network_warning(cidr_text: str | None, *, own_ip: str | None) -> str | None:
    """A startup warning when this backend's own address is outside the trusted proxy list.

    note: the frontend proxy normally shares the backend's container network. A list that leaves
    that network out (one written when the default was still added to it) stops trusting the
    frontend, and every browser request is then counted against the frontend's one address.
    """
    if not own_ip or _is_trusted_proxy(own_ip, cidr_text):
        return None
    return (
        f"TRUSTED_PROXY_CIDRS does not include this backend's own address ({own_ip}). A proxy on "
        "the same network, such as the frontend, is not trusted to pass the client address, so "
        "every request through it shares one rate-limit bucket and one audit address. List every "
        "proxy in front of the backend, the frontend's network included."
    )


def _is_allowed_forwarded_ip(
    parsed: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allow_private_forwarded_ips: bool,
) -> bool:
    if allow_private_forwarded_ips:
        return True
    return parsed.is_global


def _forwarded_header_values(header_value: str) -> Iterable[str]:
    for forwarded_entry in header_value.split(","):
        for parameter in forwarded_entry.split(";"):
            key, separator, value = parameter.strip().partition("=")
            if separator and key.lower() == "for":
                yield value.strip()


def _header_ip_values(header_name: str, header_value: str) -> Iterable[str]:
    if header_name == "forwarded":
        yield from _forwarded_header_values(header_value)
        return

    yield from (part.strip() for part in header_value.split(","))


def _first_valid_header_ip(
    header_name: str,
    header_value: str | None,
    allow_private_forwarded_ips: bool = False,
) -> str | None:
    if not header_value:
        return None
    for raw_part in _header_ip_values(header_name, header_value):
        parsed = _parse_ip(raw_part)
        if parsed and _is_allowed_forwarded_ip(parsed, allow_private_forwarded_ips):
            return str(parsed)
    return None


def resolve_client_ip(
    request: Request,
    trusted_proxy_cidrs: str | None = None,
    allow_private_forwarded_ips: bool = False,
) -> ClientIpResolution:
    """Resolve client IP while trusting forwarding headers only from proxies."""
    direct_ip = request.client.host if request.client else "unknown"
    if not _is_trusted_proxy(direct_ip, trusted_proxy_cidrs):
        return ClientIpResolution(
            client_ip=direct_ip,
            direct_ip=direct_ip,
            source="direct",
            trusted_proxy=False,
        )

    for header_name in CLIENT_IP_HEADERS:
        candidate = _first_valid_header_ip(
            header_name,
            request.headers.get(header_name),
            allow_private_forwarded_ips,
        )
        if candidate:
            return ClientIpResolution(
                client_ip=candidate,
                direct_ip=direct_ip,
                source=header_name,
                trusted_proxy=True,
            )

    return ClientIpResolution(
        client_ip=direct_ip,
        direct_ip=direct_ip,
        source="direct",
        trusted_proxy=True,
    )


def get_client_ip(
    request: Request,
    trusted_proxy_cidrs: str | None = None,
    allow_private_forwarded_ips: bool = False,
) -> str:
    """Return the resolved client IP for rate limiting and access logs."""
    return resolve_client_ip(
        request,
        trusted_proxy_cidrs,
        allow_private_forwarded_ips,
    ).client_ip


def get_client_subnet(ip_value: str) -> str | None:
    """Return a subnet key for distributed scanner throttling."""
    parsed = _parse_ip(ip_value)
    if not parsed:
        return None
    prefix = 24 if parsed.version == 4 else 64
    network = ipaddress.ip_network(f"{parsed}/{prefix}", strict=False)
    return str(network)
