"""Ratchet: a router must not dereference a client the breaker may have refused.

``connection_manager.get_client`` returns ``None`` for a tripped cluster. An unguarded
dereference raises ``AttributeError`` into a broad ``except Exception``: a 500, or a false 404.
Two rules, because either alone passes while the endpoint stays broken:

  1. every ``get_client`` result is guarded by ``require_client`` or by
     ``if client is None: raise HTTPException(503, ...)``
  2. every ``try`` that raises ``HTTPException`` and also catches ``Exception`` carries
     ``except HTTPException: raise``, or the broad handler turns the 503 into a 500

``test_unreachable_cluster_is_503.py`` proves the behaviour on real endpoints. No baseline.
"""

import ast
import pathlib
import unittest


ROUTERS = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers"


# Handlers that tolerate a None client, each with its reason. Anything not listed must be
# guarded.
TOLERATES_NONE = {
    # Fan-out handlers. A dead node is collected into unreachable_nodes and the response is
    # a 200 naming which clusters were lost, so it still says what did answer. Covered by
    # FailureIsLabelledTests and UnreachableIsNotTheSameAsErroringTests in
    # test_fresh_bypass_is_wired.
    ("cluster_members.py", "list_cluster_members"),
    ("cluster_registration.py", "_cluster_info_blocking"),
    # Reads the pooled client only to resolve the hypervisor family from its cached host_info.
    # A None client leaves the type unreported. The endpoint reports an offline cluster's
    # status, so it must not answer 503.
    ("cluster_registration.py", "get_cluster"),
    ("ipam.py", "_fetch_ipam_for_node"),
    ("lxd_operations.py", "_fetch_operations_for_node"),
    ("lxd_server.py", "_fetch_server_info_for_node"),
    ("lxd_warnings.py", "_fetch_warnings_for_node"),
    ("network_acls.py", "_fetch_acls_for_node"),
    ("permissions.py", "_fetch_identities_for_node"),
    ("permissions.py", "_fetch_groups_for_node"),
    ("storage_volumes.py", "_fetch_volumes_for_node"),
    ("regions.py", "build_regions_overview"),
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
    # a delete the caller has already committed to; the per-node loop skips it.
    ("my_keys.py", "delete_key"),
    ("my_keys.py", "cleanup_orphaned_groups"),
    # Validates the node name itself and answers 404 for an unregistered node. It does not
    # tell "never registered" from "offline"; changing that status needs its own review,
    # since verify_cluster_access does not run on this path.
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
        """A deliberate HTTPException status is not rewritten by a broad handler.

        A try that raises HTTPException anywhere in its body, through require_client or by
        hand, and also catches Exception or bare needs the passthrough. Otherwise an
        unreachable cluster answers 500 or 404 instead of 503, and a tenant ACL denial
        answers 500 instead of 403.
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
        """An exemption for a function that does not exist relaxes nothing and hides it."""
        defined = set()
        for path in ROUTERS.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined.add((path.name, node.name))
        stale = sorted(f"{f}:{fn}" for f, fn in TOLERATES_NONE if (f, fn) not in defined)
        self.assertEqual(stale, [], f"exemptions naming handlers that do not exist: {stale}")


if __name__ == "__main__":
    unittest.main()
