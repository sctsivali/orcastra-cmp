"""Routers module containing API endpoints."""

from .vms import router as vms_router
from .images import router as images_router
from .nodes import router as nodes_router
from .projects import router as projects_router
from .tags import router as tags_router
from .instances import router as instances_router
from .profiles import router as profiles_router
from .networks import router as networks_router
from .network_acls import router as network_acls_router
from .storage_pools import router as storage_pools_router
from .storage_volumes import router as storage_volumes_router
from .lxd_operations import router as lxd_operations_router
from .lxd_warnings import router as lxd_warnings_router
from .lxd_server import router as lxd_server_router
from .permissions import identities_router, groups_router
from .ipam import router as ipam_router
from .my_keys import router as my_keys_router

__all__ = [
    "vms_router",
    "images_router", 
    "nodes_router", 
    "projects_router", 
    "tags_router",
    "instances_router",
    "profiles_router",
    "networks_router",
    "network_acls_router",
    "storage_pools_router",
    "storage_volumes_router",
    "lxd_operations_router",
    "lxd_warnings_router",
    "lxd_server_router",
    "identities_router",
    "groups_router",
    "ipam_router",
    "my_keys_router",
]
