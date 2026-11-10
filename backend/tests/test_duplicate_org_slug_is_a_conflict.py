"""A slug already taken must answer 409, not 500 with a database traceback.

``provision_tenant_access`` resolves an organisation by ``(source, external_id,
api_key_id)`` and auto-creates one when that misses. ``Organization.slug`` carries its own
unique index, ``ix_organizations_slug``, so a slug already held under a different
``external_id`` misses the lookup and violates the constraint on INSERT. The
``IntegrityError`` must be turned into a 409 rather than escape the request.

The tests drive the handler and assert the status the caller receives. The fake session
raises on ``flush`` as asyncpg does through SQLAlchemy, which is all the guard can react to.
"""

import asyncio
import unittest
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.routers import integrations
from app.routers.integrations import provision_tenant_access, revoke_tenant_access
from app.services.lxd import registry as cluster_registry


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


class _ExistingOrg:
    def __init__(self, slug: str, api_key_id: Optional[str]) -> None:
        self.slug = slug
        self.api_key_id = api_key_id
        self.id = 1


class _OwnedElsewhereSession(_SlugTakenSession):
    """First lookup (by external_id + api_key_id) misses; the ownership lookup finds a row."""

    def __init__(self, owner: _ExistingOrg) -> None:
        super().__init__()
        self._owner = owner
        self._calls = 0

    async def execute(self, *_args: Any, **_kwargs: Any):
        self._calls += 1
        if self._calls == 2:
            return _Found(self._owner)
        return _NoResult()


class _Found:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any:
        return self._obj


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


class _RegistryKnowsNuc2(unittest.TestCase):
    """The writer refuses a cluster no registration knows before it touches the session; the
    cases below are about what happens after that gate."""

    def setUp(self) -> None:
        async def _registered():
            return frozenset({"nuc2"})

        async def _nobody_else_names_it(*_a, **_k) -> None:
            return None

        self._registry = cluster_registry.registered_cluster_ids
        cluster_registry.registered_cluster_ids = _registered
        # The sessions below answer by call count, so the project gate's own query would shift
        # them. Which party names a project is pinned in test_a_key_grant_stays_inside_the_bound.py.
        self._project_gate = integrations.refuse_a_project_another_party_holds
        integrations.refuse_a_project_another_party_holds = _nobody_else_names_it

    def tearDown(self) -> None:
        cluster_registry.registered_cluster_ids = self._registry
        integrations.refuse_a_project_another_party_holds = self._project_gate


class DuplicateOrgSlugTests(_RegistryKnowsNuc2):
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



class ForeignKeyOwnershipTests(_RegistryKnowsNuc2):
    """The refusal must name the real reason: another integration owns the organisation.

    `provision_tenant_access` folds `api_key_id` into its lookup, so a caller whose key does
    not own an existing org misses it and falls through to the create, where the database
    refuses on `uq_org_source_external_id` or `ix_organizations_slug` and the caller would be
    told a slug is taken. The ownership guard denies nothing the constraints do not already
    deny; it replaces a misleading conflict with an accurate one, as sync does.
    """

    def test_an_org_owned_by_another_key_is_refused_by_name(self):
        owner = _ExistingOrg(slug="suka-hati", api_key_id="oak_someone_else")
        db = _OwnedElsewhereSession(owner)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(provision_tenant_access(body=_Body(), api_key=_api_key(), db=db))

        detail = str(caught.exception.detail)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("suka-hati", detail)
        self.assertIn("different integration key", detail)
        self.assertEqual(
            db.added, [], "nothing may be staged once the organisation is known to be owned"
        )

    def test_an_unclaimed_org_still_provisions(self):
        """A row no key has claimed must not be refused, which is sync's rule too."""
        owner = _ExistingOrg(slug="suka-hati", api_key_id=None)
        db = _OwnedElsewhereSession(owner)

        # Reaches the create, so the fake's flush raises the duplicate-slug IntegrityError
        # and the conflict path answers rather than the ownership one.
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(provision_tenant_access(body=_Body(), api_key=_api_key(), db=db))

        self.assertNotIn("different integration key", str(caught.exception.detail))



class RevokeOwnershipTests(unittest.TestCase):
    """Revoke must agree with provision, and keep its idempotency.

    `revoke_tenant_access` folds `api_key_id` into the same lookup, so a miss has two
    causes. No such organisation is an idempotent success. An organisation owned by another
    key is a 409, as in provision; an empty 200 would report a revoke that touched nothing.
    """

    def test_an_org_owned_by_another_key_is_refused(self):
        owner = _ExistingOrg(slug="suka-hati", api_key_id="oak_someone_else")
        db = _OwnedElsewhereSession(owner)

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(revoke_tenant_access(body=_Body(), api_key=_api_key(), db=db))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("suka-hati", str(caught.exception.detail))

    def test_no_such_org_is_still_an_idempotent_success(self):
        """Revoking an organisation that does not exist stays a success."""
        db = _SlugTakenSession()  # every lookup misses, so no owner is found either

        result = asyncio.run(revoke_tenant_access(body=_Body(), api_key=_api_key(), db=db))

        self.assertIsNotNone(result, "an absent organisation must stay an empty success")
        self.assertEqual(db.added, [], "revoke must not create anything")


if __name__ == "__main__":
    unittest.main()
