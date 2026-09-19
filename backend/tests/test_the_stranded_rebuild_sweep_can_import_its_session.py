"""The stranded-rebuild sweep names a session maker that exists.

`recover_stranded_rebuilds` imported `AsyncSessionLocal`, a name `app.core.database` never
exported, so every boot since 2026-09-08 logged "Could not start import recovery loop" and no
worker ever failed a rebuild whose driver had died with its process. A green suite hid it
because nothing called the function. This runs it against a session whose select answers
nothing and asserts it returns rather than raising ImportError.
"""

import pytest

from app.routers import instance_rebuild


class _Result:
    def scalars(self):
        return self

    def all(self):
        return []


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_k):
        return _Result()

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_the_sweep_runs_with_the_session_maker_the_module_exports(monkeypatch):
    from app.core import database

    assert hasattr(database, "async_session_maker")
    monkeypatch.setattr(database, "async_session_maker", lambda: _Session())
    assert await instance_rebuild.recover_stranded_rebuilds() == 0
