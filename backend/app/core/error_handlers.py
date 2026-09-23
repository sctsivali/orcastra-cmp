"""Application exception handlers that decide what a failure may tell the client.

A 4xx is the caller's own request described back to them, so it goes through FastAPI's handler
unchanged, byte for byte, dict details included. A server fault is rewritten:
  * 502/503/504 are answers this application wrote on purpose ("cluster X recently unreachable:
    certificate not trusted"). They keep their sentence, minus URLs, paths and addresses.
  * any other 5xx is an unexpected fault whose text is a driver or database exception. The client
    gets a stable reference and the full text goes to the server log under that reference.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.error_sanitizer import client_detail, reference_for, sanitize_error
from app.services.lxd.exceptions import LXDConnectionError, LXDLocalStorageError

logger = logging.getLogger(__name__)

AVAILABILITY_STATUSES = frozenset({502, 503, 504})


def _method(request: Request) -> str:
    return getattr(request, "method", "WEBSOCKET")


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def sanitized_http_exception_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code < 500:
            return await http_exception_handler(request, exc)
        reference, full = reference_for(exc)
        # note: a 502/503/504 is an answer this application chose (a cluster down, a pool shed, an
        # identity provider still starting), so it is a warning. It is still logged, because the
        # reference in the response must lead somewhere.
        log = logger.warning if exc.status_code in AVAILABILITY_STATUSES else logger.error
        log("Server fault %s on %s %s: %s", reference, _method(request), request.url.path, full)
        if exc.status_code in AVAILABILITY_STATUSES:
            detail = sanitize_error(exc.detail)
        else:
            detail = client_detail(reference)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail, "reference": reference},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(LXDLocalStorageError)
    async def lxd_local_storage_error_handler(request: Request, exc: LXDLocalStorageError):
        """The backend's own disk could not hold the TLS material, not the cluster being down.

        Separate from the connection handler on purpose: conflating the two told operators every
        hypervisor was offline while they were all healthy.
        """
        reference, full = reference_for(exc)
        logger.error("LXD local storage error %s: %s", reference, full)
        return JSONResponse(
            status_code=503,
            content={
                "error": "Backend Storage Unavailable",
                "detail": sanitize_error(str(exc)),
                "code": "BACKEND_TEMP_STORAGE_FULL",
                "reference": reference,
            },
        )

    @app.exception_handler(LXDConnectionError)
    async def lxd_connection_error_handler(request: Request, exc: LXDConnectionError):
        reference, full = reference_for(exc)
        logger.error("LXD connection error %s: %s", reference, full)
        return JSONResponse(
            status_code=503,
            content={
                "error": "LXD Connection Error",
                "detail": sanitize_error(str(exc)),
                "code": "LXD_CONNECTION_ERROR",
                "reference": reference,
            },
        )
