"""A webhook delivery must be reachable only through the installation that owns it.

`redeliver` checked that the caller could see the installation named in the path, then loaded the
delivery by primary key alone. Any partner holding any installation could requeue any delivery in
the deployment by guessing an integer, and the response hands back its `event_id`. `list_deliveries`
two handlers above already scoped the same table correctly, which is what made it an omission
rather than a design choice.

note: this is a structural check on the statement the handler builds, not a request through the
endpoint. No test in this suite issues HTTP to an extensions route, and building a cross-tenant
delivery fixture needs two installations plus a delivered webhook. So this locks the filter's
presence and the live cross-tenant attempt belongs in the browser harness. Stated rather than
implied, because a structural test that reads like a behavioural one is worse than no test.
"""

import ast
import inspect

from app.routers import extension_webhooks


def _handler_source(name: str) -> str:
    fn = getattr(extension_webhooks, name)
    return inspect.getsource(fn)


def test_redeliver_constrains_the_delivery_to_the_installation() -> None:
    source = _handler_source("redeliver")

    assert "WebhookSubscription.installation_id == install_id" in source, (
        "redeliver loads a delivery without binding it to the installation in the path, "
        "so any partner can requeue any delivery by guessing an id"
    )
    assert "WebhookSubscription.id == WebhookDelivery.subscription_id" in source, (
        "the delivery is not joined to its subscription, so the installation filter cannot apply"
    )


def test_every_delivery_lookup_names_a_scope() -> None:
    """The general rule, so a new handler cannot reintroduce the same shape.

    Any handler that selects WebhookDelivery must also mention installation_id or subscription_id.
    A bare primary-key lookup on a child table is the shape of the bug.
    """
    module_source = inspect.getsource(extension_webhooks)
    tree = ast.parse(module_source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        body = ast.get_source_segment(module_source, node) or ""
        if "WebhookDelivery" not in body:
            continue
        if "installation_id" in body or "subscription_id" in body:
            continue
        offenders.append(node.name)

    assert not offenders, (
        f"these handlers touch WebhookDelivery without naming an owning scope: {offenders}"
    )


def test_the_check_can_fail() -> None:
    """Guards the guard. If `redeliver` ever stops existing under that name, the two tests above
    would pass vacuously by reading an empty string."""
    assert hasattr(extension_webhooks, "redeliver"), "redeliver is gone; the tests above are inert"
    assert "WebhookDelivery" in _handler_source("redeliver"), (
        "redeliver no longer mentions WebhookDelivery; the assertions above prove nothing"
    )
