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

important: an address is classified by what it *reaches*, not by how it is spelt. A membership
test against an IPv4 network answers False for an IPv6Address rather than raising, so
`::ffff:127.0.0.1` walked straight past the earlier prefix-list-only check while connecting to
loopback. Every literal is unwrapped to the address it tunnels to before it is judged, and the
prefix list is backed by `is_global` so a range nobody thought to list is still refused.

note: DNS is resolved here and again by the HTTP client, so a name that answers differently
between the two calls can still slip through. Closing that needs the connection pinned to the
address that was checked, which is what `core/safe_http.py` does for outbound deliveries. This
module stays the boundary validator, shaped for a Pydantic refusal; it is not the last line.
"""

import ipaddress
import logging
import socket
from typing import FrozenSet, Optional, Tuple, Union
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# Ranges a server-side fetch has no business reaching.
#
# important: 100.64.0.0/10 is carrier-grade NAT, which is also the Tailscale range this
# deployment's own hosts live on. Blocking it is deliberate and is the difference between
# refusing a request to the internet and refusing a request to Vault. Python does not count it
# as private, so it has to be listed even though `is_global` backs this list.
#
# 64:ff9b::/96 is NAT64. It is the one tunnel form `is_global` calls global, because the address
# it embeds may itself be public, so a gateway would forward it. We do not run a NAT64 gateway,
# and a receiver behind one is not a deployment shape this platform supports.
PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("2001::/32"),
]

BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "metadata.azure.com",
    "instance-data",
}

SAFE_SCHEMES = ("http", "https")

DEFAULT_PORTS = {"http": 80, "https": 443}

# One reason string for every way an address turns out to be internal. The refusal names the
# category and never the address, so a caller cannot walk it to map the network.
_INTERNAL = "host resolves to an internal address"

_MAX_UNWRAP_HOPS = 4


def _unwrap_address(address: IPAddress) -> IPAddress:
    """Follow a mapped or tunnelled literal down to the address it actually reaches.

    `::ffff:10.0.0.1`, `2002:0a00:0001::` and a Teredo literal all connect somewhere a prefix
    list written in IPv4 will never match on its own.
    """
    for _ in range(_MAX_UNWRAP_HOPS):
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
            continue
        sixtofour = getattr(address, "sixtofour", None)
        if sixtofour is not None:
            address = sixtofour
            continue
        teredo = getattr(address, "teredo", None)
        if teredo is not None:
            # (server, client). The client is the endpoint a packet ends up at.
            address = teredo[1]
            continue
        break
    return address


def _address_risk(address: IPAddress) -> Optional[str]:
    """Why this address is unsafe to connect to, or None.

    Judges the literal and what it unwraps to, because either one reaching inside is enough.
    """
    for candidate in (address, _unwrap_address(address)):
        for network in PRIVATE_NETWORKS:
            if candidate.version == network.version and candidate in network:
                return _INTERNAL
        if not candidate.is_global or candidate.is_multicast or candidate.is_unspecified:
            return _INTERNAL
    return None


def describe_url_risk(
    url: str,
    *,
    require_https: bool = False,
    resolve: bool = True,
    allowed_ports: Optional[FrozenSet[int]] = None,
) -> Optional[str]:
    """Why this URL is unsafe to fetch, or None when it is safe.

    Returns a reason rather than a bool so the caller can say what was wrong. The reason names
    the category, never the resolved address, so a refusal cannot be used to map the network.

    `allowed_ports` is opt-in. A callback base registered years ago may legitimately sit on a
    non-standard port, so the general validator does not constrain it; the surfaces where we
    choose the policy pass their own set.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "malformed URL"

    scheme = parsed.scheme.lower()
    if scheme not in SAFE_SCHEMES:
        return f"scheme must be one of {', '.join(SAFE_SCHEMES)}"

    if require_https and scheme != "https":
        return "must use https"

    # important: userinfo is not decoration. httpx turns `https://user:pass@host/` into an
    # Authorization header, so a stored URL becomes a way to smuggle a credential into our own
    # outbound requests and our own logs.
    if parsed.username or parsed.password:
        return "URL must not carry credentials"

    try:
        port = parsed.port
    except ValueError:
        return "malformed URL"

    if allowed_ports is not None:
        effective = port if port is not None else DEFAULT_PORTS.get(scheme)
        if effective not in allowed_ports:
            return "port is not one this server may call"

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

    if not resolved:
        return "host does not resolve"

    for *_, sockaddr in resolved:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return "host resolves to an address this server cannot classify"
        risk = _address_risk(address)
        if risk is not None:
            return risk

    return None


def resolve_safe_addresses(
    host: str,
    *,
    port: Optional[int] = None,
) -> Tuple[str, ...]:
    """Every address `host` answers with, refusing outright if any of them reaches inside.

    Refuses rather than filtering. A name that answers with a mix of public and internal
    addresses is hostile, not partly usable, and picking the safe one would leave the next
    lookup free to return the other.

    Raises ValueError with a category reason, matching this module's refusal contract.
    """
    if host.lower().rstrip(".") in BLOCKED_HOSTNAMES:
        raise ValueError("host is not routable from this server")
    try:
        resolved = socket.getaddrinfo(host, port)
    except Exception:
        raise ValueError("host does not resolve")

    addresses = []
    for *_, sockaddr in resolved:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            raise ValueError("host resolves to an address this server cannot classify")
        risk = _address_risk(address)
        if risk is not None:
            raise ValueError(risk)
        addresses.append(sockaddr[0])

    if not addresses:
        raise ValueError("host does not resolve")
    # dict.fromkeys rather than a set, so the order getaddrinfo chose survives.
    return tuple(dict.fromkeys(addresses))


def is_url_safe(
    url: str,
    *,
    require_https: bool = False,
    resolve: bool = True,
    allowed_ports: Optional[FrozenSet[int]] = None,
) -> bool:
    return (
        describe_url_risk(
            url, require_https=require_https, resolve=resolve, allowed_ports=allowed_ports
        )
        is None
    )


def assert_url_is_fetchable(
    url: str,
    *,
    field: str,
    require_https: bool = False,
    allowed_ports: Optional[FrozenSet[int]] = None,
) -> str:
    """Return the URL, or raise ValueError naming the field and the reason.

    Shaped for a Pydantic validator so the refusal lands at the request boundary with the other
    422s, rather than as a 500 from a background task that already accepted the value.
    """
    reason = describe_url_risk(url, require_https=require_https, allowed_ports=allowed_ports)
    if reason is not None:
        logger.warning("Refusing unsafe %s: %s", field, reason)
        raise ValueError(f"{field} is not a URL this server may call: {reason}")
    return url


def split_scheme_host(url: str) -> Tuple[str, str]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), (parsed.hostname or "")


def normalize_endpoint(endpoint: Optional[str]) -> str:
    """Strip whitespace and trailing slashes from a hypervisor endpoint.

    important: LXD's router does not collapse a double slash. An endpoint stored as
    "https://host:8443/" joined to "/1.0/instances" requests "//1.0/instances", which answers
    404 rather than the 403 that means "certificate not trusted yet" - so a healthy cluster
    reads as an unexpected protocol error. Every place that builds a path onto a stored
    endpoint must go through here.
    """
    return (endpoint or "").strip().rstrip("/")
