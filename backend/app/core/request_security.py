"""
Request security helpers for scanner detection and trusted client IP handling.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qsl, unquote

from starlette.requests import Request


DEFAULT_TRUSTED_PROXY_CIDRS = (
    "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "::1/128,fc00::/7,fe80::/10"
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


def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value:
        return None
    candidate = value.strip().split(":", 1)[0] if value.count(":") == 1 else value.strip()
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _trusted_networks(cidr_text: str | None) -> Iterable[ipaddress._BaseNetwork]:
    cidrs = cidr_text or DEFAULT_TRUSTED_PROXY_CIDRS
    for raw_cidr in cidrs.split(","):
        raw_cidr = raw_cidr.strip()
        if not raw_cidr:
            continue
        try:
            yield ipaddress.ip_network(raw_cidr, strict=False)
        except ValueError:
            continue


def _is_trusted_proxy(ip_value: str | None, cidr_text: str | None) -> bool:
    direct_ip = _parse_ip(ip_value)
    if not direct_ip:
        return False
    return any(direct_ip in network for network in _trusted_networks(cidr_text))


def _first_valid_header_ip(header_value: str | None) -> str | None:
    if not header_value:
        return None
    for raw_part in header_value.split(","):
        parsed = _parse_ip(raw_part)
        if parsed:
            return str(parsed)
    return None


def get_client_ip(request: Request, trusted_proxy_cidrs: str | None = None) -> str:
    """Return client IP while trusting forwarding headers only from proxies."""
    direct_ip = request.client.host if request.client else "unknown"
    if not _is_trusted_proxy(direct_ip, trusted_proxy_cidrs):
        return direct_ip

    for header_name in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        candidate = _first_valid_header_ip(request.headers.get(header_name))
        if candidate:
            return candidate

    return direct_ip


def get_client_subnet(ip_value: str) -> str | None:
    """Return a subnet key for distributed scanner throttling."""
    parsed = _parse_ip(ip_value)
    if not parsed:
        return None
    prefix = 24 if parsed.version == 4 else 64
    network = ipaddress.ip_network(f"{parsed}/{prefix}", strict=False)
    return str(network)
