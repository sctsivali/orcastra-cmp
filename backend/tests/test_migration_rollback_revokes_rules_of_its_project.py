"""A rollback that deletes the target project its job created also removes the rules naming it.

Instance moves may run on a shared cluster. Rules added for the new project during retention
outlived it, so another organization's later project of the same name inherited them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.migrations import retention_store


class _Db:
    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.mark.parametrize("rule_ids", [[11, 12], []])
@pytest.mark.asyncio
async def test_rules_go_only_with_the_project(monkeypatch: pytest.MonkeyPatch, rule_ids: list[int]) -> None:
    revoked: list[tuple[str, str, Any]] = []

    async def fenced(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def events(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def revoke(_db: Any, *, cluster_id: str, project_name: str, commit: bool, only_rule_ids: Any) -> None:
        assert commit is False, "the revoke belongs to the rollback's transaction"
        revoked.append((cluster_id, project_name, list(only_rule_ids)))

    monkeypatch.setattr(retention_store, "_fenced_update", fenced)
    monkeypatch.setattr(retention_store, "_events", events)
    monkeypatch.setattr(retention_store, "revoke_project_grants", revoke)
    job = SimpleNamespace(job_id="job-1", fence_token=1, target_cluster_id="shared", target_project="new-web",
                          source_organization_id=7)

    await retention_store.finish_rollback(
        _Db(), job, worker_id="w", event_actor={}, source_uuid=None, phase="rolled_back", status="success",
        message="done", revert_metadata=False, outbox_events=[], revoke_rule_ids=rule_ids,
    )

    # Only the rules recorded when the project was deleted: a rule written later for a new
    # project of the same name survives a rollback that resumes after it.
    assert revoked == ([("shared", "new-web", rule_ids)] if rule_ids else [])


@pytest.mark.asyncio
async def test_a_project_rollback_moves_schedules_back_with_the_source_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.migrations import project_rollback

    captured: dict[str, Any] = {}
    resource = SimpleNamespace(resource_id="r-web", resource_type="instance", ordinal=0,
                               source_ref={"name": "web"}, target_ref={"name": "web"}, safe_result={})

    class _Session:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    async def finish(_db: Any, _job: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(project_rollback, "async_session_maker", lambda: _Session())
    monkeypatch.setattr(project_rollback, "finish_project_rollback", finish)
    monkeypatch.setattr(project_rollback, "_resource_update", noop)
    monkeypatch.setattr(project_rollback, "project_instance_events", lambda *_a, **_k: [])
    job = SimpleNamespace(job_id="job-1", source_cluster_id="a", source_project="web", target_cluster_id="b",
                          target_project="web", source_cluster_fingerprint="fa", target_cluster_fingerprint="fb",
                          safe_result={}, requester_role="partner", actor_issuer="i", actor_subject="s",
                          actor_email="ops@example.test", actor_provider_uuid="u")

    await project_rollback._finish(job, "w", [resource], rollback=True, project_deleted=True,
                                   source_uuids={"web": "uuid-of-the-source"})

    source, _target = captured["pairs"][0]
    assert source.uuid == "uuid-of-the-source"
