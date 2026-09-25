"""A cluster that holds instance and profile config, for the tests that judge cloud-init by what changes.

The session records every call the way the real session turns it into a request on the wire (verb,
URL and keyword arguments, the project query included), and answers a GET of /<family>/<name> with
the record it holds in that project, after decoding the name once as LXD does. Any other call is
answered like tests/_raw_proxy_fakes.RecordingSession. `fail` makes every GET answer one status or
raise one exception, the way a cluster that errors, times out or cannot be reached does.
"""

from typing import Any, Dict, Optional, Tuple, Union
from urllib.parse import unquote

import requests

from tests._raw_proxy_fakes import RecordingSession

Held = Dict[Tuple[str, str], Any]


def lxd_response(status: int, body: Any) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = requests.compat.json.dumps(body).encode()
    return response


class ConfigSession(RecordingSession):
    """Holds records keyed by (project, "/<family>/<name>") and records every call."""

    def __init__(self, held: Optional[Held] = None, fail: Union[None, int, BaseException] = None) -> None:
        super().__init__()
        self.held: Held = dict(held or {})
        self.fail = fail

    @property
    def reads(self) -> list:
        return [(url, (kwargs.get("params") or {}).get("project") or "default")
                for verb, url, kwargs in self.calls if verb == "GET"]

    @property
    def writes(self) -> list:
        return [(verb, kwargs.get("json")) for verb, _url, kwargs in self.calls if verb != "GET"]

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append(("GET", url, kwargs))
        if isinstance(self.fail, BaseException):
            raise self.fail
        if isinstance(self.fail, int):
            return lxd_response(self.fail, {"type": "error", "error": "boom", "error_code": self.fail})
        project = (kwargs.get("params") or {}).get("project") or "default"
        tail = url.split("/1.0/", 1)[1].split("?", 1)[0]
        path = "/" + "/".join(unquote(part) for part in tail.split("/"))
        record = self.held.get((project, path))
        if record is None:
            return lxd_response(404, {"type": "error", "error": "Not Found", "error_code": 404})
        return lxd_response(200, {"type": "sync", "metadata": record})


def holding(config: Dict[str, Any], *paths: str, project: str = "default") -> ConfigSession:
    """A session whose objects at `paths` in `project` hold `config` as their own."""
    return ConfigSession({(project, path): {"name": path.rsplit("/", 1)[-1], "config": dict(config)}
                          for path in paths})
