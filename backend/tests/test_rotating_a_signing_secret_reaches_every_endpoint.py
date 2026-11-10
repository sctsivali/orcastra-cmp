"""One signing secret belongs to an installation, and an installation may have several
subscriptions.

`rotate_secret` is addressed through one subscription, but `rotate_signing_secret` takes an
installation id, so rotating from one endpoint changes the secret for every endpoint on that
installation. The header's fingerprint is computed from the secret at send time, so deliveries stay
signed correctly; every sibling row's stored fingerprint must still be restamped, because that is
the value an operator compares against a receiver's logs.
"""

import inspect

from app.models.extension_models import WebhookSubscription
from app.routers import extension_webhooks
from app.services.webhooks import signing


def _rotate_source() -> str:
    return inspect.getsource(extension_webhooks.rotate_secret)


def test_the_secret_is_scoped_to_the_installation_not_the_subscription() -> None:
    """The premise: if rotation becomes per subscription, the tests below do not apply."""
    source = inspect.getsource(extension_webhooks)
    assert "rotate_signing_secret(get_vault_integrations_client(), row.installation_id)" in source


def test_no_constraint_limits_an_installation_to_one_subscription() -> None:
    """The other half of the premise: with a unique constraint there are no siblings to restamp."""
    args = getattr(WebhookSubscription, "__table_args__", ())
    rendered = str(args)
    assert "installation_id" not in rendered or "Unique" not in rendered, (
        "an installation is now limited to one subscription, so this file should be removed "
        "rather than left asserting something that cannot happen"
    )


def test_every_sibling_gets_the_new_fingerprint() -> None:
    source = _rotate_source()
    assert "WebhookSubscription.installation_id == row.installation_id" in source, (
        "only the subscription named in the path is restamped, so every other endpoint on this "
        "installation reports a fingerprint for a secret that no longer exists"
    )
    assert "for subscription in siblings" in source, "the siblings are fetched but not updated"


def test_the_audit_record_names_what_else_was_affected() -> None:
    source = _rotate_source()
    assert "subscriptions_resigned" in source, (
        "the audit trail does not show that this reached endpoints nobody asked about"
    )


def test_the_endpoint_says_it_affects_more_than_one_endpoint() -> None:
    """The route documents that the secret is installation-wide, since its URL names a single
    subscription."""
    source = inspect.getsource(extension_webhooks)
    block = source[source.index('"/installations/{install_id}/webhooks/{subscription_id}/rotate-secret"') :]
    block = block[: block.index("async def rotate_secret")]
    assert "every endpoint on this installation" in block, (
        "nothing warns that other receivers will start rejecting deliveries"
    )


def test_the_wire_fingerprint_is_computed_not_stored() -> None:
    """The header derives its fingerprint from the secret in hand. Reading the stored column would
    turn a stale fingerprint into a delivery failure."""
    source = inspect.getsource(signing)
    assert "k={secret_fingerprint(secret)}" in source, (
        "the signature header no longer derives its fingerprint from the secret in hand, so a "
        "stale stored fingerprint would now break deliveries rather than just mislead"
    )
