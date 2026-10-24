"""A key's last use is recorded with its clock, and never by rewriting the key document.

`last_used_at` was written with a naive `datetime.utcnow().isoformat()`. A date-time string with no
offset is parsed as local time by JavaScript, so the add-on health panel rendered a credential used
at noon in Jakarta as having been used at five in the morning. `credential_expires_at`, beside it,
was always written through `utc_isoformat`.

It was also written by rewriting the whole key document from a cached read without the key's lock,
on every authenticated request at most once per five minutes per worker. That write could put back
a grant a cleanup had just removed, an old `is_revoked`, or old capabilities. The use now goes to
its own table (services/api_key_usage.py).
"""

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.models.api_key_usage_models import ApiKeyUsage
from app.services import api_key_usage
from tests._project_delete_fakes import SessionMaker
from tests._sqlite_session import sqlite_session


@pytest.fixture
def usage(monkeypatch):
    db = sqlite_session(ApiKeyUsage)
    monkeypatch.setattr(api_key_usage, "session_maker", SessionMaker(db))
    return db


@pytest.mark.asyncio
async def test_the_recorded_use_carries_a_utc_offset_like_expiry_does(usage) -> None:
    from app.core.api_key_expiry import utc_isoformat

    await api_key_usage.record_use("key-1")
    stored = (await api_key_usage.last_used(["key-1"]))["key-1"]

    parsed = datetime.fromisoformat(stored)
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), (
        f"{stored!r} has no UTC offset, so a browser reads it as local time"
    )
    assert stored.endswith("+00:00") and utc_isoformat().endswith("+00:00")


@pytest.mark.asyncio
async def test_a_use_only_moves_forward_and_the_newer_of_two_values_is_shown(usage) -> None:
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    usage.add(ApiKeyUsage(key_id="key-1", last_used_at=later))
    usage.sync.commit()
    await api_key_usage.record_use("key-1")
    assert datetime.fromisoformat((await api_key_usage.last_used(["key-1"]))["key-1"]) == later

    legacy = "2026-01-01T00:00:00+00:00"
    assert api_key_usage.newer(legacy, later.isoformat()) == later.isoformat()
    assert api_key_usage.newer(later.isoformat(), legacy) == later.isoformat()
    assert api_key_usage.newer(legacy, None) == legacy and api_key_usage.newer(None, None) is None

    await api_key_usage.forget("key-1")
    assert await api_key_usage.last_used(["key-1"]) == {}


def test_the_request_path_never_writes_the_key_document() -> None:
    from app.routers import integrations

    assert not hasattr(integrations, "update_api_key_last_used")
    source = inspect.getsource(integrations._schedule_last_used_update)
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(ast.parse(source.lstrip()))
        if isinstance(node, ast.Call)
    }
    assert "schedule_use" in calls
    assert not calls & {"vault_kv_put", "get_api_key_from_vault", "submit"}, calls
