"""A rebuild LXD refused leaves the instance as it found it, and its URL names exactly that instance.

cloud-init user-data is written before the rebuild so the new root disk reads it on first boot. When
LXD refuses the rebuild, that write must be undone, or the next boot of the old disk runs a
cloud-init nobody asked for. When the outcome is unknown (the wait timed out), the rebuild may still
finish, so the new user-data stays.
"""

from types import SimpleNamespace

import pytest

from app.core.blocking import BlockingTimeout
from app.services.lxd import instance as lxd_instance
from app.services.lxd.exceptions import LXDOperationError

NAME, HOST = "vm1", "cluster-a"


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _World:
    def __init__(self, post_status=202, op_status="Success", previous="#cloud-config\nold: true\n"):
        self.patches, self.posts = [], []
        self.post_status, self.op_status = post_status, op_status
        world = self

        class _Instances:
            def __getitem__(self, name):
                return SimpleNamespace(patch=lambda json, params: world.patches.append((name, json, params)))

        def post(url, json, verify, timeout):
            world.posts.append(url)
            if world.post_status >= 400:
                return _Response(world.post_status, {"error": "refused"})
            return _Response(202, {"operation": "/1.0/operations/op-1"})

        def get(url, verify, timeout):
            return _Response(200, {"metadata": {"status": world.op_status, "err": "disk full"}})

        self.client = SimpleNamespace(api=SimpleNamespace(
            instances=_Instances(), _api_endpoint="https://lxd.example.org:8443/1.0",
            session=SimpleNamespace(post=post, get=get),
        ))
        self.instance = SimpleNamespace(config={"cloud-init.user-data": previous} if previous is not None else {})


@pytest.fixture
def patched(monkeypatch):
    def install(world):
        async def no_fence(**kwargs):
            return None

        monkeypatch.setattr(lxd_instance, "find_instance_in_projects", lambda client, name, project: (world.instance, project))
        monkeypatch.setattr(lxd_instance, "assert_instance_not_migration_fenced", no_fence)
        monkeypatch.setattr(lxd_instance, "_lxd_snapshot_names", lambda client, name, project: [])
        return SimpleNamespace(_get_client_with_reconnect=lambda host: world.client)
    return install


async def _rebuild(manager, project="cust a/b", cloud_init="#cloud-config\nnew: true\n"):
    return await lxd_instance.InstanceManager.rebuild_instance(
        manager, NAME, HOST, {"type": "image", "alias": "ubuntu/24.04"}, project=project, cloud_init=cloud_init,
    )


def _user_data(patches):
    return [json["config"]["cloud-init.user-data"] for _, json, _ in patches]


async def test_a_refused_rebuild_puts_the_old_user_data_back(patched):
    world = _World(post_status=400)
    with pytest.raises(LXDOperationError):
        await _rebuild(patched(world))
    assert _user_data(world.patches) == ["#cloud-config\nnew: true\n", "#cloud-config\nold: true\n"]


async def test_a_failed_operation_puts_the_old_user_data_back(patched):
    world = _World(op_status="Failure")
    with pytest.raises(LXDOperationError):
        await _rebuild(patched(world))
    assert _user_data(world.patches)[-1] == "#cloud-config\nold: true\n"


async def test_an_instance_that_had_none_gets_the_key_unset_again(patched):
    world = _World(post_status=500, previous=None)
    with pytest.raises(LXDOperationError):
        await _rebuild(patched(world))
    assert _user_data(world.patches) == ["#cloud-config\nnew: true\n", ""]


async def test_a_rebuild_that_ran_past_its_wait_keeps_the_new_user_data(patched, monkeypatch):
    world = _World()

    async def times_out(fn, **kwargs):
        raise BlockingTimeout("rebuild", 1800)

    monkeypatch.setattr(lxd_instance, "run_blocking", times_out)
    with pytest.raises(LXDOperationError):
        await _rebuild(patched(world))
    assert _user_data(world.patches) == ["#cloud-config\nnew: true\n"]


async def test_a_successful_rebuild_writes_the_user_data_once(patched):
    world = _World()
    assert await _rebuild(patched(world)) == "op-1"
    assert _user_data(world.patches) == ["#cloud-config\nnew: true\n"]


async def test_the_rebuild_url_escapes_the_project(patched):
    world = _World()
    await _rebuild(patched(world), project="cust a/b", cloud_init=None)
    assert world.posts == ["https://lxd.example.org:8443/1.0/instances/vm1/rebuild?project=cust%20a%2Fb"]
    assert world.patches == []


# ── an accepted rebuild whose result could not be read ────────────────────────────────────────
# Once LXD accepted the rebuild, a broken wait says nothing about the rebuild: the daemon may still
# be rewriting the disk, so the error is typed, carries the operation id, and keeps the new
# user-data. A failure before LXD accepted anything stays an ordinary failure.

def test_a_wait_that_breaks_after_lxd_accepted_is_an_unknown_outcome(patched):
    import asyncio
    from requests import exceptions as requests_exc
    from app.services.lxd.exceptions import LXDRebuildOutcomeUnknown

    world = _World()

    def broken_wait(url, verify, timeout):
        raise requests_exc.ConnectionError("connection reset by peer")

    world.client.api.session.get = broken_wait
    with pytest.raises(LXDRebuildOutcomeUnknown) as caught:
        asyncio.run(_rebuild(patched(world)))
    assert caught.value.operation_id == "op-1"
    assert "may still be running" in str(caught.value)
    assert _user_data(world.patches) == ["#cloud-config\nnew: true\n"]


def test_a_post_lxd_never_answered_is_an_ordinary_failure(patched):
    import asyncio
    from requests import exceptions as requests_exc
    from app.services.lxd.exceptions import LXDRebuildOutcomeUnknown

    world = _World()

    def unreachable(url, json, verify, timeout):
        raise requests_exc.ConnectionError("connection refused")

    world.client.api.session.post = unreachable
    with pytest.raises(Exception) as caught:
        asyncio.run(_rebuild(patched(world)))
    assert not isinstance(caught.value, LXDRebuildOutcomeUnknown)


async def test_a_wait_that_runs_out_after_lxd_accepted_is_an_unknown_outcome(patched, monkeypatch):
    from app.services.lxd.exceptions import LXDRebuildOutcomeUnknown

    world = _World()

    def still_waiting(url, verify, timeout):
        raise TimeoutError("the thread is still waiting when the caller gives up")

    world.client.api.session.get = still_waiting

    async def gives_up(fn, **kwargs):
        try:
            fn()
        except TimeoutError:
            pass
        raise BlockingTimeout("rebuild", 1800)

    monkeypatch.setattr(lxd_instance, "run_blocking", gives_up)
    with pytest.raises(LXDRebuildOutcomeUnknown) as caught:
        await _rebuild(patched(world))
    assert caught.value.operation_id == "op-1"
