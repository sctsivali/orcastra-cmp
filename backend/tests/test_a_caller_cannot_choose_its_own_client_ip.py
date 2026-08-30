"""The dashboard proxy let an unauthenticated caller pick the IP the backend trusts.

`buildForwardHeaders` in the Next proxy reads `x-forwarded-for` and four siblings off the INBOUND
request, checks only that the value is a syntactically valid non-private address, and re-emits it
as `x-orcastra-client-ip`. The backend treats that header as highest-priority truth because the
frontend container sits inside the default trusted-proxy CIDRs.

For a request arriving from a real reverse proxy that is right. The dashboard port is also
reachable directly, and there the value is whatever the caller typed. So a caller could choose the
address it was rate-limited on and audited by, and rotate it to make the per-IP bucket bound
nothing. That last part matters beyond this endpoint: it is the property the API-key rate-limit
work leaned on, which stated that unauthenticated traffic stays on the per-IP bucket.

The fix is the default, in both places that set it. The code default alone would have been
theatre, because the compose files passed `true` explicitly and would have overridden it.

These are structural because the behaviour lives in a TypeScript proxy this suite cannot execute.
What they pin is that the default cannot drift back without someone editing an asserted line.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROXY = ROOT / "frontend" / "src" / "app" / "api" / "v1" / "[[...path]]" / "route.ts"
COMPOSE = [ROOT / "docker-compose.yml", ROOT / "docker-compose.prod.yml"]


def test_the_proxy_only_forwards_a_client_ip_when_asked_to() -> None:
    source = PROXY.read_text()
    assert 'process.env.FORWARD_CLIENT_IP_HEADERS === "true"' in source, (
        "the proxy forwards a caller-supplied client IP unless someone opts out, which is "
        "backwards for a port that is reachable directly"
    )
    assert 'FORWARD_CLIENT_IP_HEADERS !== "false"' not in source


@pytest.mark.parametrize("compose", COMPOSE, ids=lambda p: p.name)
def test_compose_does_not_switch_it_back_on(compose: Path) -> None:
    """Where the first version of this fix would have failed silently.

    The code default is irrelevant while the compose file passes an explicit value, so both have
    to agree or the change is decoration.
    """
    source = compose.read_text()
    assert "FORWARD_CLIENT_IP_HEADERS:-true}" not in source, (
        f"{compose.name} defaults the forwarding on, overriding the code default"
    )
    assert "FORWARD_CLIENT_IP_HEADERS:-false}" in source


def test_the_proxy_still_strips_the_headers_it_does_not_trust() -> None:
    """Turning forwarding off must not leave the raw inbound headers reaching the backend.

    The backend reads several of these directly, so passing them through would move the same
    hole one layer down instead of closing it.
    """
    source = PROXY.read_text()
    for header in (
        "cf-connecting-ip", "true-client-ip", "x-real-ip", "x-forwarded-for",
        "forwarded", "x-orcastra-client-ip", "x-orcastra-client-ip-source",
    ):
        assert f'headers.delete("{header}")' in source, (
            f"{header} is no longer stripped from the inbound request"
        )


def test_the_reason_is_recorded_where_the_default_is() -> None:
    """This default looks arbitrary and inverting it looks harmless; both are why it drifted."""
    source = PROXY.read_text()
    window = source.split("const FORWARD_CLIENT_IP_HEADERS")[0][-1200:]
    assert "trusted" in window.lower() and "audit" in window.lower(), (
        "the comment above the default no longer explains what turning it on trusts"
    )
