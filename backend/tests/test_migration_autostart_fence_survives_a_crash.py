"""The source autostart fence is keyed on its intent, like the NIC fence.

A crash, lease loss or failed readback between the boot.autostart patch and the write that
marks the source fenced left a source whose live autostart was already off while the journal
said nothing was fenced. The resumed job derived the contract from that live value, found it
changed, and stopped in manual recovery with the targets already running.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.migrations import activation
from app.services.migrations.source_fence import before_fences


def _source(autostart: str) -> dict[str, Any]:
    return {
        "name": "web",
        "config": {"boot.autostart": autostart},
        "expanded_config": {"boot.autostart": autostart},
    }


def test_the_approved_source_is_rebuilt_from_the_intent_alone() -> None:
    state = {"source_autostart_fence_intent": True, "original_autostart_local": "true"}

    restored = before_fences(_source("false"), state)

    assert restored["config"]["boot.autostart"] == "true"
    assert restored["expanded_config"]["boot.autostart"] == "true"


@pytest.mark.asyncio
async def test_a_patch_that_landed_before_the_crash_is_adopted_without_patching_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = SimpleNamespace(resource_id="r-web", source_ref={"name": "web"}, target_ref={"name": "web"})
    state = {"source_autostart_fence_intent": True, "original_autostart_local": "true"}
    transferred = [(resource, state, object())]
    updates: list[dict[str, Any]] = []

    async def read(*_args: Any) -> dict[str, Any]:
        return _source("false")

    async def resource_update(_resource_id: str, **values: Any) -> None:
        updates.append(values["safe_result"])

    async def must_not_patch(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("the fence was patched a second time")

    monkeypatch.setattr(activation, "_read", read)
    monkeypatch.setattr(activation, "_resource_update", resource_update)
    monkeypatch.setattr(activation, "_blocking", must_not_patch)

    await activation._fence_source_autostart(
        SimpleNamespace(source_project="src"), transferred, object()
    )

    assert transferred[0][1]["source_autostart_fenced"] is True
    assert transferred[0][1]["source_definition_hash"]
    assert updates and updates[-1]["source_autostart_fenced"] is True
