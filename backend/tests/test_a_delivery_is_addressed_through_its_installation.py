"""A webhook delivery must be reachable only through the installation that owns it.

If `redeliver` checked only the installation named in the path and then loaded the delivery by
primary key alone, any partner holding any installation could requeue any delivery in the
deployment by guessing an integer, and the response hands back its `event_id`.

This is a structural check on the statement the handler builds, not a request through the
endpoint: it locks the presence of the filter. A live cross-tenant attempt needs two installations
and a delivered webhook, and belongs in an end-to-end test.
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
    """Any handler that selects WebhookDelivery also names installation_id or subscription_id.

    A bare primary-key lookup on a child table skips the owner check.
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
    """`redeliver` exists and selects WebhookDelivery, so the tests above cannot pass vacuously."""
    assert hasattr(extension_webhooks, "redeliver"), "redeliver is gone; the tests above are inert"
    assert "WebhookDelivery" in _handler_source("redeliver"), (
        "redeliver no longer mentions WebhookDelivery; the assertions above prove nothing"
    )
