"""Validation patterns for names that end up in an LXD request path.

pylxd builds request URLs by concatenation and escapes nothing (`_APINode.__getitem__`,
pylxd/client.py:189-202), so a caller-supplied name becomes a raw path segment. A `?` or `#` in it
ends the path early: `DELETE /instances/ct-victim%3F/snapshots/x` reaches LXD as
`DELETE /1.0/instances/ct-victim` and deletes the instance. FastAPI hands handlers the decoded
value, so the check has to happen here, at the route boundary, before any cluster is contacted.

The instance rule is LXD's own: letters, digits and dashes, starting with a letter or digit, at
most 63 characters.
"""

# A string, not a compiled regex, because FastAPI's Path/Query/Field take the pattern directly.
LXD_NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$"

LXD_NAME_DESCRIPTION = (
    "Letters, digits and dashes, starting with a letter or digit, at most 63 characters"
)


# Instance config keys that define an LXD-native snapshot schedule.
#
# important: read-only for Orcastra. These keys can belong to a metered snapshot product managed
# outside this console, and services/lxd/snapshots.py derives `externally_managed` from
# `snapshots.schedule`. Writing one would desync that product's billing and mark the instance as
# externally managed. Kept in this import-free module so request models can refuse them.
POLICY_CONFIG_KEYS = (
    "snapshots.schedule",
    "snapshots.schedule.stopped",
    "snapshots.expiry",
    "snapshots.pattern",
)


# Storage volume names. Also allows `.` and `_`, which ISO file names need.
#
# important: still a path-traversal gate. The character class refuses `/`, `?`, `#`, whitespace and
# a leading dot, so `..` cannot pass. The 128 limit is a sanity bound: image volumes are named by
# their 64-character SHA256 fingerprint. Volume snapshots (`instance/snapshot`) contain a slash and
# are listed, never addressed by name. routers/iso_uploads.py checks the same character class
# with its own pattern, capped at 63 characters.
LXD_VOLUME_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"

LXD_VOLUME_NAME_DESCRIPTION = (
    "Letters, digits, dots, underscores and dashes, starting with a letter or digit, "
    "at most 128 characters"
)


# Cluster member names as they reach `?target=`, cache keys and logs. LXD only refuses "", "none"
# and the "@group" prefix; members are named after hosts, so this is a hostname-shaped allowlist.
LXD_MEMBER_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$"
