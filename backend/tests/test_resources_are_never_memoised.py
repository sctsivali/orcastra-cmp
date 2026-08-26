"""Node resource numbers must be read live, never from pylxd's permanent memo.

pylxd caches the /1.0/resources body on the client object and never invalidates it:

    @property
    def resources(self):
        if self._resource_cache is None:
            ...
            self._resource_cache = response.json()["metadata"]
        return self._resource_cache

This app pools clients for the life of the process by design, so every reader of
`client.resources` was handed the body captured when that worker first connected. With
WEB_CONCURRENCY=4 that meant four frozen snapshots per cluster, and whichever worker served a
cache miss wrote its own into Redis. Measured on the dev fleet, the shared regions body cycled
through exactly four distinct memory_used values per host, with two unrelated hosts moving in
identical lockstep. Real host memory does not do that.

The behavioural test below is the one that reproduces the report: the fake client faithfully
reproduces pylxd, memo and all, so reading through the memo returns a pinned number and reading
live does not. The AST guard is what keeps it fixed, because `client.resources` is invisible in
review: it looks exactly like the obvious thing to write.
"""

import ast
from pathlib import Path

import pytest

from app.services.lxd.resources import ResourceManager


APP = Path(__file__).resolve().parents[1] / "app"
# The one module allowed to speak about the memo, and it only does so in prose.
ALLOWED = {"freshness.py"}


def _resource_property_reads(tree: ast.AST):
    """Every `<something named client>.resources` attribute read in a module."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "resources":
            continue
        base = node.value
        name = (
            base.id
            if isinstance(base, ast.Name)
            else base.attr
            if isinstance(base, ast.Attribute)
            else None
        )
        if name and "client" in name.lower():
            yield node.lineno


def test_no_module_reads_the_memoised_resources_property():
    """The ratchet. No allowlist: a new call site is always a regression."""
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for lineno in _resource_property_reads(tree):
            offenders.append(f"{path.relative_to(APP.parent)}:{lineno}")

    assert not offenders, (
        "client.resources is memoised for the life of the pooled client. "
        "Use app.services.lxd.freshness.read_resources instead. Offenders: "
        + ", ".join(offenders)
    )


class _Response:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return {"metadata": self._payload}


class _Instances:
    def all(self):
        return []


class _StoragePools:
    def all(self):
        return []


class _ResourcesEndpoint:
    """`client.api.resources` - a real round trip every time, as pylxd's _APINode is."""

    def __init__(self, client):
        self._client = client

    def get(self, *args, **kwargs):
        return _Response(self._client._next_body())


class _StoragePoolsEndpoint:
    def __getitem__(self, _name):
        return self

    @property
    def resources(self):
        return self

    def get(self, *args, **kwargs):
        return _Response({"space": {"total": 0, "used": 0}})


class _Api:
    def __init__(self, client):
        self.resources = _ResourcesEndpoint(client)
        self.storage_pools = _StoragePoolsEndpoint()


class _FakeClient:
    """Reproduces pylxd exactly, memo included, so the test can tell the two paths apart."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self._resource_cache = None
        self.reads = 0
        self.instances = _Instances()
        self.storage_pools = _StoragePools()
        self.api = _Api(self)

    def _next_body(self):
        body = self._bodies[min(self.reads, len(self._bodies) - 1)]
        self.reads += 1
        return body

    # --- the pylxd memo, faithfully ---
    @property
    def resources(self):
        if self._resource_cache is None:
            self._resource_cache = self._next_body()
        return self._resource_cache


def _body(memory_used):
    return {
        "cpu": {"total": 8, "sockets": [{}]},
        "memory": {"total": 66_000_000_000, "used": memory_used},
    }


def test_repeated_reads_track_the_host():
    """The reported bug: the Memory tab sat on one number while the host moved."""
    client = _FakeClient([_body(30_000_000_000), _body(31_500_000_000)])
    manager = ResourceManager()

    first = manager.get_node_resources(client, "nuc2")
    second = manager.get_node_resources(client, "nuc2")

    memory_total = first["memory_total"]
    assert first["memory_available"] == memory_total - 30_000_000_000
    assert second["memory_available"] == memory_total - 31_500_000_000, (
        "the second read returned the first sample: the memo is back"
    )


def test_the_memo_would_have_pinned_it():
    """Ablation, in-test: prove the fake reproduces the defect rather than assuming it."""
    client = _FakeClient([_body(30_000_000_000), _body(31_500_000_000)])

    assert client.resources["memory"]["used"] == 30_000_000_000
    assert client.resources["memory"]["used"] == 30_000_000_000  # pinned, forever


def test_a_node_status_check_reads_the_body_once():
    """One sample per request, for cost and for agreement.

    CPU/memory, the hardware profile and the GPU inventory all want this body. Reading it
    per consumer triples the fan-out and lets two panels of one modal disagree about the
    same host.
    """
    client = _FakeClient([_body(30_000_000_000)])
    manager = ResourceManager()

    manager.get_node_resources(client, "nuc2")

    assert client.reads == 1, f"expected one resources read, got {client.reads}"


def test_read_resources_reports_when_the_cluster_refuses():
    """A non-200 must not be parsed as a body of zeros."""
    from app.services.lxd.freshness import read_resources

    class _Refusing:
        class api:  # noqa: N801 - mirrors the pylxd attribute shape
            class resources:
                @staticmethod
                def get():
                    response = _Response({})
                    response.status_code = 503
                    return response

    with pytest.raises(RuntimeError):
        read_resources(_Refusing(), "nuc2")
