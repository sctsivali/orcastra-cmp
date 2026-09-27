"""Public assets remain reachable while dotted dynamic pages keep their auth gate."""

import json
import re
from pathlib import Path

import pytest


FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
AUTH = FRONTEND / "src/auth.ts"
MIDDLEWARE = FRONTEND / "src/middleware.ts"


def _asset_pattern():
    source = AUTH.read_text()
    match = re.search(r"const PUBLIC_ASSET_EXTENSION =\s*/(.+?)/i;", source)
    assert match, "Public asset pattern is missing"
    return re.compile(match.group(1), re.IGNORECASE)


def _middleware_matcher():
    source = MIDDLEWARE.read_text()
    match = re.search(r'matcher:\s*\[\s*(?:/\*.*?\*/\s*)?"(.+?)",?\s*\]', source, re.S)
    assert match, "Middleware matcher shape changed; review its auth exclusions"
    literal = json.loads(f'"{match.group(1)}"')
    assert literal.startswith("/(") and literal.endswith(")")
    return re.compile("^/" + literal[2:-1] + "$")


def _is_public(path):
    if not _middleware_matcher().fullmatch(path):
        return True
    return (
        path.startswith("/_next/")
        or path == "/api" or path.startswith("/api/")
        or path == "/auth" or path.startswith("/auth/")
        or _asset_pattern().fullmatch(path) is not None
    )


@pytest.mark.parametrize("prefix", ["/inventory/networks/", "/console/", "/terminal/"])
@pytest.mark.parametrize("name", ["br0.100", "web.png", "foo.json", "logo.svg", "app.js"])
def test_dotted_dynamic_pages_still_require_auth(prefix, name):
    assert not _is_public(prefix + name)


@pytest.mark.parametrize("path", ["/healthcare", "/apiary", "/authentic", "/settings"])
def test_public_prefixes_do_not_open_unrelated_pages(path):
    assert not _is_public(path)


def test_every_shipped_public_file_remains_reachable():
    files = list((FRONTEND / "public").rglob("*"))
    paths = ["/" + path.relative_to(FRONTEND / "public").as_posix() for path in files if path.is_file()]
    assert paths
    assert all(_is_public(path) for path in paths)


@pytest.mark.parametrize("path", ["/health", "/healthz", "/favicon.ico", "/_next/static/a.js", "/api/v1/nodes"])
def test_explicit_public_endpoints_remain_public(path):
    assert _is_public(path)
