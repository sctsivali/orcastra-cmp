"""The SSRF check was called everywhere it needed to be, and it was wrong.

`describe_url_risk` tested each resolved address for membership in a list of IPv4 and IPv6
networks. Python answers that test False rather than raising when the address and the network are
different families, so an `IPv6Address` was never in an `IPv4Network`. Every IPv4-mapped IPv6
literal walked straight through:

    https://[::ffff:127.0.0.1]/          -> accepted, connects to loopback
    https://[::ffff:10.0.0.1]/           -> accepted
    https://[0:0:0:0:0:ffff:6449:7f4b]/  -> accepted, this deployment's own dashboard VM
    https://[::ffff:169.254.169.254]/    -> accepted, the cloud metadata service

The plain form of every one of those was correctly refused, which is what made it survive review:
the tests asserted `https://127.0.0.1/` was blocked and it was. A sink that is guarded for the
spelling everyone checks and open for the spelling nobody does reads as covered.

Three smaller holes rode along. The prefix list omitted 192.0.0.0/24, 198.18.0.0/15, multicast
and 240.0.0.0/4. A URL carrying userinfo was accepted, and httpx turns `https://user:pass@host/`
into an Authorization header, so a stored callback became a way to smuggle a credential into our
own outbound requests. And nothing constrained the port.

The fix judges an address by what it reaches: every mapped or tunnelled literal is unwrapped
first, and the explicit prefix list is backed by `is_global` so a range nobody thought to write
down is refused anyway. The prefix list still earns its place for 100.64.0.0/10, which Python
does not count as private and which is the Tailscale range this deployment's own hosts sit on.
"""

import ast
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

# A host that resolves to real public addresses, so the DNS half of the check runs for real.
PUBLIC = "https://example.com"


# ── the spelling that walked through ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "https://[::ffff:127.0.0.1]/",
        "https://[::ffff:10.0.0.1]/",
        "https://[::ffff:192.168.1.1]/",
        "https://[::ffff:169.254.169.254]/latest/meta-data/",
        "https://[0:0:0:0:0:ffff:6449:7f4b]/",   # ::ffff:100.73.127.75, the dashboard VM
        "https://[2002:c0a8:0101::1]/",          # 6to4 wrapping 192.168.1.1
        "https://[2001::1]/",                    # Teredo
        "https://[64:ff9b::8.8.8.8]/",           # NAT64
    ],
)
def test_a_tunnelled_or_mapped_literal_is_refused(url: str) -> None:
    """Each of these was accepted before the address was unwrapped before being judged."""
    assert not is_url_safe(url, require_https=True), f"{url} was accepted"


def test_the_plain_form_of_the_same_address_is_still_refused() -> None:
    """The half that always worked. Both spellings have to stay closed, not just the new one."""
    assert not is_url_safe("https://127.0.0.1/", require_https=True)
    assert not is_url_safe("https://169.254.169.254/", require_https=True)


# ── the ranges nobody listed ───────────────────────────────────────────────────────────────────

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

    Python does not classify it as private, and it is the Tailscale range this deployment's own
    Vault and Postgres are reachable on. Dropping the list in favour of `is_global` alone would
    reopen the one range that matters most here.
    """
    assert not is_url_safe("https://100.73.127.75/", require_https=True)
    assert any(str(net) == "100.64.0.0/10" for net in PRIVATE_NETWORKS)


# ── credentials and ports ──────────────────────────────────────────────────────────────────────

def test_a_url_carrying_credentials_is_refused() -> None:
    """httpx turns userinfo into an Authorization header, so this is credential smuggling."""
    reason = describe_url_risk("https://user:pass@example.com/", require_https=True)
    assert reason == "URL must not carry credentials"


def test_the_port_policy_is_opt_in() -> None:
    """A callback base registered years ago may sit on a non-standard port.

    So the general validator does not constrain it and the surfaces that choose a policy pass
    their own set. Constraining it globally would refuse a live integration on upgrade.
    """
    assert is_url_safe(f"{PUBLIC}:8443/hooks", require_https=True)
    assert describe_url_risk(
        f"{PUBLIC}:8443/hooks", require_https=True, allowed_ports=frozenset({443})
    ) == "port is not one this server may call"
    assert is_url_safe(f"{PUBLIC}/hooks", require_https=True, allowed_ports=frozenset({443}))


# ── the guard must not be so blunt that nothing can register ───────────────────────────────────

def test_a_public_callback_is_still_accepted() -> None:
    assert is_url_safe(f"{PUBLIC}/hooks", require_https=True)


def test_the_refusal_never_names_the_address() -> None:
    """An integrator needs the reason. Nobody needs a network map out of an error string."""
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
    """A name answering with a mix is hostile, not partly usable.

    Picking the safe address out of the set would leave the next lookup free to return the other
    one, which is the rebinding window this exists to close.
    """
    with pytest.raises(ValueError) as exc:
        resolve_safe_addresses("localhost")
    assert "internal" in str(exc.value) or "not routable" in str(exc.value)


# ── the guarded client ─────────────────────────────────────────────────────────────────────────

def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_a_body_past_the_cap_is_abandoned_rather_than_read() -> None:
    """The image proxy read the whole response and measured it afterwards.

    A responder that answers 200 with a gigabyte decided how much memory a worker used. The cap
    now applies while streaming.
    """
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
    """The one a boundary check cannot cover.

    The old image proxy validated the URL once and then fetched it with follow_redirects=True,
    so a 302 moved the request to somewhere nobody had checked.
    """
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
    """CDNs redirect. Refusing every 302 would be a behaviour regression, not a fix."""
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
    """Two copies of an SSRF filter guarantees one of them goes stale.

    That already happened: `routers/user_preferences.py` carried an older list missing
    0.0.0.0/8, fe80::/10 and any scheme check, guarding an endpoint any authenticated user
    including a tenant could reach, which then fetched with redirects followed. A second copy is
    how the fix above would fail to reach half the codebase.
    """
    app_dir = Path(__file__).resolve().parent.parent / "app"
    owner = app_dir / "core" / "url_safety.py"

    def hardcoded_ranges(tree: ast.AST) -> int:
        """`ip_network("10.0.0.0/8")` and nothing else.

        The signal is a range written into the source, which is what a private-range table is
        made of. `ip_network(raw_cidr)` parsing an operator's TRUSTED_PROXY_CIDRS and
        `ip_network(f"{addr}/{prefix}")` deriving a rate-limit subnet key are both legitimate and
        neither is a second opinion about what counts as internal.
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
