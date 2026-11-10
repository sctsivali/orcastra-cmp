"""The SSRF check judges an address by what it reaches, whatever spelling it arrives in.

Python answers False, rather than raising, when asked whether an `IPv6Address` is in an
`IPv4Network`, so every IPv4-mapped or tunnelled literal is unwrapped before it is judged:

    https://[::ffff:127.0.0.1]/          -> loopback
    https://[0:0:0:0:0:ffff:6449:7f4b]/  -> carrier-grade NAT (100.64.0.0/10)
    https://[::ffff:169.254.169.254]/    -> the cloud metadata service

The explicit prefix list is backed by `is_global`, so an unlisted range is refused too. A URL
carrying userinfo is refused because httpx turns it into an Authorization header.
"""

import ast
import socket
from pathlib import Path

import httpx
import pytest

from app.core.safe_http import SafeHTTPError, fetch_guarded
from app.core.url_safety import (
    PRIVATE_NETWORKS,
    describe_url_risk,
    is_url_safe,
    resolve_safe_addresses,
)

# The fixture below resolves this host to a public address, so the DNS half of the check runs.
PUBLIC = "https://example.com"


@pytest.fixture(autouse=True)
def _documented_public_host_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the SSRF suite hermetic while still exercising DNS address classification."""
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host == "example.com":
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", port or 443),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr("app.core.url_safety.socket.getaddrinfo", resolve)


# ── mapped and tunnelled spellings ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "https://[::ffff:127.0.0.1]/",
        "https://[::ffff:10.0.0.1]/",
        "https://[::ffff:192.168.1.1]/",
        "https://[::ffff:169.254.169.254]/latest/meta-data/",
        "https://[0:0:0:0:0:ffff:6449:7f4b]/",   # IPv4-mapped, inside 100.64.0.0/10
        "https://[2002:c0a8:0101::1]/",          # 6to4 wrapping 192.168.1.1
        "https://[2001::1]/",                    # Teredo
        "https://[64:ff9b::8.8.8.8]/",           # NAT64
    ],
)
def test_a_tunnelled_or_mapped_literal_is_refused(url: str) -> None:
    """Each literal is unwrapped to the IPv4 address it reaches, and that address is refused."""
    assert not is_url_safe(url, require_https=True), f"{url} was accepted"


def test_the_plain_form_of_the_same_address_is_still_refused() -> None:
    """The plain IPv4 spelling stays refused alongside the mapped one."""
    assert not is_url_safe("https://127.0.0.1/", require_https=True)
    assert not is_url_safe("https://169.254.169.254/", require_https=True)


# ── ranges outside the explicit list ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "https://192.0.0.1/",     # IETF protocol assignments
        "https://198.18.0.1/",    # benchmarking
        "https://224.0.0.1/",     # multicast, and is_global calls this one global
        "https://240.0.0.1/",     # reserved
        "https://0.0.0.0/",
    ],
)
def test_a_range_outside_the_original_list_is_refused(url: str) -> None:
    assert not is_url_safe(url, require_https=True), f"{url} was accepted"


def test_carrier_grade_nat_is_still_refused_by_the_explicit_list() -> None:
    """100.64.0.0/10 is why the prefix list survives `is_global`.

    Python does not classify it as private, and Tailscale hosts sit on it, so `is_global` alone
    would leave it open.
    """
    assert not is_url_safe("https://100.73.127.75/", require_https=True)
    assert any(str(net) == "100.64.0.0/10" for net in PRIVATE_NETWORKS)


# ── credentials and ports ──────────────────────────────────────────────────────────────────────

def test_a_url_carrying_credentials_is_refused() -> None:
    """httpx turns userinfo into an Authorization header, smuggling a credential out."""
    reason = describe_url_risk("https://user:pass@example.com/", require_https=True)
    assert reason == "URL must not carry credentials"


def test_the_port_policy_is_opt_in() -> None:
    """A registered callback base may sit on a non-standard port.

    The general validator does not constrain it; a surface that wants a port policy passes its
    own set, so an existing integration keeps working on upgrade.
    """
    assert is_url_safe(f"{PUBLIC}:8443/hooks", require_https=True)
    assert describe_url_risk(
        f"{PUBLIC}:8443/hooks", require_https=True, allowed_ports=frozenset({443})
    ) == "port is not one this server may call"
    assert is_url_safe(f"{PUBLIC}/hooks", require_https=True, allowed_ports=frozenset({443}))


# ── public addresses still pass ────────────────────────────────────────────────────────────────

def test_a_public_callback_is_still_accepted() -> None:
    assert is_url_safe(f"{PUBLIC}/hooks", require_https=True)


def test_the_refusal_never_names_the_address() -> None:
    """The refusal gives the reason and leaks no address from the internal network."""
    for url in ("https://127.0.0.1/", "https://[::ffff:127.0.0.1]/", "https://100.73.127.75/"):
        reason = describe_url_risk(url, require_https=True)
        assert reason
        assert "127.0.0.1" not in reason
        assert "100.73" not in reason
        assert "internal" in reason


# ── pinning the addresses a later fetch may use ────────────────────────────────────────────────

def test_pinning_returns_the_addresses_a_public_name_answers_with() -> None:
    addresses = resolve_safe_addresses("example.com", port=443)
    assert addresses
    assert all(isinstance(a, str) for a in addresses)


def test_pinning_refuses_rather_than_filtering() -> None:
    """A name that answers with any internal address is refused as a whole.

    Picking the safe address out of the set would leave the next lookup free to return the other
    one, a DNS rebinding window.
    """
    with pytest.raises(ValueError) as exc:
        resolve_safe_addresses("localhost")
    assert "internal" in str(exc.value) or "not routable" in str(exc.value)


# ── the guarded client ─────────────────────────────────────────────────────────────────────────

def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_a_body_past_the_cap_is_abandoned_rather_than_read() -> None:
    """The cap applies while streaming, so a responder cannot decide a worker's memory use."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4096, headers={"content-type": "image/png"})

    with pytest.raises(SafeHTTPError) as exc:
        _run(
            fetch_guarded(
                f"{PUBLIC}/big.png",
                max_bytes=1024,
                transport=httpx.MockTransport(handler),
            )
        )
    assert "size limit" in str(exc.value)


def test_a_redirect_to_an_internal_address_is_refused_at_the_hop() -> None:
    """Every hop is checked: the first URL says nothing about where a 302 leads."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    with pytest.raises(SafeHTTPError) as exc:
        _run(
            fetch_guarded(
                f"{PUBLIC}/logo.png",
                max_bytes=1024,
                transport=httpx.MockTransport(handler),
            )
        )
    assert "internal" in str(exc.value)


def test_an_ordinary_redirect_is_still_followed() -> None:
    """CDNs redirect, so a redirect to a public address is followed."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/logo.png":
            return httpx.Response(302, headers={"location": "/real-logo.png"})
        return httpx.Response(200, content=b"PNG", headers={"content-type": "image/png"})

    content_type, body = _run(
        fetch_guarded(
            f"{PUBLIC}/logo.png", max_bytes=1024, transport=httpx.MockTransport(handler)
        )
    )
    assert content_type == "image/png"
    assert body == b"PNG"


def test_a_redirect_loop_terminates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"{PUBLIC}/again"})

    with pytest.raises(SafeHTTPError) as exc:
        _run(
            fetch_guarded(
                f"{PUBLIC}/start", max_bytes=1024, transport=httpx.MockTransport(handler)
            )
        )
    assert "redirect" in str(exc.value)


# ── the ratchet ────────────────────────────────────────────────────────────────────────────────

def test_only_one_module_decides_what_is_a_private_address() -> None:
    """A second private-range table would drift from url_safety.py and miss its fixes."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    owner = app_dir / "core" / "url_safety.py"

    def hardcoded_ranges(tree: ast.AST) -> int:
        """Counts `ip_network("10.0.0.0/8")`: a range written into the source as a constant.

        `ip_network(raw_cidr)` parsing TRUSTED_PROXY_CIDRS and `ip_network(f"{addr}/{prefix}")`
        deriving a rate-limit subnet key are not counted.
        """
        return sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "ip_network"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        )

    offenders = []
    for path in app_dir.rglob("*.py"):
        if path == owner:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        count = hardcoded_ranges(tree)
        # One literal is a one-off comparison. Several is a table.
        if count > 1:
            offenders.append(f"{path.relative_to(app_dir)} hardcodes {count} ranges")

    assert not offenders, (
        "a second private-network table appeared; url_safety.py owns this decision: "
        + "; ".join(offenders)
    )
