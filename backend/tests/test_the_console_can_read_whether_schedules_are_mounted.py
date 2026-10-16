"""The console learns a deployment's capability instead of discovering it through a 404.

`SNAPSHOT_SCHEDULES_ENABLED` is off by default and gates both the scheduler loop and the router
mount (`main.py:335` and `main.py:689`). Nothing told the console that, so on a stock deployment
the Snapshot Schedules card printed "No schedules yet" for a deployment that has no schedule
surface at all, and the instance tab's Schedule button was enabled and submitted into a 404.

`/system/settings` is already polled every 60 seconds for `read_only_mode`, so reporting the flag
there costs no request. These tests pin the two properties that make it useful: the field is part
of the response contract, and its value is read from the setting rather than fixed.

Hermetic: settings are stubbed in place, so no environment, no database and no network.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.routers import system  # noqa: E402
from app.routers.system import SystemSettings  # noqa: E402


def _read(monkeypatch, *, enabled: bool) -> SystemSettings:
    monkeypatch.setattr(
        system,
        "get_settings",
        lambda: SimpleNamespace(
            read_only_mode=False,
            api_version="1.0",
            debug=False,
            snapshot_schedules_enabled=enabled,
            task_journal_enabled=True,
        ),
    )
    return asyncio.run(system.get_system_settings())


def test_the_field_is_part_of_the_response_contract() -> None:
    """The console gates a control on it, so it must not become optional by omission."""
    assert "snapshot_schedules_enabled" in SystemSettings.model_fields


def test_migration_rollout_capabilities_are_explicit_with_instance_stages_on() -> None:
    fields = SystemSettings.model_fields
    for name in (
        "migration_center_enabled",
        "migration_durable_copy_enabled",
        "migration_instance_move_enabled",
        "migration_project_member_relocation_enabled",
        "migration_project_cross_cluster_enabled",
        "migration_recovery_actions_enabled",
        "migration_auto_cleanup_enabled",
        "migration_running_source_enabled",
    ):
        assert name in fields

    settings = Settings()
    # Instance copy and move are released and on by default. Each flag stays a kill switch.
    assert settings.migration_center_enabled is True
    assert settings.migration_durable_copy_enabled is True
    assert settings.migration_instance_move_enabled is True
    assert settings.migration_recovery_actions_enabled is True
    assert settings.migration_auto_cleanup_enabled is True
    assert settings.migration_running_source_enabled is True
    # Project relocation is released too, behind its own master gate and two stage flags.
    assert settings.project_relocation_enabled is True
    assert settings.migration_project_member_relocation_enabled is True
    assert settings.migration_project_cross_cluster_enabled is True
    assert settings.migration_project_max_instances == 25
    # OrcaHub-provisioned projects keep their identity until OrcaHub accepts a move event.
    assert settings.migration_orcahub_identity_change_enabled is False


def test_a_deployment_with_the_feature_off_says_so(monkeypatch) -> None:
    assert _read(monkeypatch, enabled=False).snapshot_schedules_enabled is False


def test_a_deployment_with_the_feature_on_says_so(monkeypatch) -> None:
    """Both directions, because a hard-coded literal passes whichever one is tested alone."""
    assert _read(monkeypatch, enabled=True).snapshot_schedules_enabled is True


def test_the_reported_flag_is_the_one_that_gates_the_router() -> None:
    """Reporting a second, differently-named flag would be a control that lies convincingly.

    `main.py` mounts `snapshot_schedules_router` under `settings.snapshot_schedules_enabled`, and
    the environment variable that sets it is the one the deployment docs name.
    """
    assert Settings(SNAPSHOT_SCHEDULES_ENABLED=True).snapshot_schedules_enabled is True
    assert Settings(SNAPSHOT_SCHEDULES_ENABLED=False).snapshot_schedules_enabled is False
    assert Settings().snapshot_schedules_enabled is False

    mounted = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert "if settings.snapshot_schedules_enabled:\n        from app.routers.snapshot_schedules import router" in mounted
