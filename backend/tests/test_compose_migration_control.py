from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("compose_name", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_compose_exposes_the_entrypoint_migration_switch(compose_name: str) -> None:
    compose = (ROOT / compose_name).read_text()
    assert "RUN_MIGRATIONS=${RUN_MIGRATIONS:-true}" in compose


@pytest.mark.parametrize("compose_name", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_compose_does_not_inherit_an_ambient_debug_value(compose_name: str) -> None:
    compose = (ROOT / compose_name).read_text()
    backend = yaml.safe_load(compose)["services"]["backend"]
    environment = backend["environment"]
    environment_keys = (
        {entry.split("=", 1)[0] for entry in environment}
        if isinstance(environment, list)
        else set(environment)
    )

    assert ".env" in backend["env_file"]
    assert "DEBUG" not in environment_keys
    assert "DEBUG intentionally comes only from env_file" in compose


def test_backend_entrypoint_defaults_migrations_on_and_allows_an_explicit_skip() -> None:
    entrypoint = (ROOT / "backend" / "docker-entrypoint.sh").read_text()
    assert '${RUN_MIGRATIONS:-true}' in entrypoint
    assert '!= "false"' in entrypoint
