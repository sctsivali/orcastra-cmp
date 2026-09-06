"""Where the installation check sits in `validate_api_key` is load-bearing, not incidental.

The check runs AFTER the budget is spent. Placed before it, a suspended add-on would be
refused for free and could hammer the door without ever reaching its own per-key rate limit,
bounded only by the IP limiter. Placed after, it spends its budget and gets throttled like
anything else.

Both orders look equally sensible in a diff, which is exactly why this is pinned: a later tidy-up
that groups "all the authorization checks" together would silently remove the throttle from the
one caller most likely to be hostile.
"""

import ast
import inspect

from app.routers.integrations import validate_api_key


def _statement_order(fn) -> list:
    """The order in which the interesting names appear in the function body."""
    source = inspect.getsource(fn)
    tree = ast.parse(inspect.cleandoc(source) if source.startswith("async def") else source)
    seen = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            seen.append((node.lineno, node.func.id))
    return [name for _, name in sorted(seen)]


def test_the_installation_check_runs_after_the_budget_is_spent() -> None:
    order = _statement_order(validate_api_key)

    # Matched on the prefix rather than the exact name: the call became
    # `consume_api_key_budget_state` when the limiter started reporting the caller's remaining
    # budget, and an exact match would have read that rename as the throttle being deleted.
    spend = next((name for name in order if name.startswith("consume_api_key_budget")), None)
    assert spend is not None, "the per-key rate limit is gone from this path"
    assert "assert_install_is_live" in order, (
        "nothing checks the installation, so suspending an add-on does not stop its credential"
    )
    assert order.index(spend) < order.index("assert_install_is_live"), (
        "the installation check moved ahead of the budget spend, so a suspended add-on is "
        "refused without spending its own rate limit and can hammer the door for free"
    )


def test_only_install_credentials_pay_for_the_lookup() -> None:
    """An ordinary integration key must not acquire a database connection to authenticate.

    Every key on this deployment goes through here, so an unconditional query would put a
    connection checkout on the hot path of the whole external API.
    """
    source = inspect.getsource(validate_api_key)
    marker = source.index("extension_install_id")
    call = source.index("assert_install_is_live")
    assert marker < call, (
        "the installation lookup is not gated on the credential carrying extension_install_id"
    )
