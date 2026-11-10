"""An API key granting nothing is refused with a 403 rather than crashing the request.

``max()`` over an empty sequence raises ValueError, and ``grant.get("permissions", ["read"])``
returns its default only when the key is ABSENT, so a naive level check over a grant stored as
``"permissions": []`` surfaces as HTTP 500. The raw proxy and the image and ISO upload routers
share one helper for the check (app/core/api_key_permissions.py).

The level of an empty grant is -1: 0 is the level of "read", so collapsing an empty grant to
zero would hand out read access nobody granted.
"""

import unittest

from fastapi import HTTPException

from app.core.api_key_permissions import (
    PERMISSION_LEVELS,
    assert_cluster_permission,
    granted_level,
)

CLUSTER = "nuc2"


def _key(permissions):
    return {"cluster_permissions": [{"cluster_id": CLUSTER, "permissions": permissions}]}


class AnEmptyGrantGrantsNothing(unittest.TestCase):
    def test_an_empty_grant_does_not_reach_the_level_of_read(self):
        self.assertLess(
            granted_level([]), PERMISSION_LEVELS["read"],
            "an empty grant scoring at or above read would hand out access nobody granted",
        )

    def test_an_empty_grant_is_refused_for_every_level(self):
        for required in ("read", "write", "admin"):
            with self.subTest(required=required):
                with self.assertRaises(HTTPException) as caught:
                    assert_cluster_permission(_key([]), CLUSTER, required)
                self.assertEqual(caught.exception.status_code, 403)

    def test_a_missing_permissions_key_still_defaults_to_read(self):
        """Only an absent key takes the read default."""
        grant = assert_cluster_permission(
            {"cluster_permissions": [{"cluster_id": CLUSTER}]}, CLUSTER, "read"
        )
        self.assertEqual(grant["cluster_id"], CLUSTER)

        with self.assertRaises(HTTPException) as caught:
            assert_cluster_permission(
                {"cluster_permissions": [{"cluster_id": CLUSTER}]}, CLUSTER, "write"
            )
        self.assertEqual(caught.exception.status_code, 403)

    def test_the_old_expression_is_what_crashed(self):
        """Pins the ``max()`` failure the module docstring describes."""
        hierarchy = dict(PERMISSION_LEVELS)
        with self.assertRaises(ValueError):
            max(hierarchy.get(p, 0) for p in [])


class RealGrantsStillWork(unittest.TestCase):
    def test_the_strongest_level_in_a_grant_wins(self):
        self.assertEqual(granted_level(["read", "admin", "write"]), PERMISSION_LEVELS["admin"])

    def test_write_satisfies_read_but_not_admin(self):
        assert_cluster_permission(_key(["write"]), CLUSTER, "read")
        assert_cluster_permission(_key(["write"]), CLUSTER, "write")
        with self.assertRaises(HTTPException) as caught:
            assert_cluster_permission(_key(["write"]), CLUSTER, "admin")
        self.assertEqual(caught.exception.status_code, 403)

    def test_an_unknown_permission_name_does_not_grant_admin(self):
        with self.assertRaises(HTTPException):
            assert_cluster_permission(_key(["superuser"]), CLUSTER, "write")

    def test_a_cluster_with_no_grant_at_all_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            assert_cluster_permission(_key(["admin"]), "some-other-cluster", "read")
        self.assertEqual(caught.exception.status_code, 403)

    def test_a_malformed_grant_entry_is_skipped_not_crashed(self):
        malformed = {"cluster_permissions": [None, "nonsense", {"no_cluster_id": True}]}
        with self.assertRaises(HTTPException) as caught:
            assert_cluster_permission(malformed, CLUSTER, "read")
        self.assertEqual(caught.exception.status_code, 403)


class TheBoundaryRefusesTheMistake(unittest.TestCase):
    """The runtime 403 handles stored data; this catches the mistake where it is made.

    The validators sit on the request model, ClusterPermissionRequest. Stored keys are read back
    through ClusterPermission (`_key_info` builds `APIKeyInfo(**metadata)` from Vault JSON),
    so a validator there would let one legacy grant 500 the entire key list in `list_api_keys`.
    """

    def _model(self):
        from app.routers.integrations import ClusterPermissionRequest
        return ClusterPermissionRequest

    def test_an_empty_grant_is_rejected(self):
        with self.assertRaises(Exception) as caught:
            self._model()(cluster_id=CLUSTER, permissions=[])
        self.assertIn("at least one", str(caught.exception))

    def test_an_unknown_level_is_rejected_and_named(self):
        with self.assertRaises(Exception) as caught:
            self._model()(cluster_id=CLUSTER, permissions=["read", "superuser"])
        message = str(caught.exception)
        self.assertIn("superuser", message)
        self.assertNotIn("read,", message.split("unknown permission")[-1].split(".")[0])

    def test_the_real_levels_are_accepted(self):
        for grant in (["read"], ["write"], ["admin"], ["read", "write", "admin"]):
            with self.subTest(grant=grant):
                self.assertEqual(self._model()(cluster_id=CLUSTER, permissions=grant).permissions, grant)

    def test_omitting_the_field_still_defaults_to_read(self):
        self.assertEqual(self._model()(cluster_id=CLUSTER).permissions, ["read"])

    def test_a_stored_grant_with_an_unknown_name_still_authorises(self):
        """The validator is boundary-only; the runtime path never sees it."""
        assert_cluster_permission(_key(["read", "legacy-name"]), CLUSTER, "read")

    def test_the_read_model_still_accepts_a_grant_that_grants_nothing(self):
        """Every stored grant is read back through ClusterPermission, which stays lenient.

        If it refused an empty or unfamiliar grant, a single legacy key would take the whole key
        list down with a 500 rather than showing itself as the broken one.
        """
        from app.routers.integrations import ClusterPermission

        self.assertEqual(ClusterPermission(cluster_id=CLUSTER, permissions=[]).permissions, [])
        self.assertEqual(
            ClusterPermission(cluster_id=CLUSTER, permissions=["owner"]).permissions, ["owner"]
        )


if __name__ == "__main__":
    unittest.main()
