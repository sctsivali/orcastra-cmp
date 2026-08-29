"""External integrations need a budget of their own, not a share of one IP's.

Rate limiting was keyed on the client IP and its subnet. `_rate_limit_subject` understands only
`Authorization: Bearer`, so an API-key request fell through to `ip:{client_ip}`. Every external
integration reaching this deployment from one egress address therefore shared a single bucket: an
agent polling in a loop, or a harness retrying, would spend the budget that OrcaHub's scheduled
sync needed, and the victim saw 429s it could not explain or influence.

Where the budget is spent is the whole design, and the obvious placement is wrong. Counting in
the middleware, keyed on `X-API-Key-ID`, looks natural and is a targeted denial of service: a key
id is a public identifier, not a credential. It is returned to any partner listing organizations
(`IntegrationOrgResponse.api_key_id`), written to logs, and sent in the header of every outbound
callback. Anyone holding one could present it with a garbage secret and burn that integration's
whole minute from an unauthenticated request. So the spend happens after the secret verifies, and
unauthenticated traffic stays on the per-IP bucket.
"""

import unittest
from typing import Dict, List, Tuple
from unittest import mock

from starlette.requests import Request

from app.core.config import get_settings
from app.middleware import security
from app.middleware.security import RequestSecurityMiddleware, consume_api_key_budget

VALID_ID = "oak_" + "a1b2c3d4" * 4


def make_request(headers: Dict[str, str], client_ip: str = "203.0.113.7") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/integrations/raw-proxy",
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
            ],
            "client": (client_ip, 44321),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


def buckets(request: Request, client_ip: str = "203.0.113.7") -> List[Tuple[str, int]]:
    middleware = RequestSecurityMiddleware(app=None, settings=get_settings())
    return list(middleware._rate_limit_keys(client_ip, request))


class UnauthenticatedTrafficTests(unittest.TestCase):
    """The middleware must not open a per-key bucket, whatever header it is shown."""

    def test_a_forged_key_id_opens_no_bucket_in_the_middleware(self) -> None:
        """The denial-of-service this placement exists to avoid.

        A key id is public. If the middleware counted it, presenting a victim's id with any
        secret at all would spend the victim's budget from an unauthenticated request.
        """
        keys = [key for key, _limit in buckets(make_request({"X-API-Key-ID": VALID_ID}))]
        self.assertFalse(
            [k for k in keys if k.startswith("apikey:")],
            "the middleware must not charge an unverified key id",
        )

    def test_unauthenticated_api_key_traffic_stays_on_the_ip_bucket(self) -> None:
        keys = [key for key, _limit in buckets(make_request({"X-API-Key-ID": VALID_ID}))]
        self.assertIn("ip:203.0.113.7", keys)

    def test_a_browser_request_is_unaffected(self) -> None:
        keys = [key for key, _limit in buckets(make_request({}))]
        self.assertEqual(keys, ["ip:203.0.113.7", "subnet:203.0.113.0/24"])


class AuthenticatedBudgetTests(unittest.TestCase):
    """consume_api_key_budget is what `validate_api_key` calls once the secret has verified."""

    def test_each_key_gets_its_own_counter(self) -> None:
        seen = []

        def fake(bucket, limit, window):
            seen.append(bucket)
            return True

        with mock.patch.object(security, "_shared_allow", fake):
            consume_api_key_budget(VALID_ID)
            consume_api_key_budget("oak_" + "f" * 32)

        self.assertEqual(seen, [f"apikey:{VALID_ID}", "apikey:oak_" + "f" * 32])

    def test_the_budget_is_counted_in_the_shared_store(self) -> None:
        """Per-worker counters would silently mean WEB_CONCURRENCY times the configured figure."""
        with mock.patch.object(security, "_shared_allow", return_value=True) as shared:
            self.assertTrue(consume_api_key_budget(VALID_ID))
        shared.assert_called_once()
        _bucket, limit, window = shared.call_args[0]
        settings = get_settings()
        self.assertEqual(limit, settings.rate_limit_api_key_requests)
        self.assertEqual(window, settings.rate_limit_window_seconds)

    def test_exceeding_the_budget_is_refused(self) -> None:
        with mock.patch.object(security, "_shared_allow", return_value=False):
            self.assertFalse(consume_api_key_budget(VALID_ID))

    def test_it_fails_open_when_the_shared_store_cannot_answer(self) -> None:
        """A Redis outage must not throttle every integration at once; the IP bucket still holds."""
        with mock.patch.object(security, "_shared_allow", return_value=None):
            self.assertTrue(consume_api_key_budget(VALID_ID))

    def test_a_zero_budget_disables_the_bucket(self) -> None:
        disabled = get_settings().model_copy(update={"rate_limit_api_key_requests": 0})
        with mock.patch.object(security, "_shared_allow") as shared:
            with mock.patch("app.core.config.get_settings", return_value=disabled):
                self.assertTrue(consume_api_key_budget(VALID_ID))
        shared.assert_not_called()

    def test_the_bucket_is_skipped_when_rate_limiting_is_off(self) -> None:
        off = get_settings().model_copy(update={"rate_limit_enabled": False})
        with mock.patch.object(security, "_shared_allow") as shared:
            with mock.patch("app.core.config.get_settings", return_value=off):
                self.assertTrue(consume_api_key_budget(VALID_ID))
        shared.assert_not_called()


class BudgetSizingTests(unittest.TestCase):
    def test_the_per_key_budget_binds_before_the_ip_budget(self) -> None:
        """The property the whole bucket depends on, and the one easy to get backwards.

        At or above the per-IP limit the IP bucket is always reached first, the per-key bucket
        never binds, and a runaway key still spends every other integration's budget. Isolation
        exists only while this is strictly lower, which also guarantees a second key behind the
        same address always retains `rate_limit_requests - rate_limit_api_key_requests` headroom.
        """
        settings = get_settings()
        if settings.rate_limit_api_key_requests == 0:
            self.skipTest("per-key bucket disabled in this environment")
        self.assertLess(
            settings.rate_limit_api_key_requests,
            settings.rate_limit_requests,
            "a per-key budget at or above the per-IP budget can never bind, so it isolates nothing",
        )


class SpendPlacementTests(unittest.TestCase):
    def test_the_spend_happens_after_the_secret_is_verified(self) -> None:
        """Structural: the placement IS the security property, and moving it looks harmless.

        `validate_api_key` verifies through `_validate_api_key_blocking` and then checks revoked,
        inactive and expired. The budget must be spent after all of that, so a request that never
        proves it holds the key cannot charge it.
        """
        import inspect

        from app.routers.integrations import validate_api_key

        source = inspect.getsource(validate_api_key)
        self.assertIn("consume_api_key_budget", source)
        self.assertLess(
            source.index("has expired"),
            source.index("consume_api_key_budget"),
            "the budget is spent before the state checks, so an expired key can still spend it",
        )
