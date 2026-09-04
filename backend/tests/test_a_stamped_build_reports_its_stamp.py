"""A build that WAS stamped answers with what it was stamped with, through the real endpoints.

Its sibling proves the unstamped case: empty string in, null out. Nothing proved the other
direction. Every claim that a stamped deployment reports a real version and commit rested on a
manual check against one running container, which no longer exists as evidence the moment that
container is replaced.

This drives the same two surfaces the unstamped test does, with the settings stamped, and asserts
the split as well as the values: the version reaches the unauthenticated surface, the commit and the
build date do not.

The settings are cached, so the cache is cleared around the override rather than trusting import
order. No database, no Vault and no network: `TestClient` outside a `with` block runs no lifespan.
"""
import sys
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import build_identity  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

STAMPED_VERSION = "1.2.3-test"
STAMPED_COMMIT = "39787e8c1d4b"
STAMPED_DATE = "2026-09-04T00:00:00Z"


@pytest.fixture
def stamped(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_VERSION", STAMPED_VERSION)
    monkeypatch.setenv("BUILD_COMMIT", STAMPED_COMMIT)
    monkeypatch.setenv("BUILD_DATE", STAMPED_DATE)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def test_the_readers_return_what_the_build_was_stamped_with(stamped: None) -> None:
    assert build_identity.version() == STAMPED_VERSION
    assert build_identity.commit() == STAMPED_COMMIT
    assert build_identity.built_at() == STAMPED_DATE
    assert build_identity.full() == {
        "version": STAMPED_VERSION,
        "commit": STAMPED_COMMIT,
        "built_at": STAMPED_DATE,
    }


def test_health_publishes_the_stamped_version_and_still_withholds_the_rest(stamped: None) -> None:
    body = TestClient(create_app(), raise_server_exceptions=False).get("/health").json()

    assert body["version"] == STAMPED_VERSION
    assert STAMPED_COMMIT not in repr(body), (
        "a stamped commit must not appear anywhere on the unauthenticated surface"
    )
    assert STAMPED_DATE not in repr(body)


def test_the_root_endpoint_publishes_the_stamped_version_too(stamped: None) -> None:
    body = TestClient(create_app(), raise_server_exceptions=False).get("/").json()

    assert body["version"] == STAMPED_VERSION
    assert STAMPED_COMMIT not in repr(body)
    assert STAMPED_DATE not in repr(body)
