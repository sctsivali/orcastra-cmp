import unittest

from starlette.requests import Request

from app.core.request_security import (
    get_client_ip,
    get_client_subnet,
    inspect_request_target,
)
from app.core.safe_paths import safe_filename
from app.middleware.security import WindowedRateLimiter


def make_request(headers: dict[str, str], client_ip: str) -> Request:
    encoded_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in headers.items()
    ]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": encoded_headers,
            "client": (client_ip, 44321),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


class RequestSecurityInspectionTests(unittest.TestCase):
    def test_blocks_plain_path_traversal(self) -> None:
        decision = inspect_request_target("GET", "/api/v1/../../../etc/passwd")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.status_code, 404)
        self.assertEqual(decision.reason, "path_traversal")

    def test_blocks_double_encoded_path_traversal(self) -> None:
        decision = inspect_request_target("GET", "/api/v1/%252e%252e/%252e%252e/etc/passwd")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "path_traversal")

    def test_blocks_sensitive_file_probe(self) -> None:
        decision = inspect_request_target("GET", "/api/v1/.env")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.status_code, 404)
        self.assertEqual(decision.reason, "sensitive_path_probe")

    def test_blocks_blind_root_post(self) -> None:
        decision = inspect_request_target("POST", "/")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.status_code, 405)
        self.assertEqual(decision.reason, "root_method_probe")

    def test_blocks_risky_query_path_traversal(self) -> None:
        decision = inspect_request_target(
            "GET",
            "/api/v1/instances",
            "file=..%2F..%2Fetc%2Fpasswd",
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "path_traversal")

    def test_allows_lxd_dest_path_query(self) -> None:
        decision = inspect_request_target(
            "POST",
            "/api/v1/file-transfer/push",
            "dest_path=%2Froot%2F..%2Ftmp%2Fupload.txt",
        )

        self.assertIsNone(decision)


class ClientIpTrustTests(unittest.TestCase):
    def test_ignores_forwarded_for_from_untrusted_client(self) -> None:
        request = make_request({"x-forwarded-for": "203.0.113.10"}, "198.51.100.20")

        self.assertEqual(get_client_ip(request), "198.51.100.20")

    def test_accepts_forwarded_for_from_private_proxy(self) -> None:
        request = make_request({"x-forwarded-for": "203.0.113.10, 172.18.0.2"}, "172.18.0.2")

        self.assertEqual(get_client_ip(request), "203.0.113.10")

    def test_prefers_cloudflare_header_from_trusted_proxy(self) -> None:
        request = make_request(
            {
                "cf-connecting-ip": "203.0.113.55",
                "x-forwarded-for": "198.51.100.99",
            },
            "172.18.0.2",
        )

        self.assertEqual(get_client_ip(request), "203.0.113.55")

    def test_generates_ipv4_subnet_bucket(self) -> None:
        self.assertEqual(get_client_subnet("195.178.110.104"), "195.178.110.0/24")


class RateLimiterTests(unittest.TestCase):
    def test_sliding_window_limit(self) -> None:
        limiter = WindowedRateLimiter(max_buckets=10)

        self.assertTrue(limiter.allow("ip:203.0.113.10", limit=2, window_seconds=60, now=100.0))
        self.assertTrue(limiter.allow("ip:203.0.113.10", limit=2, window_seconds=60, now=101.0))
        self.assertFalse(limiter.allow("ip:203.0.113.10", limit=2, window_seconds=60, now=102.0))
        self.assertTrue(limiter.allow("ip:203.0.113.10", limit=2, window_seconds=60, now=161.0))


class SafeFilenameTests(unittest.TestCase):
    def test_safe_filename_removes_path_separators(self) -> None:
        filename = safe_filename("../../evil/cluster", fallback="cluster")

        self.assertNotIn("/", filename)
        self.assertNotIn("..", filename)
        self.assertTrue(filename)


if __name__ == "__main__":
    unittest.main()
