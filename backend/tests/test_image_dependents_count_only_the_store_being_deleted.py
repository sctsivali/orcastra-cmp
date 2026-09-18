"""Deleting an image out of one store counts only the instances that read that store.

The same fingerprint can sit in a project's own store and in the host's default store.
Deleting the default-store copy leaves the own-store project's instances with their image,
so they are not its dependents, and the other way round. The instance's project decides
which store it reads (`features.images`), read once from the project configs.
"""

from tests.test_image_delete import FP, HOST, _FakeImage, _VM, _client, _service

PROJECTS = {"default": {}, "own": {"features.images": "true"}, "shared": {"features.images": "false"}}


def _fleet():
    in_own = _VM("own-01")
    in_own.project = "own"
    in_shared = _VM("shared-01")
    in_shared.project = "shared"
    in_default = _VM("default-01")
    return [in_own, in_shared, in_default]


def test_usage_out_of_the_default_store_names_the_shared_and_default_instances_only():
    client = _client(_service(_FakeImage(), vms=_fleet(), projects=PROJECTS))
    r = client.get(f"/images/{FP}/usage", params={"host": HOST, "project": "shared"})
    assert r.status_code == 200, r.text
    assert r.json()["project"] == "shared"
    assert r.json()["dependents"] == ["default-01", "shared-01"]


def test_usage_out_of_an_own_store_names_that_projects_instances_only():
    image = _FakeImage()
    image.project = "own"
    client = _client(_service(image, vms=_fleet(), projects=PROJECTS))
    r = client.get(f"/images/{FP}/usage", params={"host": HOST, "project": "own"})
    assert r.status_code == 200, r.text
    assert r.json()["dependents"] == ["own-01"]


def test_a_delete_out_of_an_own_store_is_not_refused_for_the_default_stores_instances():
    image = _FakeImage()
    image.project = "own"
    vms = [v for v in _fleet() if v.project != "own"]
    client = _client(_service(image, vms=vms, projects=PROJECTS))
    r = client.delete(f"/images/{FP}", params={"host": HOST, "project": "own"})
    assert r.status_code == 200, r.text
    assert image.deleted is True
