"""
LXD Service - Compatibility wrapper.
Re-exports from the modular lxd package for backward compatibility.
"""

from .lxd import (
    LXDService,
    get_lxd_service,
    LXDConnectionError,
    LXDOperationError,
    LXDGracefulTimeoutError,
    LXDSnapshotExistsError,
)

__all__ = [
    'LXDService',
    'get_lxd_service',
    'LXDConnectionError',
    'LXDOperationError',
    'LXDGracefulTimeoutError',
    'LXDSnapshotExistsError',
]
