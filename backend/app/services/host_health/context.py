"""What every check is handed: settings, the store, and one clock reading for the pass."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.host_health.store import HostHealthStore


@dataclass(frozen=True)
class CheckContext:
    settings: Any
    store: HostHealthStore
    now: float
