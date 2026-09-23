"""A dotted page route is a page, not a static asset.

The frontend auth middleware waved through any path containing a dot as a public asset, so a page
such as /inventory/networks/br0.100 skipped the sign-in redirect and the role gate. It now matches an
explicit list of static asset extensions at the end of the path.
"""

import re
from pathlib import Path

import pytest

AUTH_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "auth.ts"


def _source() -> str:
    return AUTH_TS.read_text()


def _asset_pattern() -> "re.Pattern[str]":
    match = re.search(r"const PUBLIC_ASSET_EXTENSION =\s*/(.+?)/i;", _source())
    assert match, "PUBLIC_ASSET_EXTENSION is gone from auth.ts"
    return re.compile(match.group(1), re.IGNORECASE)


@pytest.mark.parametrize(
    "path",
    [
        "/inventory/networks/br0.100",
        "/console/web.01",
        "/inventory/instances/app.v2",
        "/terminal/node.js-build",
        "/settings",
    ],
)
def test_a_dotted_page_is_not_public(path: str) -> None:
    assert not _asset_pattern().search(path), f"{path} would skip the sign-in redirect"


@pytest.mark.parametrize(
    "path",
    [
        "/spice-html5/main.js",
        "/spice-html5/spice.css",
        "/_next/static/chunks/a.js",
        "/logo.PNG",
        "/fonts/geist.woff2",
        "/robots.txt",
    ],
)
def test_a_static_asset_is_public(path: str) -> None:
    assert _asset_pattern().search(path)


def test_the_dot_test_is_gone() -> None:
    code = "\n".join(line.split("//", 1)[0] for line in _source().splitlines())
    assert 'includes(".")' not in code and "includes('.')" not in code
    assert "PUBLIC_ASSET_EXTENSION.test(nextUrl.pathname)" in code
