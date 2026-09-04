"""The MCP (Model Context Protocol) server mounted inside this FastAPI app.

note: no import-time side effects. Nothing here builds a server or memoises a singleton at import,
because a module-level instance would be per-uvicorn-worker state, which is a documented recurring
defect class in this codebase. The server is built once per worker by `create_app`, and its
session manager is driven by the app's own lifespan.
"""

from .mount import build_mcp_route

__all__ = ["build_mcp_route"]
