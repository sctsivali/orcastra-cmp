"""GET /instances/{name} searches every project and must say which one held the instance.

The VMInfo conversion reads the wrapper's `project`; without it, an instance in any other
project reads as `default`. The detail dialog prefers that single read over the list row, so its
Devices tab would send the fixed-address write to `default`, where the cluster answers "Instance
not found".
"""

from types import SimpleNamespace

from app.services.lxd.utils import InstanceWrapper, find_instance_in_projects


class _Response:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _client(found_in: str):
    def get(params=None):
        if (params or {}).get("project") != found_in:
            raise RuntimeError("not found")
        return _Response({"metadata": {"name": "web01", "status": "Running", "status_code": 103,
                                       "architecture": "x86_64", "type": "container", "config": {},
                                       "devices": {}, "expanded_devices": {}, "profiles": ["default"]}})
    api = SimpleNamespace(instances={"web01": SimpleNamespace(get=get)})
    projects = SimpleNamespace(all=lambda: [SimpleNamespace(name="default"), SimpleNamespace(name="UAT-Herald")])
    return SimpleNamespace(api=api, projects=projects)


def test_the_wrapper_names_the_project_it_was_found_in():
    instance, project = find_instance_in_projects(_client("UAT-Herald"), "web01")
    assert project == "UAT-Herald"
    assert isinstance(instance, InstanceWrapper)
    assert getattr(instance, "project", None) == "UAT-Herald", "the VMInfo conversion reads `project`"


def test_the_default_project_is_still_named_as_such():
    instance, project = find_instance_in_projects(_client("default"), "web01")
    assert project == "default" and instance.project == "default"
