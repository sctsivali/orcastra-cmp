"""Verifying a webhook endpoint is one shot, and a refusal must not become queued traffic.

`verify_subscription` sends its ping through `dispatcher.deliver`, which on failure leaves the
delivery `pending` with a `next_attempt_at` so the queue picks it up. That is right for an ordinary
event and wrong for a verification: the queue then retried the ping four more times over roughly
half an hour, at an endpoint that had just declined it.

Two reasons that matters more than ordinary retry noise. The point of verification is to establish
that the endpoint wants this traffic at all, so retrying a refusal inverts the mechanism. And the
dispatcher's own auto-suspend comment names this behaviour, "us behaving like a reflector rather
than a sender", as the thing to avoid.

A refusal also must not spend the consecutive-failure budget of a subscription that has never been
active, or a few verification attempts against a host that is not deployed yet would suspend it.
"""

import ast
import inspect

from app.routers import extension_webhooks
from app.services.webhooks import dispatcher


def _verify_source() -> str:
    return inspect.getsource(extension_webhooks.verify_subscription)


def test_a_failed_verification_is_marked_terminal() -> None:
    source = _verify_source()
    assert 'delivery.state = "dead"' in source, (
        "a refused verification stays pending, so the queue claims it and keeps posting at an "
        "endpoint that declined"
    )
    assert "delivery.next_attempt_at = None" in source, (
        "the row still carries a due time, so a state change alone may not keep it out of the queue"
    )


def test_a_failed_verification_does_not_spend_the_failure_budget() -> None:
    source = _verify_source()
    failure_branch = source[source.index("else:") :]
    assert "consecutive_failures = 0" in failure_branch, (
        "verification failures accumulate towards AUTO_SUSPEND_AFTER, so a handful of attempts "
        "against an endpoint that is not deployed yet would suspend a subscription that was "
        "never active"
    )


def test_the_queue_only_claims_pending_rows() -> None:
    """The fix rests on this. If the claim widened, marking the row dead would stop working and
    the two assertions above would be green while the behaviour returned."""
    # note: a TextClause, not a string. `in` on it is not a substring test and quietly answers
    # False, which failed this against correct code until it was read rather than assumed.
    claim = str(dispatcher.CLAIM_SQL)
    assert "state = 'pending'" in claim, (
        "the claim no longer selects on pending, so a terminal state may not exclude a row"
    )


def test_the_success_path_still_activates() -> None:
    """The positive twin. A verify handler that only ever marked things dead would pass every
    assertion above while making verification impossible."""
    source = _verify_source()
    tree = ast.parse(inspect.cleandoc(source))
    assigned = {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
    }
    assert "active" in assigned, "nothing sets the subscription active, so verification cannot pass"
    assert "pending_verification" in assigned, "the refused state is gone"
