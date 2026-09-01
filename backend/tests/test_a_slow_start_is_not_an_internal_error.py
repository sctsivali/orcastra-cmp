"""A start that outlives the pooled read timeout must not surface as a 500.

`start(wait=True)` holds the HTTP response open for the whole LXD operation, and the pooled
client only allows an 8s read (`DEFAULT_LXD_TIMEOUT`). Against a loaded cluster the READ timed
out while the instance went on starting, so `start_vm` raised a raw requests ConnectionError,
the router's `except Exception` turned it into `500 Internal server error. Reference: ...`, and
the operator was told the server broke about an action that had in fact succeeded. Observed
live at 8.9s on a busy cluster, with the instance reaching Running anyway.

`stop`/`restart` never had this: both rebind onto a client whose read timeout outlives the wait
(`_rebind_for_wait`). Start was the odd one out, in three places. What is pinned here is that
asymmetry staying closed, plus the retype that keeps a genuine transport failure a 503 rather
than an internal fault.
"""

import ast
import inspect
import pathlib

import pytest
from requests import exceptions as requests_exc

from app.services.lxd import instance as instance_module
from app.services.lxd.connection import DEFAULT_LXD_TIMEOUT
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.instance import InstanceManager

_POOLED_READ_TIMEOUT = DEFAULT_LXD_TIMEOUT[1]


class _Instance:
    def __init__(self, name="web-01"):
        self.name = name
        self.started = False
        self.synced = False

    def start(self, wait=False):
        self.started = True

    def sync(self):
        self.synced = True


class _Instances:
    def __init__(self, instance):
        self._instance = instance

    def get(self, name):
        return self._instance


class _Client:
    def __init__(self, instance):
        self.instances = _Instances(instance)


class _ConnectionManager:
    """Records the timeout every caller asks for."""

    def __init__(self, client=None):
        self.asked = []
        self._client = client

    def get_project_client(self, host, project, timeout):
        self.asked.append(timeout)
        return self._client


def _manager(connection_manager) -> InstanceManager:
    mgr = InstanceManager.__new__(InstanceManager)
    mgr.connection_manager = connection_manager
    return mgr


def test_the_start_waits_on_a_client_that_outlives_the_pooled_read_timeout():
    long_instance = _Instance()
    cm = _ConnectionManager(client=_Client(long_instance))
    pooled = _Instance()

    started = _manager(cm)._start_and_wait(pooled, "nuc2", "default")

    assert cm.asked, "start must ask for its own client instead of using the pooled one"
    read_timeout = cm.asked[0][1]
    assert read_timeout > _POOLED_READ_TIMEOUT, (
        f"asked for a {read_timeout}s read, which is no better than the pooled "
        f"{_POOLED_READ_TIMEOUT}s that produced the 500"
    )
    assert long_instance.started and long_instance.synced
    assert started is long_instance
    assert pooled.started is False, "the pooled instance must not be the one waited on"


def test_it_falls_back_to_the_pooled_instance_rather_than_failing():
    """A cluster we cannot build a second client for must still get the old behaviour."""
    cm = _ConnectionManager(client=None)
    pooled = _Instance()

    started = _manager(cm)._start_and_wait(pooled, "nuc2", "default")

    assert started is pooled
    assert pooled.started is True


@pytest.mark.asyncio
async def test_a_transport_failure_is_retyped_so_the_router_answers_503(monkeypatch):
    """Raw requests errors reached `except Exception` and became a 500 with a reference id."""
    mgr = _manager(_ConnectionManager())
    instance = _Instance()

    monkeypatch.setattr(mgr, "_get_client_with_reconnect", lambda host: _Client(instance))
    monkeypatch.setattr(mgr, "_get_node_endpoint", lambda host: "https://x:8443")
    monkeypatch.setattr(
        instance_module, "find_instance_in_projects", lambda c, n, p: (instance, "default")
    )

    def _boom(*a, **k):
        raise requests_exc.ConnectionError(
            "HTTPSConnectionPool(host='10.0.0.5', port=8443): Read timed out."
        )

    monkeypatch.setattr(mgr, "_start_and_wait", _boom)
    instance.status = "Stopped"

    with pytest.raises(LXDConnectionError):
        await mgr.start_vm("web-01", "nuc2")


def test_no_start_call_bypasses_the_long_timeout_helper():
    """The ratchet. Three call sites drifted from stop/restart; a fourth must not.

    Matches `<anything>.start(wait=...)` in the service module and allows it only inside the
    helper itself, so a new start path cannot quietly reintroduce the 8s read.
    """
    source = pathlib.Path(inspect.getfile(instance_module)).read_text()
    tree = ast.parse(source)

    helper = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_start_and_wait"
    )
    allowed = range(helper.lineno, helper.end_lineno + 1)

    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "start"
        and any(kw.arg == "wait" for kw in node.keywords)
        and node.lineno not in allowed
    ]
    assert offenders == [], (
        f"start(wait=...) called outside _start_and_wait at line(s) {offenders}. Those run on "
        f"the pooled {_POOLED_READ_TIMEOUT}s read timeout and will 500 on a slow cluster."
    )
