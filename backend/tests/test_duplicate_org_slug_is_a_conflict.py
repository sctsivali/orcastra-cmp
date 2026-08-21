"""A slug already taken must answer 409, not 500 with a database traceback.

``provision_tenant_access`` resolves an organisation by ``(source, external_id,
api_key_id)`` and auto-creates one when that misses. ``Organization.slug`` carries its own
unique index, ``ix_organizations_slug``, so a slug already held under a different
``external_id`` misses the lookup and violates the constraint on INSERT. The
``IntegrityError`` was unhandled and escaped the request.

Observed live on the rebuilt stack at 2026-08-21T00:45:02, during a QA harness run::

    POST /api/v1/integrations/provision-tenant-access -> 500
    asyncpg.exceptions.UniqueViolationError: duplicate key value violates
    unique constraint "ix_organizations_slug"

Behavioural rather than structural. An earlier ratchet in this suite was written as a
substring scan and passed while the endpoint stayed broken, so this drives the handler and
asserts the status the caller receives. The fake session raises on ``flush`` exactly as
asyncpg does through SQLAlchemy, which is the only thing the guard can react to.
"""

import asyncio
import unittest
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.routers.integrations import provision_tenant_access


class _NoResult:
    """Mimics the Result object for a lookup that found nothing."""

    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self):
        return self

    def all(self) -> List[Any]:
        return []

    def first(self) -> None:
        return None


class _SlugTakenSession:
    """An AsyncSession whose flush fails the way a duplicate slug does."""

    def __init__(self) -> None:
        self.rolled_back = False
        self.added: List[Any] = []

    async def execute(self, *_args: Any, **_kwargs: Any) -> _NoResult:
        # Every lookup misses, which is what drives the code into auto-creation.
        return _NoResult()

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        raise IntegrityError(
            'INSERT INTO organizations',
            {},
            Exception('duplicate key value violates unique constraint "ix_organizations_slug"'),
        )

    async def rollback(self) -> None:
        self.rolled_back = True

    async def commit(self) -> None:  # pragma: no cover - never reached in this path
        raise AssertionError("commit must not run after the insert failed")


class _Body:
    cluster_id = "nuc2"
    project_name = "default"
    organization_external_id = "11111111-2222-3333-4444-555555555555"
    organization_name = "Acme"
    organization_slug = "acme"
    members: Optional[List[Any]] = None
    policy_name: Optional[str] = None


def _api_key() -> Dict[str, Any]:
    return {
        "id": "key-1",
        "created_by": "ops@example.com",
        "cluster_permissions": [{"cluster_id": "nuc2", "projects": ["*"]}],
    }


class DuplicateOrgSlugTests(unittest.TestCase):
    def test_a_taken_slug_answers_409_and_rolls_back(self):
        db = _SlugTakenSession()

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(provision_tenant_access(body=_Body(), api_key=_api_key(), db=db))

        status = caught.exception.status_code
        self.assertEqual(
            status,
            409,
            f"a slug already held by another organisation answered {status}. Unhandled, this "
            "leaves the caller a 500 carrying an asyncpg traceback for what is an ordinary "
            "conflict.",
        )
        self.assertIn("acme", str(caught.exception.detail))
        self.assertTrue(
            db.rolled_back,
            "the failed INSERT must be rolled back, or the session stays poisoned for "
            "whatever the dependency does on teardown",
        )

    def test_the_cluster_allow_list_still_runs_first(self):
        """The conflict guard must not be reachable before authorisation.

        Ordering matters here: the endpoint binds a caller-supplied cluster to an org, so a
        key without permission for that cluster has to be refused before any org row is
        considered, let alone created.
        """
        db = _SlugTakenSession()
        key = _api_key()
        key["cluster_permissions"] = [{"cluster_id": "someone-elses", "projects": ["*"]}]

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(provision_tenant_access(body=_Body(), api_key=key, db=db))

        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(db.added, [], "nothing may be staged before the allow-list passes")


if __name__ == "__main__":
    unittest.main()
