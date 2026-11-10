"""The dashboard proxy does not let an unauthenticated caller pick the IP the backend trusts.

When forwarding is on, `buildForwardHeaders` in the Next proxy reads `x-forwarded-for` and four
siblings off the inbound request and re-emits a valid non-private value as `x-orcastra-client-ip`,
which the backend trusts because the frontend container sits inside the default trusted-proxy CIDRs.
Behind a real reverse proxy that is right, but the dashboard port is also reachable directly, where
a caller could choose the address it is rate-limited and audited by and escape the per-IP bucket.
So forwarding is off by default in the code and in the compose files, which would override it.

These are structural because the behaviour lives in a TypeScript proxy this suite cannot execute.
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
    """An explicit value in the compose file overrides the code default, so both must agree."""
    source = compose.read_text()
    assert "FORWARD_CLIENT_IP_HEADERS:-true}" not in source, (
        f"{compose.name} defaults the forwarding on, overriding the code default"
    )
    assert "FORWARD_CLIENT_IP_HEADERS:-false}" in source


def test_the_proxy_still_strips_the_headers_it_does_not_trust() -> None:
    """Turning forwarding off must not leave the raw inbound headers reaching the backend.

    The backend reads several of these directly, so passing them through would reopen the same
    hole one layer down.
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
    """The comment above the default says what turning it on trusts, since inverting it looks harmless."""
    source = PROXY.read_text()
    window = source.split("const FORWARD_CLIENT_IP_HEADERS")[0][-1200:]
    assert "trusted" in window.lower() and "audit" in window.lower(), (
        "the comment above the default no longer explains what turning it on trusts"
    )
