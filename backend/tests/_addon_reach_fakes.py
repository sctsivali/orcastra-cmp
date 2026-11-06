"""An add-on installation and its installer's bound, shared by the add-on reach suites."""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.services import key_party_bound as kpb

BOUND = frozenset({"default", "own"})



@dataclass
class Install:
    id: int
    status: str = "active"
    granted_scope: List[Dict[str, Any]] = field(default_factory=lambda: [{"cluster_id": "nuc2", "projects": ["*"]}])
    installed_by: str = "partner@example.test"
    api_key_id: str = "oak_addon"
    organization_id: int = 7


def hold_installer(monkeypatch) -> Dict[str, Any]:
    """The installer's bound and role, as state a test changes: `bound` (None for no bound), `admin`."""
    state: Dict[str, Any] = {"bound": BOUND, "admin": False}

    async def bound(_api_key, _cluster_id, _db):
        return "partner@example.test", state["bound"]

    async def is_admin(_email, *, roles=None):
        return state["admin"]

    monkeypatch.setattr(kpb, "_bound", bound)
    monkeypatch.setattr(kpb, "creator_is_admin", is_admin)
    return state

