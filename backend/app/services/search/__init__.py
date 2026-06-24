"""Global cross-resource search.

Fans out a single query across every searchable resource (LXD instances,
projects, networks, storage, images, profiles plus DB-backed tags, local
identities and audit entries), enforces the same RBAC scope the per-resource
list endpoints use, ranks matches by relevance and returns grouped results.

Note: this package intentionally avoids eager imports so the pure ``scorer``
module stays importable without the DB/LXD stack. Import ``global_search`` from
``app.services.search.service`` directly.
"""
