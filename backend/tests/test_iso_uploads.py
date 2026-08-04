"""Unit tests for the pure logic in app.routers.iso_uploads.

Covers the volume-name safety gate, the cluster-permission check, the LXD project qualifier and
the status payload shape. The tus/import flow (DB + LXD) is exercised separately against a live
cluster; these tests need no DB or network.
"""

import types
import unittest

from fastapi import HTTPException

from app.routers.iso_uploads import (
    _VOLUME_NAME_RE,
    _assert_cluster_permission,
    _volume_project_q,
    _staging_path,
    _status_payload,
)


class VolumeNameValidationTests(unittest.TestCase):
    def test_accepts_reasonable_names(self) -> None:
        for name in ("ubuntu-24.04.iso", "win2022", "A.b_c-1", "x"):
            self.assertIsNotNone(_VOLUME_NAME_RE.match(name), name)

    def test_rejects_traversal_and_separators(self) -> None:
        for name in ("../etc", "a/b", "a b", ".hidden", "-lead", "", "a" * 64):
            self.assertIsNone(_VOLUME_NAME_RE.match(name), name)


class ClusterPermissionTests(unittest.TestCase):
    def _key(self, cluster_id: str, perms: list[str]) -> dict:
        return {"cluster_permissions": [{"cluster_id": cluster_id, "permissions": perms}]}

    def test_allows_when_level_sufficient(self) -> None:
        # write required, key has write -> no raise; admin key also satisfies write.
        _assert_cluster_permission(self._key("c1", ["write"]), "c1", "write")
        _assert_cluster_permission(self._key("c1", ["admin"]), "c1", "write")
        _assert_cluster_permission(self._key("c1", ["read"]), "c1", "read")

    def test_denies_unknown_cluster(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _assert_cluster_permission(self._key("c1", ["admin"]), "other", "read")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_denies_insufficient_level(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _assert_cluster_permission(self._key("c1", ["read"]), "c1", "write")
        self.assertEqual(ctx.exception.status_code, 403)


class HelperTests(unittest.TestCase):
    def test_project_qualifier(self) -> None:
        self.assertEqual(_volume_project_q("default"), "")
        self.assertEqual(_volume_project_q(""), "")
        self.assertEqual(_volume_project_q("acme"), "?project=acme")

    def test_staging_path_contains_session_and_ext(self) -> None:
        p = _staging_path("deadbeef")
        self.assertTrue(p.endswith("deadbeef.iso"))

    def test_status_payload_shape(self) -> None:
        row = types.SimpleNamespace(
            session_id="s1", status="ready", upload_offset=10, total_bytes=10,
            pool="default", volume_name="ubuntu.iso", size=10, error=None,
        )
        payload = _status_payload(row)
        self.assertEqual(
            set(payload.keys()),
            {"session_id", "status", "offset", "total_bytes", "pool", "volume_name", "size", "error"},
        )
        self.assertEqual(payload["offset"], 10)
        self.assertEqual(payload["volume_name"], "ubuntu.iso")


if __name__ == "__main__":
    unittest.main()
