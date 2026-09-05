"""A second OpenAPI document describing only what an add-on may call.

Production serves no OpenAPI at all: `docs_url`, `redoc_url` and `openapi_url` are None unless
DEBUG. That is the right default for the internal surface, which is large, JWT-authenticated and
would hand a reader a complete map of the control plane. It is also the reason nobody could build
against this API, because there was nothing machine-readable to build from.

So this module emits a curated document instead, and leaves the internal one switched off.

Selection is by DEPENDANT TREE, not by a tag or a marker. A public route is one whose dependency
graph contains `validate_api_key`. Three reasons that beats the alternatives: it is already true
of every external route without editing anything, a new external endpoint acquires it
automatically rather than being remembered, and it cannot drift, because a route that stops
depending on API-key authentication has stopped being public by definition.

`operation_id` is derived from the method and path, never from the Python function name, so
renaming a handler is not a breaking change for a generated client.
"""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

logger = logging.getLogger(__name__)

# The dependency whose presence means "authenticated by an integration API key".
API_KEY_DEPENDENCY = "validate_api_key"

# Handlers that belong in the public document but do not depend on validate_api_key, each with
# the reason. Mirrors the EXEMPT convention the capability ratchet already uses.
EXTRA_PUBLIC_HANDLERS: Dict[str, str] = {
    "validate_api_key_endpoint": (
        "authenticates from the request body rather than the headers, so its dependant contains "
        "no validate_api_key, and it is the first call an integrator makes"
    ),
}

# A route carrying any of these is part of the dashboard's own surface. Their presence in the
# public document would be the leak this whole module exists to avoid.
NEVER_PUBLIC_DEPENDENCIES: Set[str] = {
    "get_current_user",
    "require_roles",
    "org_scope_dependency",
    "get_ws_user",
}

# OpenAPI models OPTIONS badly and generators emit noise for it. HEAD is kept, because tus
# resumption genuinely depends on it.
EXCLUDED_METHODS = {"OPTIONS"}

SECURITY_SCHEMES = {
    "OrcastraKeyId": {"type": "apiKey", "in": "header", "name": "X-API-Key-ID"},
    "OrcastraKeySecret": {"type": "apiKey", "in": "header", "name": "X-API-Key-Secret"},
}

# important: ONE requirement object containing BOTH schemes, which OpenAPI reads as AND. Two
# separate list items would read as OR and would document that either header alone authenticates,
# which is false and is the easiest thing in this file to get wrong.
SECURITY_REQUIREMENT = [{"OrcastraKeyId": [], "OrcastraKeySecret": []}]

DESCRIPTION = """\
The surface an Orcastra add-on calls.

An add-on runs on your own infrastructure. It authenticates with the credential issued when an
operator installed it, and receives signed webhooks for the events it subscribed to. No add-on
code runs inside Orcastra or inside the dashboard's browser origin.

Every endpoint here requires both credential headers together. What a credential may do is
decided by the capabilities and the per-cluster grant the operator consented to, so two
installations of the same add-on can reach different things.
"""


def _dependency_names(route: APIRoute) -> Set[str]:
    """Every dependency in a route's tree, by the name it was registered under.

    The capability factory renames its closure to `require_<capability>`, which is what makes a
    required capability legible here rather than showing up as a generic closure.
    """
    names: Set[str] = set()
    stack = list(getattr(route.dependant, "dependencies", []))
    while stack:
        dependency = stack.pop()
        call = getattr(dependency, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", ""))
        stack.extend(getattr(dependency, "dependencies", []))
    return names


def operation_id_for(method: str, path: str, api_prefix: str) -> str:
    """Stable across a handler rename, because it is a function of the route, not the code."""
    trimmed = path[len(api_prefix):] if path.startswith(api_prefix) else path
    parts: List[str] = []
    for segment in trimmed.strip("/").split("/"):
        if not segment:
            continue
        if segment.startswith("{") and segment.endswith("}"):
            parts.append(f"by_{segment[1:-1]}")
        else:
            parts.append(segment.replace("-", "_").replace(".", "_"))
    return "_".join([method.lower()] + parts)


def _walk_routes(container: Any, _depth: int = 0) -> List[APIRoute]:
    """Every APIRoute reachable from an app or a router, however it is nested.

    Used by tests and diagnostics rather than by selection. `app.routes` is not a flat list of
    APIRoute across the FastAPI versions this project supports: up to 0.128 `include_router`
    copied routes onto the parent, and from 0.141 it stores a nested router instead, so a flat
    loop finds two routes on an application serving nearly three hundred.
    """
    found: List[APIRoute] = []
    if _depth > 8:  # a cycle would otherwise hang rather than fail
        return found
    for route in getattr(container, "routes", []):
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "routes"):
            found.extend(_walk_routes(route, _depth + 1))
        elif hasattr(route, "original_router"):
            # FastAPI 0.141's nested include. Descending here would give paths without the
            # parent's prefix and dependants without the parent's dependencies, so it is
            # deliberately only followed for diagnostics, never for selection.
            found.extend(_walk_routes(route.original_router, _depth + 1))
    return found


def _api_key_routers() -> List[Tuple[str, Any]]:
    """The routers whose handlers authenticate with an integration API key.

    important: selection is by router object, not by walking the application's route tree.
    Walking looked cleaner and was wrong in a way that only the deployed image showed. From
    FastAPI 0.141 an included router is stored nested, and the route objects inside it have not
    had the parent's prefix applied or the parent's dependencies merged. Descending into them
    therefore produces paths that are missing their prefix and dependants that are missing the
    router-level `require_partner()` and `org_scope_dependency`, which would put the dashboard's
    own JWT surface into a document served to anybody who asks.

    A router object's own `routes` list is a plain list of APIRoute on every version in
    requirements.txt, and each route's path already carries that router's prefix. So this reads
    the same on 0.109 and on 0.141.

    This is the same list the capability ratchet already maintains by filename, not a second one.
    """
    from app.routers import extensions_external, image_uploads, integrations, iso_uploads

    routers: List[Tuple[str, Any]] = [
        ("integrations.py", integrations.external_router),
        ("image_uploads.py", image_uploads.router),
        ("iso_uploads.py", iso_uploads.router),
    ]
    # Only advertise an endpoint this deployment actually serves.
    from app.core.config import get_settings

    if get_settings().extensions_enabled:
        routers.append(("extensions_external.py", extensions_external.external_router))
    return routers


def public_routes(app: Optional[FastAPI] = None) -> List[Tuple[str, APIRoute]]:
    """(method, route) for everything an add-on may call.

    `app` is accepted and unused, so callers read naturally and a future change that does need it
    does not have to move every call site.
    """
    selected: List[Tuple[str, APIRoute]] = []
    for _filename, router in _api_key_routers():
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            methods = route.methods - EXCLUDED_METHODS
            if not methods:
                # OPTIONS only. tus CORS discovery, which OpenAPI models badly and which
                # generators emit noise for. Skipped before the dependency check so a
                # deliberate exclusion does not log as a surprise.
                continue

            names = _dependency_names(route)
            handler = getattr(route.endpoint, "__name__", "")
            if API_KEY_DEPENDENCY not in names and handler not in EXTRA_PUBLIC_HANDLERS:
                # On an API-key router this means a handler that authenticates some other way.
                # The capability ratchet already refuses one that authenticates no way at all.
                logger.warning(
                    "Route %s is on an API-key router but does not depend on %s; excluded",
                    route.path, API_KEY_DEPENDENCY,
                )
                continue
            if names & NEVER_PUBLIC_DEPENDENCIES:
                # The safety assertion the selection no longer provides on its own. A route
                # reachable both ways must never reach a public document.
                logger.error(
                    "Route %s carries a session dependency and was excluded from the public "
                    "document", route.path,
                )
                continue
            for method in sorted(methods):
                selected.append((method, route))
    return selected


def required_capabilities(route: APIRoute) -> List[str]:
    """The capabilities a route's dependants declare, read from the renamed closures."""
    found = []
    for name in _dependency_names(route):
        if name.startswith("require_") and name not in ("require_partner", "require_tenant"):
            found.append(name.removeprefix("require_").replace("_", "."))
    return sorted(set(found))


def build_public_openapi(app: FastAPI, *, api_prefix: str, base_url: str, version: str) -> Dict[str, Any]:
    """The curated document. A pure function of the route table, so it is safe to memoise."""
    selected = public_routes(app)
    routes = list({id(route): route for _method, route in selected}.values())

    document = get_openapi(
        title="Orcastra Add-on API",
        version=version,
        description=DESCRIPTION,
        routes=routes,
    )

    document["servers"] = [{"url": f"{base_url.rstrip('/')}{api_prefix}"}] if base_url else []
    document.setdefault("components", {})["securitySchemes"] = SECURITY_SCHEMES

    for path, operations in document.get("paths", {}).items():
        for method, operation in operations.items():
            if method.upper() in EXCLUDED_METHODS:
                continue
            operation["operationId"] = operation_id_for(method, path, api_prefix)

            route = next(
                (r for m, r in selected if r.path == path and m.lower() == method.lower()), None
            )
            handler = getattr(route.endpoint, "__name__", "") if route else ""
            if handler in EXTRA_PUBLIC_HANDLERS:
                # It reads credentials from the body, so documenting headers it never reads
                # would be a lie a generated client would then send.
                operation["security"] = []
                continue

            operation["security"] = SECURITY_REQUIREMENT
            if route is not None:
                caps = required_capabilities(route)
                if caps:
                    operation["x-orcastra-capabilities"] = caps

    document["x-orcastra-mcp"] = {
        "endpoint": f"{api_prefix}/mcp",
        "transport": "JSON-RPC over HTTP, authenticated with the same credential headers",
        "note": (
            "The MCP endpoint is not modelled as OpenAPI paths. It speaks JSON-RPC, so turning "
            "its tools into paths would describe something no client can call."
        ),
    }
    return document
