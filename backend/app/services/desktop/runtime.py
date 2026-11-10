"""Pools, budgets and caps for desktop control, kept beside the code that spends them.

As in `services/lxd/proxy.py` for the proxy surface, each tunable sits next to the reason for its
value, so whoever changes it can see what it protects.

important: desktop control has its own thread pool, never `exec_executor`. That pool is private
so a runaway exec only affects other execs, and it has four workers; a screenshot loop at a modest
rate would fill all four and stall every other integration's `exec_instance`.
"""

import os
from concurrent.futures import ThreadPoolExecutor

__all__ = [
    "DESKTOP_ACTIONS_PER_MINUTE",
    "DESKTOP_ACTION_TIMEOUT",
    "DESKTOP_CONTROL_ENABLED",
    "DESKTOP_IMAGE_SOFT_LIMIT",
    "DESKTOP_MAX_IN_FLIGHT",
    "DESKTOP_MAX_LONG_EDGE",
    "DESKTOP_MAX_VISUAL_TOKENS",
    "DESKTOP_PROBE_TIMEOUT",
    "DESKTOP_SCREENSHOTS_PER_MINUTE",
    "DESKTOP_SCREENSHOT_TIMEOUT",
    "DESKTOP_SESSION_MAX_AGE",
    "DESKTOP_SESSION_TTL",
    "DESKTOP_STDERR_LIMIT",
    "DESKTOP_STDOUT_LIMIT",
    "DESKTOP_TARGET_HEIGHT",
    "DESKTOP_TARGET_WIDTH",
    "DESKTOP_TYPE_MAX",
    "DESKTOP_WAIT_MAX",
    "desktop_executor",
]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# On by default, with an off switch for an operator who wants one.
#
# important: this flag is not the consent boundary. Four checks stand between an agent and a
# customer's screen: the desktop.* capability, which no application type seeds and no key issued
# before capabilities existed inherits; the per-cluster grant; the project allow-list; and the
# owning organization's row in desktop_control_policy, which defaults to refusing and is the
# tenant's decision.
DESKTOP_CONTROL_ENABLED = _env_bool("ORCASTRA_DESKTOP_CONTROL_ENABLED", True)

# ── budgets ────────────────────────────────────────────────────────────────────────────────────
# A click that takes 15s means the guest is wedged, and waiting longer only holds the worker.
DESKTOP_ACTION_TIMEOUT = _env_float("ORCASTRA_DESKTOP_ACTION_TIMEOUT", 15.0)
# Capture plus encode plus several megabytes on the wire.
DESKTOP_SCREENSHOT_TIMEOUT = _env_float("ORCASTRA_DESKTOP_SCREENSHOT_TIMEOUT", 30.0)
# Several discovery commands in one shell.
DESKTOP_PROBE_TIMEOUT = _env_float("ORCASTRA_DESKTOP_PROBE_TIMEOUT", 30.0)

# ── the pool ───────────────────────────────────────────────────────────────────────────────────
DESKTOP_POOL_WORKERS = _env_int("ORCASTRA_DESKTOP_POOL_WORKERS", 4)
# Equal to the worker count, as for the exec pool: these calls are long, so shedding is better
# than queueing, and a queued screenshot is stale before it runs.
DESKTOP_MAX_IN_FLIGHT = _env_int("ORCASTRA_DESKTOP_MAX_IN_FLIGHT", DESKTOP_POOL_WORKERS)

desktop_executor = ThreadPoolExecutor(
    max_workers=DESKTOP_POOL_WORKERS, thread_name_prefix="desktop_control"
)

# ── transport caps ─────────────────────────────────────────────────────────────────────────────
# 4 MiB of base64 is 3 MiB of image, comfortably above the 2 MiB budget below, so truncation is a
# bug signal rather than a normal outcome. Worst case memory is this times DESKTOP_MAX_IN_FLIGHT
# times the worker count.
DESKTOP_STDOUT_LIMIT = _env_int("ORCASTRA_DESKTOP_STDOUT_LIMIT", 4 * 1024 * 1024)
# Geometry plus tool warnings. Nothing legitimate is larger, and an unbounded stderr is the same
# memory hole as an unbounded stdout.
DESKTOP_STDERR_LIMIT = _env_int("ORCASTRA_DESKTOP_STDERR_LIMIT", 64 * 1024)
# Above this the capture chain re-encodes as JPEG rather than shipping a large PNG.
DESKTOP_IMAGE_SOFT_LIMIT = _env_int("ORCASTRA_DESKTOP_IMAGE_SOFT_LIMIT", 2 * 1024 * 1024)

# ── what a vision model accepts ────────────────────────────────────────────────────────────────
# A computer-use tool rejects a screenshot over the model's limits instead of downscaling it, so
# the platform has to guarantee the bound.
#
# Visual tokens are ceil(w/28) * ceil(h/28). The standard tier accepts a long edge up to 1568 and
# up to 1568 visual tokens, and every current model accepts that tier. Targeting it instead of the
# wider tier newer models offer keeps a capture valid on every model.
DESKTOP_MAX_LONG_EDGE = _env_int("ORCASTRA_DESKTOP_MAX_LONG_EDGE", 1568)
DESKTOP_MAX_VISUAL_TOKENS = _env_int("ORCASTRA_DESKTOP_MAX_VISUAL_TOKENS", 1568)
# 1280x800 is 46 x 29 = 1334 visual tokens with a 1280 long edge. Fits the standard tier with room,
# and is a common desktop geometry.
DESKTOP_TARGET_WIDTH = _env_int("ORCASTRA_DESKTOP_TARGET_WIDTH", 1280)
DESKTOP_TARGET_HEIGHT = _env_int("ORCASTRA_DESKTOP_TARGET_HEIGHT", 800)

# ── per-target rate limits ─────────────────────────────────────────────────────────────────────
# Keyed per (key, cluster, project, instance) instead of per key, so one runaway agent on one
# guest cannot starve another guest, and raising the per-key limit does not widen this.
DESKTOP_ACTIONS_PER_MINUTE = _env_int("ORCASTRA_DESKTOP_ACTIONS_PER_MINUTE", 60)
# Tighter than actions: a frame costs capture, encode, megabytes on the wire, and about
# 1334 vision tokens to read. One every three seconds is well above a human's reading speed.
DESKTOP_SCREENSHOTS_PER_MINUTE = _env_int("ORCASTRA_DESKTOP_SCREENSHOTS_PER_MINUTE", 20)

# ── session lifetime ───────────────────────────────────────────────────────────────────────────
# Expiry costs one re-probe, so a short TTL is a performance cost and never a correctness one.
DESKTOP_SESSION_TTL = _env_int("ORCASTRA_DESKTOP_SESSION_TTL", 900)
# Forces a re-probe regardless of activity, so a long-lived session cannot keep using an
# environment map for a login session the user has since ended.
DESKTOP_SESSION_MAX_AGE = _env_int("ORCASTRA_DESKTOP_SESSION_MAX_AGE", 4 * 3600)

# ── per-action input bounds ────────────────────────────────────────────────────────────────────
DESKTOP_TYPE_MAX = _env_int("ORCASTRA_DESKTOP_TYPE_MAX", 1024)
# A wait is served on the event loop and never reaches the guest. Ten seconds is longer than any
# UI settle worth waiting on in one call.
DESKTOP_WAIT_MAX = _env_float("ORCASTRA_DESKTOP_WAIT_MAX", 10.0)
