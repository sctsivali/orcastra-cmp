"""The 16 KB manifest cap was documented, promised, and enforced only in the browser.

`MANIFEST_MAX_BYTES` had exactly one reference in the backend: its own definition. The dialog
refused an oversized manifest and the API accepted it, so publishing the same document through the
two supported paths gave two different answers, and the public documentation stated the browser's
answer as a server rule.

note: this is a contract bound, not a memory bound, and the code says so. Nothing in the middleware
stack limits a request body, so the bytes have already been read by the time this refuses. The
comment that used to sit above the constant claimed the opposite.
"""

import asyncio

import pytest
from fastapi import HTTPException

from app.core.extension_manifest import MANIFEST_MAX_BYTES
from app.routers.extensions import assert_manifest_within_limit


class FakeRequest:
    """Only the part the guard reads. FastAPI has already cached the body by then."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def body(self) -> bytes:
        return self._payload


def _check(size: int) -> None:
    asyncio.run(assert_manifest_within_limit(FakeRequest(b"x" * size)))


def test_a_manifest_at_the_limit_is_accepted() -> None:
    """The positive twin. Without it, a guard that refused everything would pass the test below."""
    _check(MANIFEST_MAX_BYTES)


def test_one_byte_over_is_refused() -> None:
    with pytest.raises(HTTPException) as refusal:
        _check(MANIFEST_MAX_BYTES + 1)

    assert refusal.value.status_code == 413
    detail = str(refusal.value.detail)
    assert str(MANIFEST_MAX_BYTES) in detail, (
        "the refusal does not say what the limit is, so an author has to guess how much to cut"
    )
    assert str(MANIFEST_MAX_BYTES + 1) in detail, "it does not say how big the manifest was either"


def test_both_manifest_routes_carry_the_guard() -> None:
    """A cap on one of the two ways to publish is not a cap.

    `POST /extensions` and `POST /extensions/{slug}/versions` both take a whole manifest. Checking
    the dependency in isolation cannot see whether either route actually depends on it, and a route
    added later would inherit nothing.
    """
    import inspect

    from app.routers import extensions

    for handler in ("register_extension", "publish_extension_version"):
        source = inspect.getsource(getattr(extensions, handler))
        assert "assert_manifest_within_limit" in source, (
            f"{handler} accepts a manifest without the size guard, so that route is uncapped"
        )
