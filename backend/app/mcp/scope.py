"""Where the transport endpoint leaves what the message loop needs.

A separate module because `mount` imports `auth` (for `credentials_from_headers`) and `auth`
needs this name, so holding it in either one would close an import circle.
"""

__all__ = ["SCOPE_API_KEY"]

# The validated API key, put on the ASGI scope by `_McpEndpoint` and read by `ApiKeyMiddleware`.
# The SDK builds its Request from the same scope dictionary, so this survives the hand-off.
SCOPE_API_KEY = "orcastra.mcp.api_key"
