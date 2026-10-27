"""The branding provider link must never become an access path.

organizations.provider_organization_id says whose look a buyer shows. It is filled by provisioning
from a sale and can be set by an Administrator; if any scope or RBAC check read it, a link would
quietly widen what an organization can reach. This test pins every reader to the branding code.
"""

from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
ALLOWED = {
    "models/db_models.py",
    "models/branding_models.py",
    "services/branding/store.py",
    "services/branding/provider_link.py",
    "routers/organization_branding.py",
}


def test_only_the_branding_code_reads_the_provider_link() -> None:
    readers = {
        str(path.relative_to(APP))
        for path in APP.rglob("*.py")
        if "provider_organization_id" in path.read_text(encoding="utf-8")
    }
    assert readers <= ALLOWED, f"the provider link is read outside branding: {sorted(readers - ALLOWED)}"
    assert "core/org_scope.py" not in readers
