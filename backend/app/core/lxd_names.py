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
