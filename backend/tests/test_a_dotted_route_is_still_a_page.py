"""A dotted page route is a page, not a static asset.

Two layers decide whether a path is served without a session. The middleware matcher in
middleware.ts decides whether auth() runs at all, and the authorized callback in auth.ts waves
through what it recognises as an asset. Both used to be fooled by a dot: the callback by
includes("."), the matcher by unanchored ".*\\.json"-style exclusions that skipped auth() for any
path containing ".json" anywhere. A page such as /inventory/networks/br0.json therefore rendered
with no sign-in redirect.

The decision is modelled here from the two source files, with the matcher wrapped the way
Next 15.5 getMiddlewareMatchers compiles it (optional /_next/data prefix, optional transport
suffix), so a path counts as public exactly when the deployed middleware would let it through.
"""

import json
import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
AUTH_TS = FRONTEND / "src" / "auth.ts"
MIDDLEWARE_TS = FRONTEND / "src" / "middleware.ts"
PUBLIC_DIR = FRONTEND / "public"

# The page routes whose last segment is a name a user chose, so it can carry a dot.
DYNAMIC_PAGE_PREFIXES = ("/inventory/networks/", "/terminal/", "/console/")
DOTTED_NAMES = (
    "br0.100",
    "br0.json",
    "a.png.b",
    "x.svg",
    "web.ico",
    "app.png",
    "logo.jpg",
    "img.webp",
    "n.js",
    "n.css",
    "robots.txt",
)


def _source() -> str:
    return AUTH_TS.read_text()


def _asset_pattern() -> "re.Pattern[str]":
    match = re.search(r"const PUBLIC_ASSET_EXTENSION =\s*/(.+?)/i;", _source())
    assert match, "PUBLIC_ASSET_EXTENSION is gone from auth.ts"
    return re.compile(match.group(1), re.IGNORECASE)


def _matcher() -> "re.Pattern[str]":
    source = MIDDLEWARE_TS.read_text()
    match = re.search(r'matcher:\s*\[\s*(?:/\*.*?\*/\s*)?"(.+?)",?\s*\]', source, re.S)
    assert match, "the middleware matcher is not a single string literal any more"
    literal = json.loads(f'"{match.group(1)}"')
    assert literal.startswith("/(") and literal.endswith(")"), literal
    inner = literal[2:-1]
    return re.compile(
        r"^(?:/(_next/data/[^/]{1,}))?(?:/(" + inner + r"))"
        r"(\.json|\.rsc|\.segments/.+\.segment\.rsc)?[/#?]?$"
    )


def _runs_auth(path: str) -> bool:
    return _matcher().match(path) is not None


def _callback_waves_through(path: str) -> bool:
    return (
        path.startswith("/_next")
        or path.startswith("/favicon")
        or path.startswith("/api")
        or _asset_pattern().search(path) is not None
    )


def _is_public(path: str) -> bool:
    return not _runs_auth(path) or _callback_waves_through(path)


@pytest.mark.parametrize("prefix", DYNAMIC_PAGE_PREFIXES)
@pytest.mark.parametrize("name", DOTTED_NAMES)
def test_a_dotted_page_is_gated(prefix: str, name: str) -> None:
    path = prefix + name
    assert _runs_auth(path), f"{path} skips auth() entirely at the middleware matcher"
    assert not _callback_waves_through(path), f"{path} is waved through as an asset"


@pytest.mark.parametrize("path", ["/settings", "/", "/inventory/instances", "/healthcare"])
def test_an_ordinary_page_is_gated(path: str) -> None:
    assert not _is_public(path)


def test_every_public_file_is_served_without_a_session() -> None:
    files = sorted(p for p in PUBLIC_DIR.rglob("*") if p.is_file())
    assert files, "frontend/public is empty, the check below would pass vacuously"
    gated = ["/" + p.relative_to(PUBLIC_DIR).as_posix() for p in files]
    gated = [path for path in gated if not _is_public(path)]
    assert not gated, f"public files that would redirect to sign-in: {gated}"


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/healthz",
        "/health/",
        "/favicon.ico",
        "/_next/static/chunks/a.js",
        "/_next/static/media/geist.woff2",
        "/_next/image",
        "/api/v1/nodes",
        "/api/auth/session",
        "/robots.txt",
        "/logo.PNG",
    ],
)
def test_a_static_or_probe_path_is_public(path: str) -> None:
    assert _is_public(path)


def test_the_dot_test_is_gone() -> None:
    code = "\n".join(line.split("//", 1)[0] for line in _source().splitlines())
    assert 'includes(".")' not in code and "includes('.')" not in code
    assert "PUBLIC_ASSET_EXTENSION.test(nextUrl.pathname)" in code
