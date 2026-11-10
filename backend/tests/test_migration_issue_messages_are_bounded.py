"""A preflight issue fits the API's 2000-character field however many instances it names.

A message joining every instance name of a large project would exceed the response model, so
the plan row commits and every later read of it answers 500. A revised plan must also store the
same report the approval hash covers, or every execute asks for re-approval.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.models.migration_schemas import MigrationIssue
from app.services.migrations import repository
from app.services.migrations.preflight import PreflightResult, name_list


def test_a_long_name_list_names_the_first_few_and_counts_the_rest() -> None:
    names = [f"web-{index:03d}-" + "x" * 50 for index in range(150)]

    text = name_list(names)

    assert text.startswith("web-000-")
    assert text.endswith("and 140 more")
    assert len(text) < 1000


def test_a_short_name_list_is_unchanged() -> None:
    assert name_list(["a", "b"]) == "a, b"


@pytest.mark.parametrize("record", ["block", "warn"])
def test_every_recorded_issue_fits_the_response_model(record: str) -> None:
    result = PreflightResult(strategy="cross_cluster_pull", downtime="cold")

    getattr(result, record)("source_footprint_bounded_by_pool", "m" * 5000, "r" * 5000)

    issue = (result.blockers or result.warnings)[0]
    # The presenter adds the severity; message and remediation are what the model bounds.
    MigrationIssue.model_validate({**issue, "severity": "warning"})
    assert issue["message"].endswith("…")


class _Db:
    async def commit(self) -> None:
        return None

    async def refresh(self, _row: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_a_revised_plan_stores_the_report_the_approval_hash_covers() -> None:
    report = {
        "blockers": [],
        "warnings": [],
        "resources": [{"profiles": ["organization-base.webserver-tier.production-env1"]}],
    }
    row = SimpleNamespace(preflight_payload=None, status="ready", revision=1, expires_at=None, updated_at=None)

    await repository.revise_plan_preflight(_Db(), row, report=report)

    assert row.preflight_payload == report
