"""Publishing a new version, and the two things it must not disturb.

The documentation told authors twice to "publish a new version" and there was no call that could.
`POST /extensions` answers 409 on a duplicate slug and `PATCH` refuses a version change, so a
reader following the tutorial reached a dead end with no documented escape.

The rule is that a new version replaces what the add-on ASKS FOR, and changes nothing about what
an existing installation ALREADY HOLDS. Its grant lives on its own row, so it keeps working at the
version it consented to, and its stored digest stops matching, which makes any rescope answer 409
until an operator approves the new manifest. That is the re-consent the docs promise.

The second thing here is older and quieter: `update_extension` recomputed the manifest digest on
every edit, so correcting a typo in a description silently invalidated every existing consent. A
digest is a promise about what is asked for, not about the prose.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.core.extension_manifest import manifest_digest
from app.models.extension_schemas import ExtensionManifest, ExtensionUpdate
from app.services import extension_service


def _manifest(**over: Any) -> ExtensionManifest:
    base: Dict[str, Any] = {
        "slug": "acme-health",
        "name": "Acme Health",
        "publisher": "Acme Ltd",
        "version": "1.0.0",
        "description": "Watches clusters",
        "capabilities": ["inventory.read"],
        "scope": {"permission": "read", "clusters": "selected", "projects": "selected"},
    }
    base.update(over)
    return ExtensionManifest(**base)


@dataclass
class FakeExtension:
    slug: str = "acme-health"
    name: str = "Acme Health"
    publisher: str = "Acme Ltd"
    description: Optional[str] = "Watches clusters"
    homepage_url: Optional[str] = None
    support_url: Optional[str] = None
    version: str = "1.0.0"
    status: str = "published"
    owner_email: str = "author@example.com"
    requested_capabilities: List[str] = field(default_factory=lambda: ["inventory.read"])
    requested_scope: Dict[str, Any] = field(default_factory=dict)
    settings_schema: Optional[List[Dict[str, Any]]] = None
    default_webhook_url: Optional[str] = None
    default_webhook_events: Optional[List[str]] = None
    manifest: Dict[str, Any] = field(default_factory=dict)
    manifest_digest: str = "original-digest"


class FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        return None


def _publish(extension, manifest, actor="author@example.com"):
    return asyncio.run(
        extension_service.publish_version(
            FakeDB(), extension, manifest=manifest, actor_email=actor
        )
    )


# ── what a new version does ────────────────────────────────────────────────────────────────────

def test_a_greater_version_replaces_what_the_add_on_asks_for() -> None:
    extension = FakeExtension()
    before = extension.manifest_digest

    _publish(extension, _manifest(version="2.0.0", capabilities=["inventory.read", "lxd.proxy"]))

    assert extension.version == "2.0.0"
    assert extension.requested_capabilities == ["inventory.read", "lxd.proxy"]
    assert extension.manifest_digest != before, (
        "the digest must move, or an operator would be shown the old consent contract for a "
        "manifest that now asks for more"
    )
    assert extension.manifest_digest == manifest_digest(extension.manifest)


@pytest.mark.parametrize("version", ["1.0.0", "0.9.9", "1.0.0-rc.1"])
def test_a_version_that_does_not_move_forward_is_refused(version: str) -> None:
    extension = FakeExtension()
    with pytest.raises(HTTPException) as refusal:
        _publish(extension, _manifest(version=version))
    assert refusal.value.status_code == 409
    assert extension.version == "1.0.0", "a refused publish must change nothing"
    assert extension.manifest_digest == "original-digest"


def test_somebody_elses_add_on_cannot_be_versioned() -> None:
    """A 404 rather than a 403, matching `get_extension_or_404`: not-yours and not-there are
    deliberately indistinguishable, so the endpoint cannot be used to enumerate slugs."""
    extension = FakeExtension()
    with pytest.raises(HTTPException) as refusal:
        _publish(extension, _manifest(version="2.0.0"), actor="someone-else@example.com")
    assert refusal.value.status_code == 404
    assert extension.version == "1.0.0"


def test_a_new_version_may_not_change_the_slug() -> None:
    extension = FakeExtension()
    with pytest.raises(HTTPException) as refusal:
        _publish(extension, _manifest(slug="acme-something-else", version="2.0.0"))
    assert refusal.value.status_code == 422
    assert extension.slug == "acme-health"


def test_the_settings_schema_moves_with_the_version() -> None:
    """A new version that drops a settings field must drop it here too, or the form keeps
    rendering a field the add-on no longer reads."""
    extension = FakeExtension(settings_schema=[{"key": "old", "type": "string", "label": "Old"}])
    _publish(
        extension,
        _manifest(
            version="2.0.0",
            settings=[{"key": "retention_days", "type": "integer", "label": "Retention"}],
        ),
    )
    keys = [f["key"] for f in extension.settings_schema or []]
    assert keys == ["retention_days"]


# ── the quieter bug: a cosmetic edit must not invalidate consent ────────────────────────────────

def test_editing_a_description_leaves_the_digest_alone() -> None:
    extension = FakeExtension(manifest={"slug": "acme-health", "description": "Watches clusters"})
    asyncio.run(
        extension_service.update_extension(
            FakeDB(), extension, ExtensionUpdate(description="Watches clusters, carefully")
        )
    )
    assert extension.description == "Watches clusters, carefully"
    assert extension.manifest["description"] == "Watches clusters, carefully"
    assert extension.manifest_digest == "original-digest", (
        "a prose edit moved the consent digest, so every existing installation now disagrees with "
        "a document it never stopped agreeing with"
    )


def test_publishing_a_draft_still_works_through_the_same_path() -> None:
    """The positive twin for the test above. A guard that froze the digest by freezing the whole
    update would pass that assertion while breaking the draft-to-published step, which is the one
    an author needs most."""
    extension = FakeExtension(status="draft")
    asyncio.run(
        extension_service.update_extension(FakeDB(), extension, ExtensionUpdate(status="published"))
    )
    assert extension.status == "published"


# ── ordering, which is what decides "greater" ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "lower,higher",
    [
        ("1.0.0", "1.0.1"),
        ("1.0.9", "1.1.0"),
        ("1.9.0", "2.0.0"),
        # important: these two are the only pairs here that a string comparison gets WRONG, and
        # without them this test passes against a comparison that never parsed anything. The first
        # draft used ("1.0.0", "10.0.0"), which strings happen to order correctly, so ablating the
        # parser to `return (core,)` left the whole file green.
        ("2.0.0", "10.0.0"),
        ("9.0.0", "10.0.0"),
    ],
)
def test_versions_order_numerically_not_as_text(lower: str, higher: str) -> None:
    assert extension_service._semver_tuple(lower) < extension_service._semver_tuple(higher)


def test_a_double_digit_version_is_accepted_over_a_single_digit_one() -> None:
    """The behavioural half of the case above. Comparing as text refuses 10.0.0 over 9.0.0, which
    is an add-on author blocked from shipping by an off-by-a-character bug."""
    extension = FakeExtension(version="9.0.0")
    _publish(extension, _manifest(version="10.0.0"))
    assert extension.version == "10.0.0"
