"""
Custom exceptions for LXD operations.
"""


class LXDConnectionError(Exception):
    """Exception raised when LXD connection fails."""
    pass


class LXDOperationError(Exception):
    """Exception raised when an LXD operation fails."""
    pass


class LXDGracefulTimeoutError(LXDOperationError):
    """A graceful stop/restart timed out waiting for the guest to shut down.

    Distinct from a generic operation failure because it is recoverable in a
    specific way: the caller can retry with force. It most often means the guest
    never answered the ACPI shutdown request -- e.g. a VM with no OS installed,
    or one sitting at a boot menu/UEFI shell, which has nothing listening.
    """
    pass
