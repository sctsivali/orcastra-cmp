"""
LXD Service - Compatibility wrapper.
Re-exports from the modular lxd package for backward compatibility.
"""

from .lxd import (
    LXDService,
    get_lxd_service,
    LXDConnectionError,
    LXDLocalStorageError,
    LXDOperationError,
    LXDGracefulTimeoutError,
    LXDSnapshotExistsError,
    require_client,
    unreachable_detail,
)

__all__ = [
    'LXDService',
    'get_lxd_service',
    'LXDConnectionError',
    'LXDLocalStorageError',
    'LXDOperationError',
    'LXDGracefulTimeoutError',
    'LXDSnapshotExistsError',
    'require_client',
    'unreachable_detail',
]
