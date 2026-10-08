"""A location change must reach every caller's cached clusters list and regions map.

Every partner and tenant caches GET /clusters and the regions overview under its own scope key
(`...:scope=<digest>`, `...:scope=<digest>:acl=<digest>`). The location PATCH deleted only the
caller's scope key and `scope=all`, so everyone else whose cluster set holds the cluster kept
the old place for the TTL: 180 s on the Clusters page and 120 s on the regions map. Redis is
authoritative across workers, so dropping the Redis keys is what makes every worker re-read.
"""

import fnmatch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.cache import CacheManager

P = "orcastra:api_responses:"
DROPPED = [
    P + "clusters:list:all:scope=all",
    P + "clusters:list:all:scope=ab12cd34ef56",
    P + "clusters:list:all:scope=ab12cd34ef56:acl=ff00aa11",
    P + "clusters:facilities:scope=all",
    P + "regions:overview:v6:scope=all",
    P + "regions:overview:v6:scope=0123456789ab",
    P + "regions:overview:v6:scope=0123456789ab:acl=00ff00ff",
]
KEPT = [
    P + "instances:list:all:scope=all",
    P + "cluster_members:list:host=other-cluster:scope=all",
    P + "projects:list:all:scope=ab12cd34ef56",
]

LOCATION = {
    "region_id": "c-sg", "region_name": "Singapore", "country_code": "SG",
    "country_name": "Singapore", "city": "Singapore", "latitude": 1.29, "longitude": 103.85,
    "timezone": "Asia/Singapore", "continent": "AS",
}


class FakeRedis:
    def __init__(self, keys):
        self.store = {k: "cached" for k in keys}

    def keys(self, pattern):
        return [k for k in self.store if fnmatch.fnmatchcase(k, pattern)]

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


class _Store:
    def get_cluster_info(self, cluster_id, include_cert_content=False):
        return {"metadata": {"endpoint": "https://192.0.2.30:8443", "display_name": cluster_id,
                             "type": "LXD", "zone": "Jakarta", "location": {}}}

    def save_cluster_metadata(self, **kwargs):
        return True


@pytest.fixture
def client_and_redis(monkeypatch):
    from app.routers import cluster_registration as module
    from app.core.dependencies import check_read_only_mode
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    manager = CacheManager(redis_url=None)
    manager.redis_client = FakeRedis(DROPPED + KEPT)
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: manager)

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_cert_manager] = _Store
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False), manager.redis_client


def test_every_scope_loses_its_cached_list_and_map(client_and_redis):
    client, redis = client_and_redis
    response = client.patch("/clusters/qa-scope/location", json={"location": LOCATION})
    assert response.status_code == 200, response.text

    left = [k for k in DROPPED if k in redis.store]
    assert left == [], f"still cached after the move: {left}"


def test_unrelated_caches_are_left_alone(client_and_redis):
    client, redis = client_and_redis
    client.patch("/clusters/qa-scope/location", json={"location": LOCATION})

    assert all(k in redis.store for k in KEPT), sorted(redis.store)
