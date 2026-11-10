"""The frontend liveness probe must depend on nothing outside the process.

`/` is auth-gated and its redirect carries the public origin (next-auth rewrites the request
origin to NEXTAUTH_URL), so a probe that follows it leaves the container, and one edge 502
marks a working container unhealthy for autoheal to restart. Checked structurally:

  * the probe targets /healthz, a route that calls nothing (not /health, the readiness proxy,
    which would restart the frontend whenever the backend restarts);
  * the probe client cannot follow a redirect (Node's http.get), has its own timeout, and the
    compose and Dockerfile copies are the same bytes;
  * an auth redirect built for a loopback request stays on loopback, so a redirect-following
    probe (ex; BusyBox wget) lands on the sign-in page at 127.0.0.1 without a backend call.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.prod.yml"
DOCKERFILE = ROOT / "frontend" / "Dockerfile"
MIDDLEWARE = ROOT / "frontend" / "src" / "middleware.ts"
AUTH = ROOT / "frontend" / "src" / "auth.ts"
AUTH_REDIRECT = ROOT / "frontend" / "src" / "lib" / "auth-redirect.ts"
HEALTHZ_ROUTE = ROOT / "frontend" / "src" / "app" / "healthz" / "route.ts"

_DURATION = re.compile(r"^(\d+)(ms|s|m)$")


def _seconds(value: str) -> float:
    m = _DURATION.match(value.strip())
    assert m, f"unparseable duration {value!r}"
    n, unit = int(m.group(1)), m.group(2)
    return n / 1000 if unit == "ms" else n * 60 if unit == "m" else n


def _service_block(compose: str, name: str) -> str:
    lines = compose.splitlines()
    start = next(i for i, l in enumerate(lines) if l == f"  {name}:")
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^  [A-Za-z_-]+:\s*$", lines[i])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _compose_healthcheck(block: str) -> dict:
    hc = block.split("    healthcheck:", 1)[1]
    out = {}
    for key in ("test", "interval", "timeout", "retries", "start_period"):
        m = re.search(rf"^\s+{key}:\s*(.+)$", hc, re.M)
        assert m, f"frontend healthcheck has no {key}"
        out[key] = m.group(1).strip()
    return out


def _compose_probe_argv(test_line: str) -> list:
    # `test: ["CMD", "node", "-e", "<js>"]` where the JS carries single quotes only.
    assert test_line.startswith("[") and test_line.endswith("]"), test_line
    return re.findall(r'"((?:[^"\\]|\\.)*)"', test_line)


def _dockerfile_probe_argv(dockerfile: str) -> list:
    m = re.search(r'CMD \[(.+)\]\s*$', dockerfile, re.M)
    assert m, "Dockerfile HEALTHCHECK has no exec-form CMD"
    return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))


class TheFrontendProbeNeverLeavesTheBox(unittest.TestCase):
    def setUp(self):
        self.compose = COMPOSE.read_text()
        self.frontend = _service_block(self.compose, "frontend")
        self.hc = _compose_healthcheck(self.frontend)
        self.argv = _compose_probe_argv(self.hc["test"])

    def test_the_probe_targets_the_liveness_route(self):
        self.assertEqual(self.argv[:3], ["CMD", "node", "-e"])
        js = self.argv[3]
        self.assertIn("path:'/healthz'", js)
        self.assertIn("host:'127.0.0.1'", js)
        self.assertNotIn("/health'", js.replace("/healthz'", ""), "readiness must not be the liveness probe")

    def test_the_probe_client_does_not_follow_redirects(self):
        js = self.argv[3]
        self.assertIn("require('http').get(", js)
        self.assertIn("r.statusCode===200?0:1", js, "anything but 200, a 3xx included, must fail the probe")
        for banned in ("wget", "curl", "follow", "redirect"):
            self.assertNotIn(banned, self.hc["test"].lower())

    def test_the_probe_has_an_internal_timeout(self):
        js = self.argv[3]
        self.assertRegex(js, r"timeout:\d+")
        self.assertIn(".on('timeout',()=>process.exit(1))", js)
        self.assertIn(".on('error',()=>process.exit(1))", js)
        internal_ms = int(re.search(r"timeout:(\d+)", js).group(1))
        self.assertLessEqual(internal_ms / 1000, _seconds(self.hc["timeout"]))

    def test_retries_times_interval_covers_a_minute(self):
        # Autoheal acts on Docker's `unhealthy`, which needs `retries` consecutive failures.
        # Below a minute a single slow GC pause could cost a restart.
        self.assertGreaterEqual(int(self.hc["retries"]) * _seconds(self.hc["interval"]), 60)
        self.assertGreaterEqual(_seconds(self.hc["start_period"]), 60)

    def test_the_compose_and_dockerfile_probes_agree(self):
        dockerfile = DOCKERFILE.read_text()
        self.assertEqual(_dockerfile_probe_argv(dockerfile), self.argv[1:])
        m = re.search(r"^HEALTHCHECK (.+?)\\?$", dockerfile, re.M)
        assert m, "no HEALTHCHECK in the frontend Dockerfile"
        flags = dict(re.findall(r"--([a-z-]+)=(\S+)", m.group(1)))
        self.assertEqual(_seconds(flags["interval"]), _seconds(self.hc["interval"]))
        self.assertEqual(_seconds(flags["timeout"]), _seconds(self.hc["timeout"]))
        self.assertEqual(int(flags["retries"]), int(self.hc["retries"]))
        self.assertEqual(_seconds(flags["start-period"]), _seconds(self.hc["start_period"]))

    def test_the_liveness_route_is_exempt_from_the_auth_middleware(self):
        source = MIDDLEWARE.read_text()
        m = re.search(r'matcher:\s*\[\s*(?:/\*.*?\*/\s*)?"(/\(\(\?!.+?\)\.\*\))"', source, re.S)
        assert m, "middleware matcher not found"
        pattern = m.group(1).replace("\\\\", "\\")
        self.assertIsNone(re.fullmatch(pattern, "/healthz"), "/healthz must not run the auth middleware")
        self.assertIsNone(re.fullmatch(pattern, "/health"))
        self.assertIsNotNone(re.fullmatch(pattern, "/inventory/instances"), "pages still match")

    def test_the_liveness_route_calls_nothing(self):
        source = HEALTHZ_ROUTE.read_text()
        self.assertIn('export const dynamic = "force-dynamic"', source)
        for banned in ("fetch(", "BACKEND_URL", "auth(", "redirect("):
            self.assertNotIn(banned, source)
        self.assertIn("no-store", source)

    def test_every_auth_redirect_stays_on_the_requesting_loopback_host(self):
        auth = AUTH.read_text()
        self.assertNotIn('new URL("/auth/signin", nextUrl.origin)', auth)
        self.assertNotIn('new URL("/", nextUrl)', auth)
        self.assertIn('request.headers.get("host")', auth)
        redirects = re.findall(r"Response\.redirect\(\s*(\w+)\(", auth)
        self.assertEqual(redirects, ["redirectTo"] * 3, "three redirects, all through the helper")
        helper = AUTH_REDIRECT.read_text()
        self.assertIn("127", helper)
        self.assertIn("localhost", helper)
        self.assertIn("nextUrl.origin", helper, "a non-loopback host keeps the app origin")
        self.assertNotIn("nextUrl.href", auth, "callbackUrl is relative, so it resolves on whichever origin signs in")


if __name__ == "__main__":
    unittest.main()
