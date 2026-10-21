"""Ratchet: a router must not dereference a client the breaker may have refused.

``connection_manager.get_client`` returns ``None`` for a cluster the breaker has tripped,
without making any network call. An unguarded dereference then raises ``AttributeError``
into a broad ``except Exception`` and the operator gets 500, or worse a 404 claiming their
operation, volume or cluster member does not exist.

TWO rules, because either alone is a ratchet that passes while the endpoint stays broken:

  1. every ``get_client`` result is guarded before use, either by ``require_client`` or by an
     explicit ``if client is None: raise HTTPException(503, ...)``
  2. every ``try`` containing a ``require_client`` call that also catches ``Exception``
     carries ``except HTTPException: raise``

Rule 2 is the one that bites. Eleven of the nineteen sites converted in this sweep had a
broad handler and no passthrough, so the 503 the guard raised was caught two lines later and
re-raised as a 500 with ``{"detail": "503: Could not connect to blitz01"}``. That was
observed, not theorised: the guards went in first and the behavioural tests stayed red until
the passthroughs followed.

Structural, because the behavioural half lives in ``test_unreachable_cluster_is_503.py``.
That one proves the shape produces a 503 on ten real endpoints; this one stops the shape
regrowing on the others without anyone noticing.

No baseline count. This sweep converted every in-scope site in one pass, so the rule is
absolute from day one, and an absolute rule cannot rot the way a count can.
"""

import ast
import pathlib
import unittest


ROUTERS = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers"


# Handlers that correctly tolerate a None client. Anything not listed must be guarded.
# Each entry carries the reason, because an unexplained exemption is indistinguishable from
# an oversight six months later.
TOLERATES_NONE = {
    # Fan-out handlers. A dead node is collected into unreachable_nodes and the response is
    # a 200 naming which clusters were lost, which is a better contract than 503 because it
    # says what did answer. Covered by FailureIsLabelledTests in test_fresh_bypass_is_wired.
    # note: that premise was false for six of these for a while. They raised the connection
    # error and their own outer `except Exception` caught it straight back into an empty
    # list, so the exemption held while the behaviour it assumed did not. It is now enforced
    # by UnreachableIsNotTheSameAsErroringTests
    # .test_the_raise_is_not_swallowed_by_the_helper_s_own_broad_handler.
    ("cluster_members.py", "list_cluster_members"),
    ("cluster_registration.py", "_cluster_info_blocking"),
    # Reads the pooled client only to resolve the hypervisor family from its cached host_info.
    # A None client means the type is left unreported, which is the honest answer; 503 here
    # would be a regression, since reporting the status of an offline cluster is the job.
    ("cluster_registration.py", "get_cluster"),
    ("ipam.py", "_fetch_ipam_for_node"),
    ("lxd_operations.py", "_fetch_operations_for_node"),
    ("lxd_server.py", "_fetch_server_info_for_node"),
    ("lxd_warnings.py", "_fetch_warnings_for_node"),
    ("network_acls.py", "_fetch_acls_for_node"),
    ("permissions.py", "_fetch_identities_for_node"),
    ("permissions.py", "_fetch_groups_for_node"),
    ("storage_volumes.py", "_fetch_volumes_for_node"),
    ("regions.py", "get_regions_overview"),
    ("regions.py", "_process_cluster"),
    # Session bookkeeping. These count or tear down console sessions across the fleet; one
    # unreachable cluster must not fail the sweep for the others.
    ("console.py", "check_active_sessions"),
    ("console.py", "kill_all_sessions"),
    ("console.py", "kill_active_session"),
    # Background upload workers. They report failure through the upload session record
    # rather than an HTTP status, because the HTTP request returned long ago.
    ("image_uploads.py", "_resolve_alias_sync"),
    ("image_uploads.py", "_lxd_import_sync"),
    ("iso_uploads.py", "_resolve_volume_sync"),
    ("iso_uploads.py", "_lxd_import_iso_sync"),
    # Best-effort cleanup after the database row is already gone. A dead node must not fail
    # a delete the caller has already committed to, and skipping one node is the entire
    # point of the per-node loop.
    ("my_keys.py", "delete_key"),
    ("my_keys.py", "cleanup_orphaned_groups"),
    # Validates the node name itself and answers 404 for an unregistered node. Conflates
    # "never registered" with "offline", which is worth fixing, but it is a status change on
    # a path where verify_cluster_access does not run, so it needs its own reasoning.
    ("my_keys.py", "create_key"),
}


def _is_client_none_check(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and bool(test.ops)
        and isinstance(test.ops[0], ast.Is)
        and bool(test.comparators)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
        and isinstance(test.left, ast.Name)
        and "client" in test.left.id.lower()
    )


def _raises_503(node: ast.If) -> bool:
    for stmt in ast.walk(node):
        if not isinstance(stmt, ast.Raise) or stmt.exc is None:
            continue
        exc = stmt.exc
        if not isinstance(exc, ast.Call):
            continue
        if getattr(exc.func, "id", None) != "HTTPException":
            continue
        for kw in exc.keywords:
            if kw.arg == "status_code" and getattr(kw.value, "value", None) == 503:
                return True
    return False


def _handler_names(node: ast.Try) -> list:
    out = []
    for h in node.handlers:
        t = h.type
        if t is None:
            out.append("bare")
        elif isinstance(t, ast.Tuple):
            out.extend(getattr(e, "id", getattr(e, "attr", "?")) for e in t.elts)
        else:
            out.append(getattr(t, "id", getattr(t, "attr", "?")))
    return out


class RouterClientDerefTests(unittest.TestCase):
    def test_every_get_client_result_is_guarded(self):
        offenders = []
        for path in sorted(ROUTERS.glob("*.py")):
            src = path.read_text()
            tree = ast.parse(src)
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if (path.name, fn.name) in TOLERATES_NONE:
                    continue
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                        continue
                    call = node.value
                    if getattr(call.func, "id", None) == "require_client":
                        continue
                    if getattr(call.func, "attr", None) != "get_client":
                        continue
                    hand_guarded = any(
                        isinstance(n, ast.If) and _is_client_none_check(n.test) and _raises_503(n)
                        for n in ast.walk(fn)
                    )
                    if not hand_guarded:
                        offenders.append(
                            f"{path.name}:{node.lineno} {fn.name} uses a client the breaker "
                            "may have refused. Wrap it in require_client(...) so an offline "
                            "cluster answers 503 instead of an AttributeError becoming a 500."
                        )
        self.assertEqual(offenders, [])

    def test_a_deliberate_status_is_not_converted_by_a_broad_handler(self):
        """The guard is only half the fix. Without this the status is rewritten.

        This used to look only for `require_client(`, which covered the converted helpers and
        nothing else. Thirteen try-blocks across six routers raised HTTPException directly,
        with a status chosen on purpose, and their own broad handler rewrote it: an
        unreachable cluster answered 500 or 404 instead of 503, and a tenant ACL denial
        answered 500 instead of 403. Found in the log during a full QA sweep, as
        "Error getting profile default: 503: Could not connect to ...".

        The rule is the general one: if a try raises HTTPException anywhere in its body and
        also catches Exception or bare, it needs the passthrough. How the exception got there,
        through require_client or by hand, does not change the consequence.
        """
        offenders = []
        for path in sorted(ROUTERS.glob("*.py")):
            src = path.read_text()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                seg = ast.get_source_segment(src, node) or ""
                raises_directly = any(
                    isinstance(n, ast.Raise)
                    and isinstance(n.exc, ast.Call)
                    and getattr(n.exc.func, "id", "") == "HTTPException"
                    for stmt in node.body
                    for n in ast.walk(stmt)
                )
                if "require_client(" not in seg and not raises_directly:
                    continue
                names = _handler_names(node)
                if "Exception" not in names and "bare" not in names:
                    continue
                if "HTTPException" not in names:
                    offenders.append(
                        f"{path.name}:{node.lineno} raises a deliberate status inside a try "
                        "whose broad handler rewrites it. Add `except HTTPException: raise` "
                        "above it."
                    )
        self.assertEqual(offenders, [])

    def test_every_exemption_names_a_real_handler(self):
        """An exemption for a function that does not exist relaxes nothing and hides it.

        test_fresh_bypass_is_wired.py spent an unknown period exempting a function that had
        never existed anywhere under app/, which is why this assertion exists in both files.
        """
        defined = set()
        for path in ROUTERS.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined.add((path.name, node.name))
        stale = sorted(f"{f}:{fn}" for f, fn in TOLERATES_NONE if (f, fn) not in defined)
        self.assertEqual(stale, [], f"exemptions naming handlers that do not exist: {stale}")


if __name__ == "__main__":
    unittest.main()
