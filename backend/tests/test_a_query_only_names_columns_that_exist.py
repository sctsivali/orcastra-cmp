"""A query that names a column the model does not have is a 500 that faked-session tests miss.

Service tests fake the session, so a statement naming a missing attribute (ex; a nonexistent
`OrganizationCluster.cluster_name`) is never built against the real model and every test passes.
SQLAlchemy raises at statement-construction time, not at execution, so the attribute reference is
enough to catch it without a database. This walks the listed modules for `Model.attribute` and
asserts each one resolves.
"""

import ast
import importlib
import inspect
import os
from pathlib import Path
from typing import Dict, List, Set

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")

import pytest  # noqa: E402

from app.models import db_models, extension_models, snapshot_schedule_models, task_journal_models  # noqa: E402

# Modules that build queries against the ORM. A new one belongs here.
MODULES = [
    "app.services.extension_install",
    "app.services.extension_service",
    "app.services.extension_scope",
    "app.services.extension_teardown",
    "app.services.extension_reconcile",
    # extension_credentials and extension_settings work against Vault, not the ORM. They are
    # listed anyway: the coverage check below requires every extension_* service here, so an
    # exemption would leave a gap.
    "app.services.extension_credential_guard",
    "app.services.extension_credentials",
    "app.services.extension_settings",
    "app.routers.extensions",
    "app.routers.extension_installs",
    "app.routers.extension_webhooks",
    "app.routers.extensions_external",
    # The task journal: ORM selects over its own tables and the schedules.
    "app.services.task_journal.reads",
    "app.services.task_journal.repository",
    "app.services.task_journal.repository_sql",
    "app.services.task_journal.repository_sweeps",
    "app.services.task_journal.repository_log",
    "app.routers.tasks",
    "app.routers.cluster_log",
]


def _model_classes() -> Dict[str, type]:
    classes: Dict[str, type] = {}
    for module in (db_models, extension_models, snapshot_schedule_models, task_journal_models):
        for name in dir(module):
            candidate = getattr(module, name)
            if isinstance(candidate, type) and hasattr(candidate, "__tablename__"):
                classes[name] = candidate
    return classes


MODELS = _model_classes()


def _referenced_attributes(source: str) -> Set[tuple]:
    """(ClassName, attribute) for every `SomeModel.attr` in the source."""
    found: Set[tuple] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id in MODELS:
            found.add((value.id, node.attr))
    return found


def test_the_model_inventory_is_not_empty() -> None:
    """A guard on the guard: an empty inventory would make every assertion below vacuous."""
    assert len(MODELS) > 10, f"only found {len(MODELS)} models, so this check sees almost nothing"
    assert "OrganizationCluster" in MODELS
    assert "ExtensionInstallation" in MODELS
    assert "TaskJournal" in MODELS


@pytest.mark.parametrize("module_name", MODULES)
def test_every_referenced_column_exists(module_name: str) -> None:
    module = importlib.import_module(module_name)
    missing: List[str] = []

    for class_name, attribute in sorted(_referenced_attributes(inspect.getsource(module))):
        if not hasattr(MODELS[class_name], attribute):
            missing.append(f"{class_name}.{attribute}")

    assert not missing, (
        f"{module_name} names attributes these models do not have: {missing}. "
        "SQLAlchemy raises when the statement is built, so this is a 500 rather than a bad query."
    )


def test_the_module_list_covers_the_extension_services() -> None:
    """A new service that queries the ORM has to be added here, or it is unchecked."""
    services = Path(__file__).resolve().parent.parent / "app" / "services"
    on_disk = {
        f"app.services.{p.stem}"
        for p in services.glob("extension_*.py")
    }
    uncovered = sorted(on_disk - set(MODULES))
    assert not uncovered, f"these extension services are not covered by this check: {uncovered}"
