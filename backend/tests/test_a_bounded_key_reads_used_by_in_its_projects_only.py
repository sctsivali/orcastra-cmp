"""A key bound to some projects reads network and pool references in those projects only (A4).

LXD's used_by on a network or a storage pool is cluster-wide: it names every project's instances,
profiles and volumes, member names included. The structured proxy (`lxd.proxy` list_networks and
list_storage_pools) and the MCP `lxd_list_resources` tool, which goes through it, emitted that list
raw, so a key granted only its own organization's projects read another organization's inventory
on a shared cluster. The key's own project grant (`visible_projects`, None for `*`) now holds the
references, the same set the operation reads are already filtered by. The raw proxy was left raw
at first and is held to the same grant since 2026-11-06
(test_a_raw_read_keeps_used_by_in_the_key_projects.py).
"""

from types import SimpleNamespace

from app.services.lxd.proxy_operations import execute_proxy_operation

MINE = "/1.0/instances/web?project=mine"
THEIRS = "/1.0/instances/db?project=theirs"
THEIR_PROFILE = "/1.0/profiles/x?project=theirs"
DEFAULT_PROFILE = "/1.0/profiles/default"


class _Manager:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def _service():
    network = SimpleNamespace(name="br0", type="bridge", managed=True, description="", used_by=[MINE, THEIRS, DEFAULT_PROFILE])
    pool = SimpleNamespace(name="pool", driver="zfs", description="", status="Created", used_by=[MINE, THEIR_PROFILE])
    client = SimpleNamespace(networks=_Manager([network]), storage_pools=_Manager([pool]), profiles=_Manager([]))
    manager = SimpleNamespace(get_client=lambda _cluster: client, get_project_client=lambda *_a, **_k: client)
    return SimpleNamespace(connection_manager=manager)


def _list(operation, visible):
    return execute_proxy_operation(_service(), "c1", operation, {}, "mine", visible_projects=visible)


def test_a_bounded_key_keeps_references_in_its_projects() -> None:
    assert _list("list_networks", frozenset({"mine"}))[0]["used_by"] == [MINE]
    assert _list("list_storage_pools", frozenset({"mine"}))[0]["used_by"] == [MINE]


def test_a_key_granted_default_sees_the_default_profile_reference() -> None:
    assert _list("list_networks", frozenset({"default"}))[0]["used_by"] == [DEFAULT_PROFILE]


def test_a_wildcard_key_keeps_every_reference() -> None:
    assert _list("list_networks", None)[0]["used_by"] == [MINE, THEIRS, DEFAULT_PROFILE]


def test_the_default_grant_fails_closed() -> None:
    assert _list("list_storage_pools", frozenset())[0]["used_by"] == []
