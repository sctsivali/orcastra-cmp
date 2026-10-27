"""The console toolbar's actions leave an audit record (routers/console_actions.py).

The keys go to the guest over SPICE, which the backend relays without reading, so the browser
reports each toolbar action afterwards. Only a caller who may use this instance's console can
record against it, a body that is not a toolbar action is refused, and a caller cannot flood the
trail.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.auth import AuthenticatedUser
from app.routers import console_actions as route

TENANT = AuthenticatedUser(user_id="tenant-1", email="tenant@buyer.test", groups=["role_tenant"])


@pytest.fixture
def world(monkeypatch):
    events, checks = [], []

    class _Logger:
        def log_activity(self, **kwargs):
            events.append(kwargs)

    def verify(host, project, instance, permission):
        checks.append((host, project, instance, permission))
        if instance == "not-mine":
            raise HTTPException(status_code=404, detail="Instance not found")
        return project

    monkeypatch.setattr(route, "get_activity_logger", lambda: _Logger())
    monkeypatch.setattr(route, "_verify_instance_access", verify)
    monkeypatch.setattr(route, "_shared_allow", lambda *_args: None)
    route._local.clear()
    return events, checks


@pytest.mark.parametrize("combo", sorted(route.COMBO_IDS) + ["ctrl+alt+f2", "Win+R", "ctrl+shift+escape", "delete"])
def test_toolbar_combinations_and_custom_ones_are_accepted(combo) -> None:
    body = route.ConsoleActionBody(host="nuc2", project="qa", action="keys.send", combo=combo)
    assert body.combo == combo.lower()


@pytest.mark.parametrize("combo", [None, "", "rm -rf /", "ctrl+ctrl+a", "alt+ctrl+a", "win+meta", "a" * 70, "ctrl+alt+f13"])
def test_anything_else_is_refused(combo) -> None:
    with pytest.raises(ValidationError):
        route.ConsoleActionBody(host="nuc2", project="qa", action="keys.send", combo=combo)


def test_a_screenshot_carries_no_combination() -> None:
    with pytest.raises(ValidationError):
        route.ConsoleActionBody(host="nuc2", action="screenshot.take", combo="win")
    assert route.ConsoleActionBody(host="nuc2", action="screenshot.take").combo is None


async def test_a_sent_combination_is_recorded_against_the_instance(world) -> None:
    events, checks = world
    body = route.ConsoleActionBody(host="nuc2", project="qa", action="keys.send", combo="ctrl-alt-del")
    await route.record_console_action(body, instance_name="qa-vm", user=TENANT)

    assert checks == [("nuc2", "qa", "qa-vm", "console")]
    assert events == [{
        "action": "console.keys.send", "target_type": "instance", "target_id": "qa-vm",
        "target_host": "nuc2", "target_project": "qa", "details": {"combo": "ctrl-alt-del"},
    }]


async def test_a_screenshot_is_recorded(world) -> None:
    events, _checks = world
    await route.record_console_action(
        route.ConsoleActionBody(host="nuc2", project="qa", action="screenshot.take"), instance_name="qa-vm", user=TENANT,
    )
    assert events[0]["action"] == "console.screenshot.take" and events[0]["details"] is None


async def test_an_instance_the_caller_cannot_use_is_refused_and_not_recorded(world) -> None:
    events, _checks = world
    with pytest.raises(HTTPException) as refused:
        await route.record_console_action(
            route.ConsoleActionBody(host="nuc2", project="qa", action="keys.send", combo="win"),
            instance_name="not-mine", user=TENANT,
        )
    assert refused.value.status_code == 404
    assert events == []


async def test_a_caller_cannot_flood_the_trail(world) -> None:
    events, _checks = world
    body = route.ConsoleActionBody(host="nuc2", project="qa", action="keys.send", combo="win")
    for _ in range(route.PER_MINUTE):
        await route.record_console_action(body, instance_name="qa-vm", user=TENANT)
    with pytest.raises(HTTPException) as refused:
        await route.record_console_action(body, instance_name="qa-vm", user=TENANT)
    assert refused.value.status_code == 429 and refused.value.headers["Retry-After"] == "60"
    assert len(events) == route.PER_MINUTE


def test_the_actions_are_in_the_audit_catalogue() -> None:
    from app.services.activity_catalogue import ACTION_CATEGORIES

    assert "console.keys.send" in ACTION_CATEGORIES and "console.screenshot.take" in ACTION_CATEGORIES
