"""Public OpenAPI document for the API surface add-ons call.

The internal OpenAPI is served only in DEBUG because it maps the whole JWT-authenticated control
plane. This module builds a separate, curated document instead.

Routes come from the API-key routers (`_api_key_routers`) and are kept only when their dependency
tree contains `validate_api_key`, so a route that stops using API-key auth drops out by itself.
`operation_id` is derived from method and path, so renaming a handler does not break generated
clients.
"""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

logger = logging.getLogger(__name__)

# Dependency that marks a route as authenticated by an integration API key.
API_KEY_DEPENDENCY = "validate_api_key"

# Public handlers that do not depend on validate_api_key, each with the reason.
EXTRA_PUBLIC_HANDLERS: Dict[str, str] = {
    "validate_api_key_endpoint": (
        "authenticates from the request body rather than the headers, so its dependant contains "
        "no validate_api_key, and it is the first call an integrator makes"
    ),
}

# A route with any of these dependencies belongs to the dashboard itself and must never be public.
NEVER_PUBLIC_DEPENDENCIES: Set[str] = {
    "get_current_user",
    "require_roles",
    "org_scope_dependency",
    "get_ws_user",
}

# OPTIONS only adds noise to generated clients. HEAD stays: tus resumption needs it.
EXCLUDED_METHODS = {"OPTIONS"}

SECURITY_SCHEMES = {
    "OrcastraKeyId": {"type": "apiKey", "in": "header", "name": "X-API-Key-ID"},
    "OrcastraKeySecret": {"type": "apiKey", "in": "header", "name": "X-API-Key-Secret"},
}

# important: one requirement object holding both schemes, which OpenAPI reads as AND. Two list
# items would mean OR, documenting that either header alone authenticates.
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
    """Names of every dependency in a route's tree.

    The capability factory names its closures `require_<capability>`, which is how capabilities are
    read back here.
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
    """Operation id built from method and path, so it survives a handler rename."""
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


def _walk_route_paths(
    container: Any, prefix: str = "", _depth: int = 0
) -> List[Tuple[str, APIRoute]]:
    """(served path, route) for every APIRoute under an app or router, in declaration order.

    For tests and diagnostics only. Up to FastAPI 0.128 `include_router` copies routes onto the
    parent with the prefix applied; from 0.141 it nests the router, and the served path is
    `include_context.prefix` joined with the route's own path.
    """
    found: List[Tuple[str, APIRoute]] = []
    if _depth > 8:  # guards against a cycle
        return found
    for route in getattr(container, "routes", []):
        if isinstance(route, APIRoute):
            found.append((prefix + route.path, route))
        elif hasattr(route, "routes"):
            found.extend(_walk_route_paths(route, prefix, _depth + 1))
        elif hasattr(route, "original_router"):
            # FastAPI 0.141 nested include. Its dependants lack the parent's dependencies, so
            # this path is never used for selection.
            context = getattr(route, "include_context", None)
            nested = prefix + str(getattr(context, "prefix", "") or "")
            found.extend(_walk_route_paths(route.original_router, nested, _depth + 1))
    return found


def _walk_routes(container: Any) -> List[APIRoute]:
    """Every APIRoute under an app or router.

    On FastAPI 0.141 a nested route's `.path` lacks the parent prefix; use `_walk_route_paths` for
    served paths.
    """
    return [route for _path, route in _walk_route_paths(container)]


def _api_key_routers() -> List[Tuple[str, Any]]:
    """Routers whose handlers authenticate with an integration API key.

    important: select by router object, never by walking the app's route tree. From FastAPI 0.141
    nested routes lack the parent's prefix and router-level dependencies (`require_partner()`,
    `org_scope_dependency`), so a walk could publish JWT-only routes. A router's own `routes` list
    is flat with its prefix applied on every supported version. Same list as the capability
    ratchet uses.
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
    """(method, route) for everything an add-on may call. `app` is currently unused."""
    selected: List[Tuple[str, APIRoute]] = []
    for _filename, router in _api_key_routers():
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            methods = route.methods - EXCLUDED_METHODS
            if not methods:
                # OPTIONS-only route (tus CORS discovery). Skipped before the dependency check
                # so it does not log a warning.
                continue

            names = _dependency_names(route)
            handler = getattr(route.endpoint, "__name__", "")
            if API_KEY_DEPENDENCY not in names and handler not in EXTRA_PUBLIC_HANDLERS:
                # Authenticates some other way. Routes with no auth at all are refused by the
                # capability ratchet.
                logger.warning(
                    "Route %s is on an API-key router but does not depend on %s; excluded",
                    route.path, API_KEY_DEPENDENCY,
                )
                continue
            if names & NEVER_PUBLIC_DEPENDENCIES:
                # A route reachable with a session must never appear in the public document.
                logger.error(
                    "Route %s carries a session dependency and was excluded from the public "
                    "document", route.path,
                )
                continue
            for method in sorted(methods):
                selected.append((method, route))
    return selected


def required_capabilities(route: APIRoute) -> List[str]:
    """Capabilities a route requires, read from its `require_<capability>` dependencies."""
    found = []
    for name in _dependency_names(route):
        if name.startswith("require_") and name not in ("require_partner", "require_tenant"):
            found.append(name.removeprefix("require_").replace("_", "."))
    return sorted(set(found))


def build_public_openapi(app: FastAPI, *, api_prefix: str, base_url: str, version: str) -> Dict[str, Any]:
    """Build the public document. Depends only on the route table, so callers may cache it."""
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
                # Reads credentials from the body, so the headers are not documented.
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
