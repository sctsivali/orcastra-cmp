"""An image import is refused on an unpatched build when the uploader does not hold the cluster.

The daemon unpacks an imported image's metadata area as root on the host. On a build without the
fixes, a crafted image turns that into host file access: symlinks in image data (fixed in LXD
5.21.6, 5.0.8, 4.0.12 and 6.9) and, on Incus, special files and hard links in the metadata area
(GHSA-5pgx-q9xx-j3qv, fixed in 7.6.0 and the LTS releases after 7.0.2 and 6.0.7). On a cluster one
party holds alone that is the party's own host, so the upload stays allowed; where another party
holds the cluster, or the uploader is an integration key, it is a way out of the tenancy.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import image_import_gate as gate


def _client(server: str, version: str):
    return SimpleNamespace(host_info={"auth": "trusted", "environment": {"server": server, "server_version": version}})


@pytest.fixture
def world(monkeypatch):
    state = {"held_elsewhere": set(), "client": _client("incus", "7.0.2")}

    async def _held(nodes, db, user):
        return set(state["held_elsewhere"]) & set(nodes)

    async def _client_for(_service, _cluster):
        return state["client"]

    monkeypatch.setattr(gate, "clusters_held_elsewhere", _held)
    monkeypatch.setattr(gate, "client_for", _client_for)
    return state


async def test_a_single_holder_may_import_on_an_unpatched_build(world):
    await gate.refuse_unpatched_import(object(), "incus-lts", db=None, user=None)


async def test_a_shared_cluster_on_an_unpatched_incus_build_is_refused_with_the_build_to_move_to(world):
    world["held_elsewhere"] = {"incus-lts"}
    with pytest.raises(HTTPException) as caught:
        await gate.refuse_unpatched_import(object(), "incus-lts", db=None, user=None)
    assert caught.value.status_code == 409
    assert caught.value.detail["error"] == "unpatched_image_import"
    assert "Incus 7.0.2" in caught.value.detail["message"] and "7.0.3" in caught.value.detail["message"]


async def test_an_integration_key_is_refused_on_an_unpatched_build_wherever_it_is(world):
    with pytest.raises(HTTPException) as caught:
        await gate.refuse_unpatched_import(object(), "incus-lts", via_api_key=True)
    assert caught.value.status_code == 409


@pytest.mark.parametrize("server,version", [("incus", "7.6"), ("incus", "6.0.8"), ("lxd", "5.21.8"), ("lxd", "6.9")])
async def test_a_patched_build_imports_for_everyone(world, server, version):
    world["held_elsewhere"] = {"c"}
    world["client"] = _client(server, version)
    await gate.refuse_unpatched_import(object(), "c", db=None, user=None)
    await gate.refuse_unpatched_import(object(), "c", via_api_key=True)


async def test_an_unpatched_lxd_build_is_refused_where_others_hold_it(world):
    world["held_elsewhere"] = {"c"}
    world["client"] = _client("lxd", "5.21.5")
    with pytest.raises(HTTPException) as caught:
        await gate.refuse_unpatched_import(object(), "c", db=None, user=None)
    assert "LXD 5.21.5" in caught.value.detail["message"] and "5.21.6" in caught.value.detail["message"]


async def test_a_build_that_cannot_be_read_is_not_waved_through(world):
    world["held_elsewhere"] = {"c"}
    world["client"] = SimpleNamespace(host_info={"auth": "untrusted"})
    with pytest.raises(HTTPException) as caught:
        await gate.refuse_unpatched_import(object(), "c", db=None, user=None)
    assert caught.value.status_code == 503
