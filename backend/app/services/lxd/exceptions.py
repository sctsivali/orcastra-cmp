"""
Custom exceptions for LXD operations.
"""


class LXDConnectionError(Exception):
    """Exception raised when LXD connection fails."""
    pass


class LXDOperationError(Exception):
    """Exception raised when an LXD operation fails."""
    pass
