from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("compose_name", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_compose_exposes_the_entrypoint_migration_switch(compose_name: str) -> None:
    compose = (ROOT / compose_name).read_text()
    assert "RUN_MIGRATIONS=${RUN_MIGRATIONS:-true}" in compose


def test_backend_entrypoint_defaults_migrations_on_and_allows_an_explicit_skip() -> None:
    entrypoint = (ROOT / "backend" / "docker-entrypoint.sh").read_text()
    assert '${RUN_MIGRATIONS:-true}' in entrypoint
    assert '!= "false"' in entrypoint
