"""Serving the add-on API document, without turning the internal one back on.

`main.py` keeps `openapi_url` at None in production and this router does not change that. What it
adds is a second, curated document describing only the API-key surface, plus one renderer for it.

Unauthenticated on purpose: an integrator needs the specification before they hold a credential,
and the document names no internal route. Gated by a setting so an operator who disagrees can
switch it off.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html

from app.core.config import get_settings
from app.core.public_openapi import build_public_openapi

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Documentation"])

# A pure function of the route table, which is fixed once the app is built, so it is built once.
_document: Optional[Dict[str, Any]] = None


def _spec(request: Request) -> Dict[str, Any]:
    global _document
    if _document is None:
        settings = get_settings()
        _document = build_public_openapi(
            request.app,
            api_prefix=settings.api_prefix,
            base_url=settings.public_api_base_url,
            version=settings.app_version,
        )
    return _document


@router.get(
    "/openapi.public.json",
    include_in_schema=False,
    summary="The add-on API, as OpenAPI",
)
async def public_openapi(request: Request) -> Response:
    settings = get_settings()
    if not settings.public_openapi_enabled:
        raise HTTPException(status_code=404, detail="Not Found")

    import json

    return Response(
        content=json.dumps(_spec(request)),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/docs/public", include_in_schema=False, summary="Browse the add-on API")
async def public_docs(request: Request) -> Response:
    settings = get_settings()
    if not (settings.public_openapi_enabled and settings.public_docs_ui_enabled):
        raise HTTPException(status_code=404, detail="Not Found")

    # note: "Try it out" here can do nothing a credential could not already do. Every capability
    # gate, the per-cluster grant, the path rules and the per-key rate limit apply exactly as
    # they do to any other caller, because this is the same API.
    return get_swagger_ui_html(
        openapi_url=f"{settings.api_prefix}/openapi.public.json",
        title="Orcastra Add-on API",
    )
