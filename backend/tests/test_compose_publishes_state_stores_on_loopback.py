"""Postgres and Redis are published on loopback by both compose files, with the template's values.

Docker-published ports bypass the host firewall, and Redis has no password. The template said
`REDIS_PORT=6381` and `POSTGRES_PORT=5432`, which docker-compose.prod.yml turned into a binding on
every interface, while the installers and the guide already wrote `127.0.0.1:<port>`. Both
compose files now read `[host_ip:]port`, and the template carries the loopback prefix.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml")
STORES = {"postgres": ("POSTGRES_PORT", 5432), "redis": ("REDIS_PORT", 6379)}
_VAR = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _template() -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _interpolate(text: str, env: Dict[str, Optional[str]]) -> str:
    return _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), text)


def _bindings(compose: str, service: str, env: Dict[str, Optional[str]]):
    document = yaml.safe_load((ROOT / compose).read_text())
    for entry in document["services"][service].get("ports", []):
        parts = _interpolate(str(entry), env).split(":")
        assert len(parts) == 3, f"{compose} {service}: '{entry}' renders as {':'.join(parts)}"
        yield parts


@pytest.mark.parametrize("compose", COMPOSE_FILES)
@pytest.mark.parametrize("service", sorted(STORES))
@pytest.mark.parametrize("source", ["template", "unset"])
def test_the_store_is_published_on_loopback_only(compose, service, source):
    env = _template() if source == "template" else {}
    variable, target = STORES[service]
    bindings = list(_bindings(compose, service, env))
    assert bindings, f"{compose} publishes no port for {service}"
    for host_ip, published, container in bindings:
        assert host_ip == "127.0.0.1", f"{compose} {service} via {variable} binds {host_ip or 'every interface'}"
        assert published.isdigit() and int(container) == target


def test_the_template_carries_the_loopback_prefix():
    template = _template()
    for variable, _target in STORES.values():
        assert template[variable].startswith("127.0.0.1:"), variable


def test_a_bare_port_would_be_caught():
    with pytest.raises(AssertionError):
        for host_ip, _p, _c in _bindings("docker-compose.prod.yml", "redis", {"REDIS_PORT": "6381"}):
            assert host_ip == "127.0.0.1"
