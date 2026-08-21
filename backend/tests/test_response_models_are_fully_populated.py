"""Fields a response model declares must actually be sent.

Both cases here were the same shape: a model declared a field, a handler or serializer never
set it, and `response_model` silently emitted the default. That is worse than a missing field,
because the frontend can read it, type it, and build a control on it without ever seeing an
error. The images Flags facet did exactly that: it offered Cached and Auto-update filters over
two values the backend had never sent, so those options could not appear no matter what the
fleet contained.
"""

import ast
import inspect
from pathlib import Path

from app.models.schemas import ImageInfo
from app.routers.permissions import IdentityInfo
from app.services.lxd import images as images_service

BACKEND = Path(__file__).resolve().parents[1]


def _keywords_passed_to(source: str, call_name: str) -> set[str]:
    """Keyword names handed to `call_name(...)` anywhere in the source."""
    passed: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == call_name:
            passed.update(k.arg for k in node.keywords if k.arg)
    return passed


def test_image_flags_the_frontend_filters_on_are_declared() -> None:
    for field in ("cached", "auto_update", "created_at", "last_used_at", "expires_at", "profiles"):
        assert field in ImageInfo.model_fields, f"ImageInfo lost {field}"


def test_image_list_populates_every_declared_field() -> None:
    source = inspect.getsource(images_service)
    passed = _keywords_passed_to(source, "ImageInfo")
    missing = set(ImageInfo.model_fields) - passed
    assert not missing, f"ImageInfo declares fields the service never sets: {sorted(missing)}"


def test_auto_update_is_read_defensively() -> None:
    # pylxd declares auto_update optional, so the attribute is absent rather than falsy when
    # LXD omits it, and a direct read raises AttributeError mid-listing.
    source = inspect.getsource(images_service)
    assert 'getattr(image, "auto_update"' in source, (
        "read auto_update with getattr and a default: pylxd marks it optional, so a direct "
        "attribute access raises when LXD does not send it"
    )


def test_get_identity_sets_the_fields_it_declares() -> None:
    source = (BACKEND / "app" / "routers" / "permissions.py").read_text()
    handler = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "get_identity"
    )
    passed = _keywords_passed_to(ast.unparse(handler), "IdentityInfo")
    missing = set(IdentityInfo.model_fields) - passed
    assert not missing, f"get_identity leaves declared fields unset: {sorted(missing)}"


def test_cluster_member_sentinel_is_not_a_member_name() -> None:
    """LXD says "none" when a target is not clustered, and that is not a member called none."""
    from app.services.lxd.instance import _cluster_member

    for sentinel in (None, "", "  ", "none", "None", "NONE"):
        assert _cluster_member(sentinel) is None, f"{sentinel!r} should read as absent"
    assert _cluster_member("member-a") == "member-a"
    assert _cluster_member("  member-a  ") == "member-a"
