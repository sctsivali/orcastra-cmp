"""Instance runtime status, shared by the schema modules that describe an instance."""

from enum import Enum


class VMStatus(str, Enum):
    """Possible VM statuses."""
    RUNNING = "Running"
    STOPPED = "Stopped"
    FROZEN = "Frozen"
    ERROR = "Error"
    UNKNOWN = "Unknown"
