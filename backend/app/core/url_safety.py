"""Whether a caller-supplied URL is safe for this server to fetch.

A URL a caller stores and the server later requests is an SSRF sink: the request leaves from
inside the network boundary, where Vault, Redis, Postgres and a link-local metadata service may
be reachable.

important: an address is classified by what it *reaches*, not by how it is spelt. A membership
test against an IPv4 network answers False for an IPv6Address instead of raising, so
`::ffff:127.0.0.1` would pass a prefix-list-only check while connecting to loopback. Every
literal is unwrapped to the address it tunnels to before it is judged, and the prefix list is
backed by `is_global` so an unlisted range is still refused. DNS is resolved here and again by
the HTTP client, so a name that answers differently the second time passes this check alone;
fetch through `core/safe_http.py`, which pins the connection to the checked address.
"""

import ipaddress
import logging
import socket
from typing import Any, FrozenSet, Optional, Sequence, Tuple, Union
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# Ranges a server-side fetch must not reach.
# important: 100.64.0.0/10 is carrier-grade NAT, which Tailscale also assigns to its nodes, so a
# deployment's own hosts (Vault included) can live there. Python does not count it as private.
# 64:ff9b::/96 is NAT64, the one tunnel form `is_global` calls global. A receiver behind a NAT64
# gateway is not a supported deployment shape.
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
    """Follow a mapped or tunnelled literal down to the address it reaches.

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


def _address_risk(
    address: IPAddress,
    exemptions: Sequence[Any] = (),
) -> Optional[str]:
    """Why this address is unsafe to connect to, or None.

    Judges the literal and what it unwraps to, because either one reaching inside is enough.
    """
    for candidate in (address, _unwrap_address(address)):
        # An operator-declared exemption: a webhook test receiver on a development deployment
        # (`webhook_destination_exemptions` is empty unless DEBUG is on), or an operator's own
        # feed mirror on a private network (`_mirror_exemptions` in core/config.py).
        if any(
            candidate.version == network.version and candidate in network
            for network in exemptions
        ):
            return None
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
    exemptions: Sequence[Any] = (),
) -> Optional[str]:
    """Why this URL is unsafe to fetch, or None when it is safe.

    The reason names the category, never the resolved address, so a refusal cannot map the
    network. `allowed_ports` is opt-in: a registered callback base may sit on a non-standard
    port, so only surfaces with their own port policy pass a set.
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

    # important: httpx turns the userinfo of `https://user:pass@host/` into an Authorization
    # header, so a stored URL could smuggle a credential into this server's outbound requests
    # and logs.
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
        risk = _address_risk(address, exemptions)
        if risk is not None:
            return risk

    return None


def resolve_safe_addresses(
    host: str,
    *,
    port: Optional[int] = None,
    exemptions: Sequence[Any] = (),
) -> Tuple[str, ...]:
    """Every address `host` answers with, refusing outright if any of them reaches inside.

    A name that answers with a mix of public and internal addresses is hostile; picking the safe
    one would leave the next lookup free to return the other. Raises ValueError with a category
    reason.
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
        risk = _address_risk(address, exemptions)
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

    important: LXD's router does not collapse a double slash: "https://host:8443/" joined to
    "/1.0/instances" answers 404 instead of the 403 that means "certificate not trusted yet".
    Every place that builds a path onto a stored endpoint must go through here.
    """
    return (endpoint or "").strip().rstrip("/")
