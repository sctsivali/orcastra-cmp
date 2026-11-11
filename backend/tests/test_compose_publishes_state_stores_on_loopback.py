"""Postgres and Redis are published on loopback by both compose files, whatever an old env says.

Docker-published ports bypass the host firewall, and Redis has no password. The old template set
REDIS_PORT=6381 and POSTGRES_PORT=5432, and the production compose file published that bare port on
every interface; the installers wrote 127.0.0.1:<port> into the same variables. Both compose files
now read POSTGRES_BIND/POSTGRES_HOST_PORT and REDIS_BIND/REDIS_HOST_PORT, default to 127.0.0.1, and
ignore the old variables, so neither kind of existing env file can widen the binding.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml")
STORES = {"postgres": 5432, "redis": 6379}
_VAR = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")

# Env files that exist in the field, beside the new template.
LEGACY = {
    "old template": {"POSTGRES_PORT": "5432", "REDIS_PORT": "6381"},
    "installer and guide": {"POSTGRES_PORT": "127.0.0.1:5432", "REDIS_PORT": "127.0.0.1:6381"},
    "exposed on purpose": {"POSTGRES_PORT": "0.0.0.0:5432", "REDIS_PORT": "0.0.0.0:6381"},
}


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


ENVS = {"template": _template(), "unset": {}, **LEGACY}


@pytest.mark.parametrize("compose", COMPOSE_FILES)
@pytest.mark.parametrize("service", sorted(STORES))
@pytest.mark.parametrize("source", sorted(ENVS))
def test_the_store_is_published_on_loopback_only(compose, service, source):
    bindings = list(_bindings(compose, service, ENVS[source]))
    assert bindings, f"{compose} publishes no port for {service}"
    for host_ip, published, container in bindings:
        assert host_ip == "127.0.0.1", f"{compose} {service} with the {source} env binds {host_ip or 'every interface'}"
        assert published.isdigit() and int(container) == STORES[service]


def test_the_template_names_the_loopback_bind():
    template = _template()
    assert template["POSTGRES_BIND"] == template["REDIS_BIND"] == "127.0.0.1"
    assert "POSTGRES_PORT" not in template and "REDIS_PORT" not in template


def test_an_explicit_bind_and_port_are_honoured():
    env = {"POSTGRES_BIND": "10.0.0.5", "POSTGRES_HOST_PORT": "15432"}
    (binding,) = list(_bindings("docker-compose.prod.yml", "postgres", env))
    assert binding == ["10.0.0.5", "15432", "5432"]


def test_every_production_service_keeps_bounded_json_file_logs():
    """Fluent Bit tails json-file logs; rotation keeps them and the disk bounded on every host."""
    document = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
    for name, service in document["services"].items():
        logging = service.get("logging") or {}
        assert logging.get("driver") == "json-file", name
        assert logging.get("options", {}).get("max-size") and logging.get("options", {}).get("max-file"), name
