"""Sizes the transport enforces, in a module of their own.

Separate because the transport (`mount`) and a tool description (`tools_guest`) both need them,
and `mount` imports the server that imports the tools, so either holding them would close an
import circle.
"""

import math

from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE

from app.services.lxd import proxy as lxd_proxy

__all__ = ["request_body_limit"]

# Room for everything in a tools/call body that is not the file itself: the JSON-RPC envelope,
# `_meta`, and a 4096-byte path even if every byte of it is JSON-escaped to six.
_ENVELOPE_HEADROOM = 64 * 1024


def request_body_limit() -> int:
    """The largest request body the transport accepts.

    important: the SDK's default is 4 MiB for the whole body, and `lxd_write_file` promises
    `FILE_WRITE_LIMIT` (4 MiB by default) DECODED. Base64 of that is a third larger, so the largest
    documented write was answered 413 before it reached a tool, while the same write over REST
    `/proxy` succeeded. Derived from the limit rather than restated, so retuning
    ORCASTRA_FILE_WRITE_LIMIT moves both together; never below the SDK's own default.
    """
    encoded = 4 * math.ceil(lxd_proxy.FILE_WRITE_LIMIT / 3)
    return max(DEFAULT_MAX_REQUEST_BODY_SIZE, encoded + _ENVELOPE_HEADROOM)
