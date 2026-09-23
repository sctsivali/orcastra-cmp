"""Composed Migration Center application service."""

from .actions import MigrationActionsMixin
from .planning import MigrationPlanningMixin


class MigrationService(MigrationActionsMixin, MigrationPlanningMixin):
    """Plan and operate durable migrations through one stable service API."""
