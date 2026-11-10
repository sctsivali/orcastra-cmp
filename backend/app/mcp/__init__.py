"""The MCP (Model Context Protocol) server mounted inside this FastAPI app.

No import-time side effects: nothing here builds a server or memoises a singleton at import,
because a module-level instance would be per-uvicorn-worker state. The server is built once per
worker by `create_app`, and its session manager is driven by the app's own lifespan.
"""

from .mount import build_mcp_route

__all__ = ["build_mcp_route"]
