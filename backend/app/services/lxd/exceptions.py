"""
Custom exceptions for LXD operations.
"""


class LXDConnectionError(Exception):
    """Exception raised when LXD connection fails."""
    pass


class LXDOperationError(Exception):
    """Exception raised when an LXD operation fails."""
    pass


class LXDTimeoutError(LXDOperationError):
    """Raised when an LXD operation does not finish within the read timeout.

    Subclasses LXDOperationError so existing ``except LXDOperationError`` handlers
    still catch it; routers may catch it first to return a 504 (Gateway Timeout).
    """
    pass
