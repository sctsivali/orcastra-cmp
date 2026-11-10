"""The add-on API document describes every endpoint an add-on may call, and nothing else.

Production serves no internal OpenAPI: `docs_url`, `redoc_url` and `openapi_url` are None unless
DEBUG. The curated public document is served without DEBUG, so both are pinned here: the public
document exists, and the internal one stays off.

Routes are taken from the API-key routers (`_api_key_routers`) and kept only when their dependency
tree contains `validate_api_key`, so a new route on those routers is picked up by itself. An
external endpoint missing from the document is one an integrator cannot discover; an internal one
appearing in it publishes a map of the control plane.

Operation ids are pinned as a literal because a generated client names its methods after them, so
a changed id renames an integrator's code. The list doubles as the inventory of what is public.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_VERSION", "0.0.0-test")
os.environ.setdefault("CORS_ORIGINS", "https://dashboard.example.com")
os.environ.setdefault("PUBLIC_API_BASE_URL", "https://api.example.com")

import pytest  # noqa: E402

from app.core.public_openapi import (  # noqa: E402
    EXTRA_PUBLIC_HANDLERS,
    NEVER_PUBLIC_DEPENDENCIES,
    SECURITY_SCHEMES,
    _dependency_names,
    _walk_route_paths,
    _walk_routes,
    build_public_openapi,
    operation_id_for,
    public_routes,
)
from app.main import create_app  # noqa: E402

# Every operation an add-on may call. A new external endpoint fails here until it is added, so
# publishing one is always a reviewed change.
PINNED_OPERATION_IDS = {
    # The owner-key contract (organization_owner_keys.v1), answering 404 unless the feature flag is on
    "delete_integrations_external_organizations_by_external_id_owner_keys_self",
    "get_integrations_external_organizations_by_external_id_owner_keys",
    "post_integrations_external_organizations_owner_keys",
    "get_extensions_self",
    "get_integrations_clusters",
    # The failover read (cluster_failover_config.v1): numbers only, never the server configuration
    "get_integrations_clusters_failover",
    "get_integrations_external_policies",
    "get_integrations_image_uploads_by_session_id",
    "get_integrations_instances",
    "get_integrations_instances_by_name",
    "get_integrations_iso_uploads_by_session_id",
    "get_integrations_operations_by_operation_id",
    "get_integrations_projects",
    "get_integrations_regions",
    "get_integrations_whoami",
    "head_integrations_image_uploads_by_session_id",
    "head_integrations_iso_uploads_by_session_id",
    "patch_integrations_image_uploads_by_session_id",
    "patch_integrations_iso_uploads_by_session_id",
    "post_integrations_external_sync_policies",
    "post_integrations_image_uploads",
    "post_integrations_image_uploads_by_session_id_finalize",
    "post_integrations_iso_uploads",
    "post_integrations_iso_uploads_by_session_id_finalize",
    "post_integrations_provision_tenant_access",
    "post_integrations_proxy",
    "post_integrations_raw_proxy",
    "post_integrations_revoke_tenant_access",
    "post_integrations_sync",
    "post_integrations_validate",
}


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture(scope="module")
def document(app):
    return build_public_openapi(
        app, api_prefix="/api/v1", base_url="https://api.example.com", version="1.0.0"
    )


def _operations(document):
    return {
        (path, method): operation
        for path, methods in document["paths"].items()
        for method, operation in methods.items()
    }


# ── The document exists, and the internal one does not ─────────────────────────────────────────

def test_the_internal_document_stays_off_in_production(monkeypatch) -> None:
    """Serving the curated document must not turn the internal one on."""
    monkeypatch.setenv("DEBUG", "false")
    from app.core import config

    config.get_settings.cache_clear()
    try:
        production = create_app()
        assert production.openapi_url is None
        assert production.docs_url is None
        assert production.redoc_url is None
        # Served paths: on FastAPI 0.141 a route below a nested include reports `.path`
        # without the `/api/v1` prefix, so the set would lack the entry asserted below.
        paths = {served for served, _route in _walk_route_paths(production)}
        assert "/api/v1/openapi.public.json" in paths
    finally:
        config.get_settings.cache_clear()


# ── What is in it ──────────────────────────────────────────────────────────────────────────────

def test_the_document_is_not_empty(app, document) -> None:
    """A set comparison alone would report an empty document as a rename.

    From FastAPI 0.141 `app.routes` is not a flat list of APIRoute: include_router stores a
    nested router instead of copying routes onto the parent, so a loop over app.routes finds
    almost nothing and the document is served, valid, and empty. requirements.txt allows 0.109
    through 0.141, so a developer's virtualenv may not hold the version the image runs.
    """
    assert len(_walk_routes(app)) > 50, (
        "route traversal found almost nothing, so it is walking the wrong structure for this "
        "FastAPI version rather than finding no public routes"
    )
    assert public_routes(app), "no route was selected as public"
    assert document["paths"], "the document is served and empty"


def test_every_public_operation_id_is_pinned(document) -> None:
    found = {operation["operationId"] for operation in _operations(document).values()}
    added = sorted(found - PINNED_OPERATION_IDS)
    removed = sorted(PINNED_OPERATION_IDS - found)
    assert found == PINNED_OPERATION_IDS, (
        f"the public API changed shape. added={added} removed={removed}. "
        "An added endpoint needs documenting; a removed or renamed one breaks generated clients."
    )


def test_an_operation_id_is_derived_from_the_route_not_the_function() -> None:
    """Renaming a Python handler must not break a generated client."""
    assert operation_id_for("GET", "/api/v1/integrations/instances/{name}", "/api/v1") == (
        "get_integrations_instances_by_name"
    )
    assert operation_id_for("POST", "/api/v1/integrations/raw-proxy", "/api/v1") == (
        "post_integrations_raw_proxy"
    )


def test_no_session_authenticated_route_reaches_the_public_document(app) -> None:
    """A session-authenticated route in the document would publish the dashboard's own surface."""
    leaked = [
        route.path
        for _method, route in public_routes(app)
        if _dependency_names(route) & NEVER_PUBLIC_DEPENDENCIES
    ]
    assert not leaked, f"the dashboard's own surface reached the public document: {leaked}"


def test_the_two_headers_are_required_together_not_either_one(document) -> None:
    """Both API-key headers are documented as required together.

    Two entries in the security list means OR, and would document that either header alone
    authenticates. One object holding both keys means AND, which is what the server does.
    """
    for (path, method), operation in _operations(document).items():
        where = f"{method.upper()} {path}"
        security = operation.get("security")
        if operation["operationId"] == "post_integrations_validate":
            assert security == [], f"{where} documents headers it does not read"
            continue
        assert security is not None, f"{where} documents no authentication at all"
        assert len(security) == 1, f"{where} reads as either header, not both"
        assert set(security[0]) == set(SECURITY_SCHEMES), f"{where} names the wrong schemes"


def test_the_credential_oracle_is_exempt_with_a_written_reason() -> None:
    """Each exemption from the API-key rule carries a written reason."""
    assert "validate_api_key_endpoint" in EXTRA_PUBLIC_HANDLERS
    assert len(EXTRA_PUBLIC_HANDLERS["validate_api_key_endpoint"]) > 40


def test_the_servers_entry_is_absolute(document) -> None:
    """A relative servers entry makes every generated client unusable."""
    assert document["servers"], "no servers entry, so a generated client has nowhere to send"
    url = document["servers"][0]["url"]
    assert url.startswith("https://"), f"servers entry is not absolute: {url}"
    assert url.endswith("/api/v1")


def test_a_capability_gated_operation_says_which_capability(document) -> None:
    """Read from the dependency tree, not from prose, so it cannot describe a gate that moved."""
    operations = _operations(document)
    proxy = next(
        o for o in operations.values() if o["operationId"] == "post_integrations_raw_proxy"
    )
    assert "lxd.proxy" in proxy.get("x-orcastra-capabilities", [])

    uploads = next(
        o for o in operations.values() if o["operationId"] == "post_integrations_image_uploads"
    )
    assert "storage.upload" in uploads.get("x-orcastra-capabilities", [])


def test_the_mcp_endpoint_is_described_rather_than_invented(document) -> None:
    """JSON-RPC tools as OpenAPI paths would describe calls no client can make.

    The document points at the MCP endpoint instead and names the transport.
    """
    mcp = document.get("x-orcastra-mcp")
    assert mcp and mcp["endpoint"].endswith("/mcp")
    assert "JSON-RPC" in mcp["transport"]


# ── The flags next to each other ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "flag,path_fragment",
    [
        ("EXTENSIONS_ENABLED", "/api/v1/extensions/self"),
        ("PUBLIC_OPENAPI_ENABLED", "/api/v1/openapi.public.json"),
        ("MCP_ENABLED", "/api/v1/mcp"),
    ],
)
def test_each_flag_gates_only_its_own_surface(monkeypatch, flag, path_fragment) -> None:
    """Three feature flags sit in adjacent blocks in one function.

    An edit that replaces one flag's `if` instead of inserting before it mounts that surface
    whenever a neighbouring flag is on and leaves its own flag dead. A dead flag goes unnoticed
    until someone relies on it to close a surface off.
    """
    from app.core import config

    monkeypatch.setenv(flag, "false")
    config.get_settings.cache_clear()
    try:
        built = create_app()
        paths = {served for served, _route in _walk_route_paths(built)} | {
            getattr(r, "path", "") for r in built.routes
        }
        assert path_fragment not in paths, f"{flag}=false did not remove {path_fragment}"

        # The other two surfaces stay mounted.
        others = {
            "EXTENSIONS_ENABLED": "/api/v1/extensions/self",
            "PUBLIC_OPENAPI_ENABLED": "/api/v1/openapi.public.json",
            "MCP_ENABLED": "/api/v1/mcp",
        }
        for other_flag, other_path in others.items():
            if other_flag == flag:
                continue
            assert other_path in paths, (
                f"turning off {flag} also removed {other_path}, which belongs to {other_flag}"
            )
    finally:
        config.get_settings.cache_clear()
