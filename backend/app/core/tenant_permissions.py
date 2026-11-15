"""The permissions a tenant access rule may grant.

One vocabulary for every writer of tenant rules: the dashboard's policy editor
(routers/tenant_access.py), OrcaHub provisioning and the OrcaHub policy sync
(routers/integrations.py). A name outside it grants nothing anywhere, so it is refused where it is
written instead of being stored and reported as synced.
"""

TENANT_RULE_PERMISSIONS = frozenset({"view", "operate", "console", "snapshot", "file_transfer"})
