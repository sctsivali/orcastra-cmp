"""Each API key spends a rate-limit budget of its own, beside its address's.

`_rate_limit_subject` understands only `Authorization: Bearer`, so an API-key request lands on
`ip:{client_ip}`. Without a per-key bucket every integration behind one egress address shares that
bucket, and one key polling in a loop spends the budget another integration's scheduled sync needs.

The spend happens after the secret verifies. A key id is public: it is returned to any partner
listing organizations (`IntegrationOrgResponse.api_key_id`), written to logs, and sent in the
header of every outbound callback. Counted in the middleware, it would let anyone burn an
integration's whole minute with a garbage secret. Unauthenticated traffic stays on the per-IP
bucket.
"""

import unittest
from typing import Dict, List, Tuple
from unittest import mock

from starlette.requests import Request

from app.core.config import Settings, get_settings
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
        """A key id is public, so a middleware count would let an unauthenticated request
        presenting a victim's id spend the victim's budget."""
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
    """The per-key budget must be strictly lower than the per-IP one.

    At or above the per-IP limit the IP bucket is always reached first, the per-key bucket never
    binds, and a runaway key still spends every other integration's budget. Below it, a second key
    behind the same address keeps `rate_limit_requests - rate_limit_api_key_requests` headroom.

    Asserted against the defaults rather than `get_settings()`, so a host's `.env` cannot fail the
    suite. The runtime warns about a misconfigured deployment at startup.
    """

    def test_the_default_per_key_budget_binds_before_the_default_ip_budget(self) -> None:
        fields = Settings.model_fields
        per_key = fields["rate_limit_api_key_requests"].default
        per_ip = fields["rate_limit_requests"].default
        self.assertGreater(per_key, 0, "the shipped default must not disable the bucket")
        self.assertLess(
            per_key,
            per_ip,
            "a per-key budget at or above the per-IP budget can never bind, so it isolates nothing",
        )

    def test_a_deployment_that_breaks_the_relationship_is_warned_about(self) -> None:
        """Startup warns an operator who tunes either variable out of that relationship.

        Reads the file rather than importing `app.main`: the import builds the whole FastAPI app
        as a side effect and can fail depending on which tests ran before.
        """
        import ast
        from pathlib import Path

        main = Path(__file__).resolve().parents[1] / "app" / "main.py"
        tree = ast.parse(main.read_text())
        lifespan = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "lifespan"
            ),
            None,
        )
        self.assertIsNotNone(lifespan, "app/main.py no longer defines lifespan")

        source = ast.unparse(lifespan)
        self.assertIn("RATE_LIMIT_API_KEY_REQUESTS", source)
        self.assertIn("rate_limit_api_key_requests >= settings.rate_limit_requests", source)


class SpendPlacementTests(unittest.TestCase):
    def test_the_spend_happens_after_the_secret_is_verified(self) -> None:
        """`validate_api_key` verifies through `_validate_api_key_blocking` and then checks
        revoked, inactive and expired. The budget is spent after all of that, so a request that
        never proves it holds the key cannot charge it.
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
