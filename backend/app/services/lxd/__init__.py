"""
LXD Service Package - Modular LXD management.

This package provides a modular architecture for managing LXD operations:
- connection: Connection and client management
- exceptions: Custom exception classes
- utils: Helper functions and utilities
- resources: Resource tracking and metrics
- instance: VM/container lifecycle operations
- projects: Project management
- images: Image management
"""

from .lxd_service import LXDService, get_lxd_service
from .exceptions import LXDConnectionError, LXDOperationError

__all__ = [
    'LXDService',
    'get_lxd_service',
    'LXDConnectionError',
    'LXDOperationError',
]
