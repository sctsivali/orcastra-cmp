"""Whether a caller-supplied URL is safe for this server to fetch.

A URL a caller stores and the server later requests is an SSRF sink: the request leaves from
inside the network boundary, carrying whatever credentials the caller's integration is entitled
to. This deployment sits on a Tailscale network alongside Vault, Redis and Postgres, and on
infrastructure with a link-local metadata service, so "the server will fetch this for you" reaches
considerably more than the public internet.

The logic here started as `_is_ssrf_safe` in `routers/user_preferences.py`, where it guarded the
image proxy and nothing else. It was never applied to `app_url`, the integration callback base,
which is stored per API key and then used for three separate outbound requests, one of which
carries the key's own plaintext secret. Having the right check in the codebase and not calling it
is worse than not having it, because it reads as covered.

note: DNS is resolved here and again by the HTTP client, so a name that answers differently
between the two calls can still slip through. Closing that needs the connection pinned to the
address that was checked, which is a larger change than this module. The bound is stated rather
than implied.
"""

import ipaddress
import logging
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Ranges a server-side fetch has no business reaching.
#
# important: 100.64.0.0/10 is carrier-grade NAT, which is also the Tailscale range this
# deployment's own hosts live on. Blocking it is deliberate and is the difference between
# refusing a request to the internet and refusing a request to Vault.
PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "metadata.goog"}

SAFE_SCHEMES = ("http", "https")


def describe_url_risk(
    url: str,
    *,
    require_https: bool = False,
    resolve: bool = True,
) -> Optional[str]:
    """Why this URL is unsafe to fetch, or None when it is safe.

    Returns a reason rather than a bool so the caller can say what was wrong. The reason names
    the category, never the resolved address, so a refusal cannot be used to map the network.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "malformed URL"

    if parsed.scheme.lower() not in SAFE_SCHEMES:
        return f"scheme must be one of {', '.join(SAFE_SCHEMES)}"

    if require_https and parsed.scheme.lower() != "https":
        return "must use https"

    host = (parsed.hostname or "").strip()
    if not host:
        return "no host"

    if host.lower().rstrip(".") in BLOCKED_HOSTNAMES:
        return "host is not routable from this server"

    if not resolve:
        return None

    try:
        resolved = socket.getaddrinfo(host, None)
    except Exception:
        # A name that will not resolve cannot be fetched anyway, and refusing here keeps the
        # failure at the boundary where the operator sees it rather than in a background task.
        return "host does not resolve"

    for *_, sockaddr in resolved:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return "host resolves to an address this server cannot classify"
        if any(address in network for network in PRIVATE_NETWORKS):
            return "host resolves to an internal address"

    return None


def is_url_safe(url: str, *, require_https: bool = False, resolve: bool = True) -> bool:
    return describe_url_risk(url, require_https=require_https, resolve=resolve) is None


def assert_url_is_fetchable(url: str, *, field: str, require_https: bool = False) -> str:
    """Return the URL, or raise ValueError naming the field and the reason.

    Shaped for a Pydantic validator so the refusal lands at the request boundary with the other
    422s, rather than as a 500 from a background task that already accepted the value.
    """
    reason = describe_url_risk(url, require_https=require_https)
    if reason is not None:
        logger.warning("Refusing unsafe %s: %s", field, reason)
        raise ValueError(f"{field} is not a URL this server may call: {reason}")
    return url


def split_scheme_host(url: str) -> Tuple[str, str]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), (parsed.hostname or "")
