import unittest

from starlette.requests import Request

from app.core.request_security import (
    get_client_ip,
    get_client_subnet,
    inspect_request_target,
    resolve_client_ip,
    untrusted_own_network_warning,
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
            "/api/v1/instance-files/uploads",
            "dest_path=%2Froot%2F..%2Ftmp%2Fupload.txt",
        )

        self.assertIsNone(decision)


class ClientIpTrustTests(unittest.TestCase):
    def test_ignores_forwarded_for_from_untrusted_client(self) -> None:
        request = make_request({"x-forwarded-for": "195.178.110.104"}, "142.132.147.94")

        self.assertEqual(get_client_ip(request), "142.132.147.94")

    def test_accepts_forwarded_for_from_private_proxy(self) -> None:
        request = make_request({"x-forwarded-for": "195.178.110.104, 172.18.0.2"}, "172.18.0.2")

        self.assertEqual(get_client_ip(request), "195.178.110.104")

    def test_prefers_cloudflare_header_from_trusted_proxy(self) -> None:
        request = make_request(
            {
                "cf-connecting-ip": "142.132.147.94",
                "x-forwarded-for": "195.178.110.104",
            },
            "172.18.0.2",
        )

        self.assertEqual(get_client_ip(request), "142.132.147.94")

    def test_prefers_internal_orcastra_client_ip_from_trusted_proxy(self) -> None:
        request = make_request(
            {
                "x-orcastra-client-ip": "195.178.110.104",
                "cf-connecting-ip": "142.132.147.94",
            },
            "172.18.0.2",
        )

        resolution = resolve_client_ip(request)

        self.assertEqual(resolution.client_ip, "195.178.110.104")
        self.assertEqual(resolution.direct_ip, "172.18.0.2")
        self.assertEqual(resolution.source, "x-orcastra-client-ip")
        self.assertTrue(resolution.trusted_proxy)

    def test_a_configured_list_replaces_the_default(self) -> None:
        # RC5 acceptance FU-6: the default was always added to a configured list, so a list could
        # widen trust but never narrow it, and the installer's narrow value had no effect.
        request = make_request({"x-orcastra-client-ip": "195.178.110.104"}, "172.18.0.5")

        self.assertEqual(
            get_client_ip(request, trusted_proxy_cidrs="173.245.48.0/20,103.21.244.0/22"),
            "172.18.0.5",
        )

    def test_the_installer_value_trusts_its_compose_network_and_nothing_else_private(self) -> None:
        installer = "127.0.0.0/8,::1/128,172.20.0.0/14"
        from_compose = make_request({"x-orcastra-client-ip": "195.178.110.104"}, "172.21.0.3")
        from_the_lan = make_request({"x-orcastra-client-ip": "195.178.110.104"}, "192.168.1.20")

        self.assertEqual(get_client_ip(from_compose, trusted_proxy_cidrs=installer), "195.178.110.104")
        self.assertEqual(get_client_ip(from_the_lan, trusted_proxy_cidrs=installer), "192.168.1.20")

    def test_loopback_is_trusted_whatever_the_list_says(self) -> None:
        request = make_request({"x-forwarded-for": "8.8.8.8"}, "127.0.0.1")

        self.assertEqual(get_client_ip(request, trusted_proxy_cidrs="173.245.48.0/20"), "8.8.8.8")

    def test_an_empty_list_keeps_the_default(self) -> None:
        request = make_request({"x-forwarded-for": "8.8.8.8"}, "10.1.2.3")

        self.assertEqual(get_client_ip(request, trusted_proxy_cidrs=""), "8.8.8.8")
        self.assertEqual(get_client_ip(request, trusted_proxy_cidrs=None), "8.8.8.8")

    def test_startup_warns_when_its_own_network_is_not_trusted(self) -> None:
        warning = untrusted_own_network_warning("173.245.48.0/20", own_ip="172.18.0.4")
        self.assertIsNotNone(warning)
        self.assertIn("172.18.0.4", warning)
        self.assertIsNone(untrusted_own_network_warning("127.0.0.0/8,172.16.0.0/12", own_ip="172.18.0.4"))
        self.assertIsNone(untrusted_own_network_warning("173.245.48.0/20", own_ip=None))

    def test_rejects_private_forwarded_ip_by_default(self) -> None:
        request = make_request({"cf-connecting-ip": "172.18.0.5"}, "172.18.0.2")

        self.assertEqual(get_client_ip(request), "172.18.0.2")

    def test_allows_private_forwarded_ip_when_explicitly_enabled(self) -> None:
        request = make_request({"cf-connecting-ip": "172.18.0.5"}, "172.18.0.2")

        self.assertEqual(get_client_ip(request, allow_private_forwarded_ips=True), "172.18.0.5")

    def test_accepts_standard_forwarded_header_from_trusted_proxy(self) -> None:
        request = make_request({"forwarded": "for=195.178.110.104;proto=https"}, "172.18.0.2")

        self.assertEqual(get_client_ip(request), "195.178.110.104")

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
