"""The stranded-rebuild sweep names a session maker that exists.

An import of a name `app.core.database` does not export fails only when the function runs, and
then no worker fails a rebuild whose driver died with its process. This runs
`recover_stranded_rebuilds` against a session whose select answers nothing and asserts it
returns 0 instead of raising ImportError.
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
