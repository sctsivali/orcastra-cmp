"""A query that names a column the model does not have is a 500 no unit test here can see.

`build_install_preview` selected `OrganizationCluster.cluster_name`, which does not exist. The
service tests fake the session, so the statement was never constructed against the real model and
every one of them passed. It surfaced as HTTP 500 on the one organization in the deployment that
had a cluster bound to it, during the first live run.

SQLAlchemy raises at statement-construction time, not at execution, so the attribute reference is
enough to catch it without a database. This walks the extension modules for `Model.attribute` and
asserts each one resolves, which is cheap and covers the whole class rather than the one instance.
"""

import ast
import importlib
import inspect
import os
from pathlib import Path
from typing import Dict, List, Set

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

import pytest  # noqa: E402

from app.models import db_models, extension_models  # noqa: E402

# Modules that build queries against the ORM. A new one belongs here.
MODULES = [
    "app.services.extension_install",
    "app.services.extension_service",
    "app.services.extension_scope",
    "app.services.extension_teardown",
    # These two work against Vault rather than the ORM today. Listed anyway, because the
    # coverage check below is what makes a new one impossible to forget and an exemption
    # here would be a hole rather than a saving.
    "app.services.extension_credentials",
    "app.services.extension_settings",
    "app.routers.extensions",
    "app.routers.extension_installs",
    "app.routers.extension_webhooks",
    "app.routers.extensions_external",
]


def _model_classes() -> Dict[str, type]:
    classes: Dict[str, type] = {}
    for module in (db_models, extension_models):
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
