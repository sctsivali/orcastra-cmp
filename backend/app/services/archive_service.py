"""
Archive Service for OpenSearch log compression & archive management.

Provides methods to:
- Query snapshot repository status
- List/manage snapshots
- Get ISM policy status for indices
- Trigger manual snapshots
- Restore indices from snapshots
- Get archive health overview
"""

import logging
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# HTTP client timeout (seconds)
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ArchiveServiceError(Exception):
    """Raised when an archive operation fails."""
    pass


class ArchiveService:
    """Manages OpenSearch log archive operations via the OpenSearch API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
        snapshot_repo: str = "orcastra-archive",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = (username, password)
        self._verify = verify_ssl
        self._repo = snapshot_repo
        self._client: Optional[httpx.AsyncClient] = None

    # -- HTTP helpers --

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                auth=self._auth,
                verify=self._verify,
                timeout=_TIMEOUT,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        json: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        client = await self._get_client()
        try:
            resp = await client.request(method, path, json=json, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            raise ArchiveServiceError(
                f"OpenSearch {method} {path} → {exc.response.status_code}: {body}"
            ) from exc
        except httpx.RequestError as exc:
            raise ArchiveServiceError(
                f"OpenSearch connection error: {exc}"
            ) from exc

    async def _get(self, path: str, **kw: Any) -> Dict[str, Any]:
        return await self._request("GET", path, **kw)

    async def _put(self, path: str, **kw: Any) -> Dict[str, Any]:
        return await self._request("PUT", path, **kw)

    async def _post(self, path: str, **kw: Any) -> Dict[str, Any]:
        return await self._request("POST", path, **kw)

    async def _delete(self, path: str, **kw: Any) -> Dict[str, Any]:
        return await self._request("DELETE", path, **kw)

    # -- Health & Status --

    async def get_cluster_health(self) -> Dict[str, Any]:
        """Get OpenSearch cluster health."""
        return await self._get("/_cluster/health")

    async def get_archive_overview(self) -> Dict[str, Any]:
        """
        Get comprehensive archive status overview:
        - Cluster health
        - Snapshot repository status
        - Snapshot count & latest snapshot info
        - Index lifecycle status (ISM)
        - Archive indices summary
        """
        overview: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cluster": {},
            "snapshot_repository": {},
            "snapshots": {},
            "indices": {},
            "ism_policies": {},
        }

        # cluster health
        try:
            health = await self.get_cluster_health()
            overview["cluster"] = {
                "status": health.get("status"),
                "number_of_nodes": health.get("number_of_nodes"),
                "active_shards": health.get("active_shards"),
            }
        except ArchiveServiceError as e:
            overview["cluster"] = {"error": str(e)}

        # snapshot repo
        try:
            repo = await self.get_snapshot_repository()
            overview["snapshot_repository"] = repo
        except ArchiveServiceError:
            overview["snapshot_repository"] = {"status": "not_configured"}

        # snapshot summary
        try:
            snapshots = await self.list_snapshots()
            overview["snapshots"] = {
                "total": len(snapshots),
                "latest": snapshots[-1] if snapshots else None,
                "oldest": snapshots[0] if snapshots else None,
            }
        except ArchiveServiceError:
            overview["snapshots"] = {"total": 0, "error": "unavailable"}

        # indices overview
        try:
            indices = await self.list_archive_indices()
            overview["indices"] = {
                "total": len(indices),
                "by_category": self._group_indices_by_category(indices),
            }
        except ArchiveServiceError:
            overview["indices"] = {"error": "unavailable"}

        # ISM policy summary
        try:
            policies = await self.list_ism_policies()
            overview["ism_policies"] = {
                "total": len(policies),
                "policies": [
                    {"name": p.get("_id"), "description": p.get("policy", {}).get("description", "")}
                    for p in policies
                ],
            }
        except ArchiveServiceError:
            overview["ism_policies"] = {"error": "unavailable"}

        return overview

    # -- Snapshot Repository --

    async def get_snapshot_repository(self) -> Dict[str, Any]:
        """Get snapshot repository configuration."""
        data = await self._get(f"/_snapshot/{self._repo}")
        return data.get(self._repo, data)

    async def verify_snapshot_repository(self) -> Dict[str, Any]:
        """Verify snapshot repository is functional."""
        return await self._post(f"/_snapshot/{self._repo}/_verify")

    # -- Snapshots --

    async def list_snapshots(self) -> List[Dict[str, Any]]:
        """List all snapshots in the archive repository."""
        data = await self._get(f"/_snapshot/{self._repo}/_all")
        snapshots = data.get("snapshots", [])
        # sort by start_time
        snapshots.sort(key=lambda s: s.get("start_time", ""))
        return snapshots

    async def get_snapshot(self, snapshot_name: str) -> Dict[str, Any]:
        """Get details of a specific snapshot."""
        data = await self._get(f"/_snapshot/{self._repo}/{snapshot_name}")
        snapshots = data.get("snapshots", [])
        if not snapshots:
            raise ArchiveServiceError(f"Snapshot '{snapshot_name}' not found")
        return snapshots[0]

    async def create_snapshot(
        self,
        snapshot_name: str,
        indices: str = "orcastra-*,vault-audit-*",
        wait_for_completion: bool = False,
    ) -> Dict[str, Any]:
        """
        Create a new snapshot manually.
        """
        params = {"wait_for_completion": str(wait_for_completion).lower()}
        body = {
            "indices": indices,
            "ignore_unavailable": True,
            "include_global_state": False,
        }
        return await self._put(
            f"/_snapshot/{self._repo}/{snapshot_name}",
            json=body,
            params=params,
        )

    async def delete_snapshot(self, snapshot_name: str) -> Dict[str, Any]:
        """Delete a snapshot from the repository."""
        return await self._delete(f"/_snapshot/{self._repo}/{snapshot_name}")

    async def restore_snapshot(
        self,
        snapshot_name: str,
        indices: Optional[str] = None,
        rename_pattern: Optional[str] = None,
        rename_replacement: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Restore indices from a snapshot.
        """
        body: Dict[str, Any] = {
            "ignore_unavailable": True,
            "include_global_state": False,
        }
        if indices:
            body["indices"] = indices
        if rename_pattern and rename_replacement:
            body["rename_pattern"] = rename_pattern
            body["rename_replacement"] = rename_replacement

        return await self._post(
            f"/_snapshot/{self._repo}/{snapshot_name}/_restore",
            json=body,
        )

    async def get_snapshot_status(self) -> Dict[str, Any]:
        """Get status of any currently running snapshots."""
        return await self._get(f"/_snapshot/{self._repo}/_status")

    # -- Indices --

    async def list_archive_indices(self) -> List[Dict[str, Any]]:
        """List all orcastra/vault-audit indices with metadata."""
        data = await self._get(
            "/_cat/indices/orcastra-*,vault-audit-*",
            params={"format": "json", "h": "index,health,status,docs.count,store.size,creation.date.string"},
        )
        return sorted(data, key=lambda i: i.get("index", ""))

    async def get_index_ism_status(self, index: str) -> Dict[str, Any]:
        """Get ISM policy execution status for a specific index."""
        return await self._get(f"/_plugins/_ism/explain/{index}")

    async def list_indices_ism_status(
        self, pattern: str = "orcastra-*,vault-audit-*"
    ) -> Dict[str, Any]:
        """Get ISM status for all matching indices."""
        return await self._get(f"/_plugins/_ism/explain/{pattern}")

    # -- ISM Policies ----

    async def list_ism_policies(self) -> List[Dict[str, Any]]:
        """List all ISM policies."""
        data = await self._get("/_plugins/_ism/policies")
        return data.get("policies", [])

    async def get_ism_policy(self, policy_name: str) -> Dict[str, Any]:
        """Get a specific ISM policy."""
        return await self._get(f"/_plugins/_ism/policies/{policy_name}")

    # -- Helpers --

    @staticmethod
    def _group_indices_by_category(
        indices: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Group indices by category prefix."""
        categories: Dict[str, Dict[str, Any]] = {}
        for idx in indices:
            name = idx.get("index", "")
            for prefix in ("orcastra-access", "orcastra-audit", "orcastra-app", "vault-audit"):
                if name.startswith(prefix):
                    cat = categories.setdefault(prefix, {"count": 0, "total_docs": 0})
                    cat["count"] += 1
                    try:
                        cat["total_docs"] += int(idx.get("docs.count", 0))
                    except (ValueError, TypeError):
                        pass
                    break
        return categories


# -- Singleton --

_archive_service: Optional[ArchiveService] = None


def get_archive_service() -> ArchiveService:
    """Get or create the singleton ArchiveService instance."""
    global _archive_service
    if _archive_service is None:
        settings = get_settings()
        if not settings.opensearch_archive_enabled:
            raise ArchiveServiceError(
                "OpenSearch archive is not enabled. Set OPENSEARCH_ARCHIVE_ENABLED=true"
            )
        if not settings.opensearch_url:
            raise ArchiveServiceError(
                "OPENSEARCH_URL is required when OPENSEARCH_ARCHIVE_ENABLED=true"
            )
        _archive_service = ArchiveService(
            base_url=settings.opensearch_url,
            username=settings.opensearch_admin_user,
            password=settings.opensearch_admin_password,
            verify_ssl=settings.opensearch_ssl_verify,
            snapshot_repo=settings.opensearch_snapshot_repo,
        )
    return _archive_service
