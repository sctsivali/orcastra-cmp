"""Whether a URL is one this server may call on somebody else's behalf.

A URL a caller supplies and the server later requests is a server-side request forgery sink: the
request leaves from inside the network boundary, next to Vault, Redis and Postgres, and carries
whatever credential the caller's integration is entitled to.

important: an address is judged by what it reaches, not by how it is spelt. A membership test
against an IPv4 network answers False for an IPv6 address instead of raising, so `::ffff:127.0.0.1`
walks past a prefix list written in IPv4 while connecting to loopback. Every literal is unwrapped to
the address it tunnels to before it is judged, and the prefix list is backed by `is_global` so a
range nobody thought to list is still refused.

Two halves, deliberately separate:
  * check_url is pure. It never resolves a name, so it is safe inside a Pydantic validator, which
    runs on the event loop.
  * resolve_public_addresses resolves off the loop and refuses a name that answers with any
    internal address. core/safe_http.py calls it on every hop and connects to the address it
    judged, so a name cannot answer differently between the check and the connection.
"""

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import FrozenSet, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# important: 100.64.0.0/10 is carrier-grade NAT, which is also the Tailscale range this deployment's
# own hosts live on. Python does not count it as private, so it is listed even though is_global
# backs this list. 64:ff9b::/96 is NAT64, the one tunnel form is_global calls global.
PRIVATE_NETWORKS: Tuple[IPNetwork, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "0.0.0.0/8",
        "169.254.0.0/16",
        "192.0.0.0/24",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2002::/16",
        "2001::/32",
    )
)

# Cloud metadata endpoints. Refused even inside an operator-allowed network: no integration has a
# reason to reach one, and an allowlist entry broad enough to cover one is a mistake.
NEVER_ALLOWED: FrozenSet[IPAddress] = frozenset(
    {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}
)

BLOCKED_HOSTNAMES: FrozenSet[str] = frozenset(
    {
        "localhost",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "metadata.azure.com",
        "instance-data",
    }
)

SAFE_SCHEMES: Tuple[str, ...] = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}

# One reason for every way an address turns out to be internal. A refusal names the category and
# never the address, so it cannot be used to map the network.
INTERNAL = "host resolves to an internal address"
_MAX_UNWRAP_HOPS = 4


class UnsafeURLError(ValueError):
    """A URL or address this server must not call. The message is a category, never an address."""


@dataclass(frozen=True)
class URLTarget:
    scheme: str
    host: str
    port: int
    url: str


def _unwrap(address: IPAddress) -> IPAddress:
    """Follow a mapped or tunnelled literal down to the address it actually reaches."""
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
            address = teredo[1]
            continue
        break
    return address


def address_risk(
    address: Union[str, IPAddress], *, allowed_networks: Sequence[IPNetwork] = ()
) -> Optional[str]:
    """Why connecting to this address is unsafe, or None.

    Judges the literal and what it unwraps to, because either one reaching inside is enough. An
    address inside an operator-allowed network passes, except a metadata endpoint, which never does.
    """
    try:
        literal = ipaddress.ip_address(address) if isinstance(address, str) else address
    except ValueError:
        return "host resolves to an address this server cannot classify"
    for candidate in (literal, _unwrap(literal)):
        if candidate in NEVER_ALLOWED:
            return INTERNAL
    for candidate in (literal, _unwrap(literal)):
        if any(candidate.version == net.version and candidate in net for net in allowed_networks):
            continue
        if any(candidate.version == net.version and candidate in net for net in PRIVATE_NETWORKS):
            return INTERNAL
        if not candidate.is_global or candidate.is_multicast or candidate.is_unspecified:
            return INTERNAL
    return None


def check_url(
    url: str,
    *,
    require_https: bool = False,
    allowed_ports: Optional[FrozenSet[int]] = None,
    allowed_networks: Sequence[IPNetwork] = (),
) -> URLTarget:
    """Validate a URL without any I/O and return its parts, or raise UnsafeURLError.

    note: a hostname is only checked against the blocked names here. Whether it resolves inside is
    decided by resolve_public_addresses at the moment of the request, because a name can answer
    differently later and a check made now would vouch for an address nobody connects to.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("malformed URL")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise UnsafeURLError("malformed URL") from None
    scheme = (parts.scheme or "").lower()
    if scheme not in SAFE_SCHEMES:
        raise UnsafeURLError(f"scheme must be one of {', '.join(SAFE_SCHEMES)}")
    if require_https and scheme != "https":
        raise UnsafeURLError("must use https")
    # important: httpx turns https://user:pass@host/ into an Authorization header, so a stored URL
    # would smuggle a credential into our own outbound requests and our own logs.
    if parts.username or parts.password:
        raise UnsafeURLError("URL must not carry credentials")
    effective_port = port if port is not None else DEFAULT_PORTS[scheme]
    if allowed_ports is not None and effective_port not in allowed_ports:
        raise UnsafeURLError("port is not one this server may call")
    host = (parts.hostname or "").strip()
    if not host:
        raise UnsafeURLError("no host")
    if host.lower().rstrip(".") in BLOCKED_HOSTNAMES:
        raise UnsafeURLError("host is not routable from this server")
    try:
        literal: Optional[IPAddress] = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        risk = address_risk(literal, allowed_networks=allowed_networks)
        if risk is not None:
            raise UnsafeURLError(risk)
    return URLTarget(scheme=scheme, host=host, port=effective_port, url=url.strip())


def _getaddrinfo(host: str, port: int) -> list:
    """The resolver. A module-level seam so tests can answer without the network."""
    return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)


async def resolve_public_addresses(
    host: str,
    port: int,
    *,
    allowed_networks: Sequence[IPNetwork] = (),
    timeout: float = 3.0,
) -> Tuple[str, ...]:
    """Every address `host` answers with, refusing outright if any of them reaches inside.

    Refuses rather than filtering: a name that answers with a mix of public and internal addresses
    is hostile, and picking the safe one leaves the next lookup free to return the other. Runs the
    lookup on the default executor, never on the event loop. IPv4 answers come first.
    """
    if host.lower().rstrip(".") in BLOCKED_HOSTNAMES:
        raise UnsafeURLError("host is not routable from this server")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        risk = address_risk(literal, allowed_networks=allowed_networks)
        if risk is not None:
            raise UnsafeURLError(risk)
        return (str(literal),)
    loop = asyncio.get_running_loop()
    try:
        answers = await asyncio.wait_for(
            loop.run_in_executor(None, _getaddrinfo, host, port), timeout=timeout
        )
    except (asyncio.TimeoutError, OSError, UnicodeError):
        raise UnsafeURLError("host does not resolve") from None
    addresses = []
    for *_, sockaddr in answers or []:
        risk = address_risk(str(sockaddr[0]), allowed_networks=allowed_networks)
        if risk is not None:
            raise UnsafeURLError(risk)
        addresses.append(str(sockaddr[0]))
    if not addresses:
        raise UnsafeURLError("host does not resolve")
    unique = list(dict.fromkeys(addresses))
    return tuple(sorted(unique, key=lambda a: ipaddress.ip_address(a).version))


def validate_url_field(
    value: str,
    *,
    field: str,
    require_https: bool = False,
    allowed_networks: Sequence[IPNetwork] = (),
) -> str:
    """Pydantic adapter: return the URL or raise ValueError naming the field and the reason.

    No DNS here, so a validator never blocks the event loop. The use-time check in safe_http is the
    one that resolves.
    """
    try:
        check_url(value, require_https=require_https, allowed_networks=allowed_networks)
    except UnsafeURLError as exc:
        logger.warning("Refusing unsafe %s: %s", field, exc)
        raise ValueError(f"{field} is not a URL this server may call: {exc}") from None
    return value
