"""Names that travel into an LXD request path.

pylxd builds every request URL by string concatenation and escapes nothing
(`_APINode.__getitem__`, pylxd/client.py:189-202), so any name a caller supplies becomes a raw path
segment. A segment is therefore not just data: a `?` or a `#` in it ends the path and moves the
request to a different resource.

The snapshot routes make that concrete. `/1.0/instances/{instance}/snapshots/{snapshot}` carries two
caller-controlled segments, and the instance one comes first, so

    DELETE /instances/ct-victim%3F/snapshots/ops-anything

reaches the daemon as `DELETE /1.0/instances/ct-victim`, which deletes the instance rather than a
snapshot. FastAPI hands the handler the percent-decoded value, so encoding on the wire does not
help: the guard has to be here.

Validating at the route boundary rather than inside the handler means a malformed name is refused
with a 422 before any client is built, any cluster is contacted, or any authorization decision is
made on a name the server cannot address.

The rule is LXD's own for instances, snapshots, projects and volumes: letters, digits and dashes,
starting with a letter or digit, at most 63 characters.
"""

# note: kept as a string rather than a compiled pattern because FastAPI's Path/Query/Field take the
# pattern directly, which is what puts the refusal at the boundary instead of in the handler body.
LXD_NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$"

LXD_NAME_DESCRIPTION = (
    "Letters, digits and dashes, starting with a letter or digit, at most 63 characters"
)


# The instance config keys that describe an LXD-native snapshot schedule.
#
# important: these belong to the metered snapshot product sold upstream. Orcastra reads them and
# never writes them, because a write leaves that product's ledger disagreeing with the cluster
# about a schedule a tenant is paying for. `snapshots.schedule` is also the single key
# services/lxd/snapshots.py derives `externally_managed` from, so setting one from this console
# makes an instance report itself as owned by another product and puts the operator's own snapshot
# actions behind a billing acknowledgement they cannot answer for.
#
# They live in this leaf module rather than beside the snapshot service so the create request
# models can refuse them at the boundary. models/schemas.py is imported early and pulling the LXD
# service package in from there inverts the dependency; this module imports nothing from the app.
POLICY_CONFIG_KEYS = (
    "snapshots.schedule",
    "snapshots.schedule.stopped",
    "snapshots.expiry",
    "snapshots.pattern",
)


# A storage volume's name, which is a laxer rule than an instance's in two ways.
#
# important: this is the same traversal gate as LXD_NAME_PATTERN, not a weaker one. The character
# class is the security property and it still refuses `/`, `?`, `#`, whitespace and a leading dot,
# so a bare `..` cannot pass. What it admits is `.` and `_`, because real volume names carry them:
# this fleet holds `netgate-installer-v1.1.1-RELEASE-amd64.iso` and `qa-pr6-v1.iso`, and the
# instance rule would make every ISO volume unreachable.
#
# The length is a sanity bound rather than a security one, and 63 is the wrong number here. An
# `image` volume is named by its SHA256 fingerprint, which is 64 characters, so the instance limit
# refuses every image volume on the fleet. 128 clears that with room for a longer digest without
# leaving the bound open.
#
# note: an instance's own volume snapshots are named `instance/snapshot` by LXD, so they carry a
# slash and are not addressable through a single path segment at all. That predates this pattern
# and is not changed by it: those rows are listed, never addressed by name.
#
# `routers/iso_uploads.py` has carried this character class since ISO upload shipped, with a comment
# naming it the IDOR gate. It lives here now so the volume routes beside it read one definition
# rather than a second copy that can drift.
LXD_VOLUME_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"

LXD_VOLUME_NAME_DESCRIPTION = (
    "Letters, digits, dots, underscores and dashes, starting with a letter or digit, "
    "at most 128 characters"
)


# A cluster member (server) name as it reaches a ?target=, a cache key or a log line. LXD itself only
# refuses an empty name, "none" and the "@group" target prefix, and members are named after their
# hosts, so this is a hostname-shaped allowlist: letters, digits, dots, underscores and dashes,
# starting with a letter or digit, at most 253 characters.
LXD_MEMBER_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$"
