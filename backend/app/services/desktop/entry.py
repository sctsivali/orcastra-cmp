"""What the proxy operation vocabulary calls. One place where a desktop request becomes an action.

Runs on the LXD service's blocking side, like `exec_instance` and `get_file`, because everything
underneath it is pylxd. The router hands it an instance and validated params and gets back a plain
dict, which is the same contract the rest of that vocabulary keeps.
"""

import asyncio
import logging
import time
from dataclasses import asdict
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException

from .actions import CAPTURE_ACTIONS, CLIPBOARD_ACTIONS, INPUT_ACTIONS, build_action
from .guest.provider import GuestExecProvider
from .protocol import ActionResult, DesktopProvider, ProbeReport, Target
from app.core.blocking import BlockingPoolFull

from .runtime import DESKTOP_CONTROL_ENABLED, DESKTOP_MAX_IN_FLIGHT
from . import session_store

logger = logging.getLogger(__name__)

__all__ = [
    "OPERATION_CAPABILITY",
    "desktop_action",
    "desktop_probe",
    "serve_wait",
    "wait_seconds",
]

# Waits in progress on this worker. Waits run on the event loop and the per-instance budget is
# keyed on a caller-chosen name, so without this bound one key with consent and a grant could
# hold an HTTP slot per made-up name. Same ceiling as the pool.
_WAIT_SLOTS = asyncio.Semaphore(DESKTOP_MAX_IN_FLIGHT)

# One proxy operation per capability.
#
# important: GUEST_ACCESS_OPERATIONS maps an operation to exactly one capability, and the router
# enforces that map. One operation whose cost varied with an `action` field would let a caller
# granted only desktop.view send `action: "type"` and get input. `_assert_action_matches` closes
# the same hole from the other side.
OPERATION_CAPABILITY = {
    "desktop_probe": "desktop.view",
    "desktop_view": "desktop.view",
    "desktop_input": "desktop.input",
    "desktop_clipboard": "desktop.clipboard",
}

# Which capability each family of actions costs. Derived from the action sets rather than a third
# list, so an action added to a set cannot be left unpriced.
_FAMILY_CAPABILITY = (
    (CAPTURE_ACTIONS, "desktop.view"),
    (INPUT_ACTIONS, "desktop.input"),
    (CLIPBOARD_ACTIONS, "desktop.clipboard"),
)


def _capability_for_action(name: str) -> Optional[str]:
    """The capability this action costs, or None for one that costs nothing.

    `wait` is the only action without one: it never reaches the guest, so it neither reads nor
    touches anything.
    """
    for family, capability in _FAMILY_CAPABILITY:
        if name in family:
            return capability
    return None


def _require_enabled() -> None:
    if not DESKTOP_CONTROL_ENABLED:
        raise HTTPException(
            status_code=403,
            detail=(
                "desktop control is disabled on this deployment. It photographs and types into a "
                "customer's screen, so it is off until an operator sets "
                "ORCASTRA_DESKTOP_CONTROL_ENABLED."
            ),
        )


def _target(params: Dict[str, Any], inst: Any) -> Target:
    return Target(
        cluster_id=str(params.get("cluster_id") or ""),
        project=str(params.get("project") or "default"),
        instance=str(getattr(inst, "name", params.get("name") or "")),
    )


def _agent_present(inst: Any) -> bool:
    """A VM with no agent can neither exec nor read files, so there is nothing to try.

    Read from the instance state the caller already has rather than by attempting a command: a
    doomed exec would burn the whole budget before failing, and the failure would say nothing
    about why.
    """
    state = getattr(inst, "state", None)
    try:
        processes = state().processes if callable(state) else getattr(state, "processes", 0)
    except Exception:  # noqa: BLE001 - a container reports differently and is always fine
        return True
    return processes != -1


def _provider(inst: Any) -> DesktopProvider:
    """The provider for this guest.

    A second provider, ex; a framebuffer provider for guests that cannot run capture tooling,
    becomes a branch here instead of an edit at every call site.
    """
    return GuestExecProvider(inst)


def _report_for(
    inst: Any, target: Target, *, refresh: bool = False
) -> Tuple[ProbeReport, bool]:
    """The probe for this guest, and whether this call established the session.

    The second value lets the router write one session row per session instead of one per action,
    so a tenant's session list shows that an agent connected rather than every mouse move.
    """
    if not refresh:
        cached = session_store.load(target)
        if cached is not None:
            return cached, False
    report = _provider(inst).probe(target, agent=_agent_present(inst))
    session_store.store(target, report)
    return report, True


def desktop_probe(inst: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """What this guest can do, and what is missing when it cannot."""
    _require_enabled()
    target = _target(params, inst)
    report, started = _report_for(inst, target, refresh=bool(params.get("refresh")))
    payload = asdict(report)
    payload["session_id"] = session_store.session_id(target)
    payload["session_started"] = started
    payload["provider"] = _provider(inst).name
    return payload


def _assert_action_matches(operation: str, action_name: str) -> None:
    """The action a caller asked for must be one this operation is priced for."""
    required = _capability_for_action(action_name)
    if required is None:
        return
    allowed = OPERATION_CAPABILITY.get(operation)
    if required != allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{action_name} costs {required}, which {operation} is not priced for. Send it as "
                f"the operation that charges {required}."
            ),
        )


def wait_seconds(operation: str, params: Dict[str, Any]) -> Optional[float]:
    """How long a `wait` asks for, validated as `desktop_action` validates it; None otherwise.

    important: the proxy route serves a wait on the event loop with this answer instead of handing
    it to `desktop_action` on the desktop pool. That pool has four workers, so four agents waiting
    the maximum there would refuse every other desktop action on the deployment.
    """
    if operation not in ("desktop_view", "desktop_input", "desktop_clipboard"):
        return None
    if str(params.get("action") or "") != "wait":
        return None
    _require_enabled()
    return build_action("wait", params).duration


async def serve_wait(seconds: float) -> Dict[str, Any]:
    """A validated wait, served on the event loop and bounded per worker.

    Raises BlockingPoolFull when the worker already holds its share, which the proxy route answers
    as `overloaded`, as it answers a full desktop pool.
    """
    if _WAIT_SLOTS.locked():
        raise BlockingPoolFull("desktop wait", DESKTOP_MAX_IN_FLIGHT)
    async with _WAIT_SLOTS:
        await asyncio.sleep(seconds)
    return {"ok": True, "action": "wait", "waited_seconds": seconds}


def desktop_action(inst: Any, params: Dict[str, Any], operation: str = "desktop_view") -> Dict[str, Any]:
    """Perform one desktop action, re-checking the guest's readiness first."""
    _require_enabled()
    action = build_action(str(params.get("action") or ""), params)
    _assert_action_matches(operation, action.name)
    target = _target(params, inst)

    if action.name == "wait":
        # Never sent to the guest. The proxy route answers a wait itself (`wait_seconds`), so
        # only a caller that skipped that reaches this, and sleeping is still the right answer.
        time.sleep(action.duration)
        return {"ok": True, "action": "wait", "waited_seconds": action.duration}

    report, started = _report_for(inst, target)
    if not report.ready:
        return {
            "ok": False,
            "action": action.name,
            "error_code": "desktop_not_ready",
            "detail": report.reason or "this guest is not prepared for desktop control",
            "missing": report.missing,
            "install_hint": report.install_hint,
        }

    if action.display_generation is not None and report.display is not None:
        if action.display_generation != report.display.generation:
            return {
                "ok": False,
                "action": action.name,
                "error_code": "desktop_display_changed",
                "detail": (
                    "the display changed since that screenshot, so this coordinate would land "
                    "somewhere arbitrary. Take another screenshot."
                ),
                "display": asdict(report.display),
            }

    result: ActionResult = _provider(inst).perform(target, action, report)

    # A capture that observed a different geometry updates the cached session, so the next
    # coordinate is measured against what is on screen now.
    if result.display is not None and result.ok:
        previous = report.display
        if previous is None or (previous.width, previous.height) != (
            result.display.width, result.display.height
        ):
            report.display = result.display
            session_store.store(target, report)

    payload: Dict[str, Any] = {
        "ok": result.ok,
        "action": result.action,
        "session_id": session_store.session_id(target),
        "session_started": started,
    }
    if result.display is not None:
        payload["display"] = asdict(result.display)
    if result.error_code:
        payload["error_code"] = result.error_code
    if result.detail:
        payload["detail"] = result.detail
    if result.cursor is not None:
        payload["cursor"] = list(result.cursor)
    if result.image_base64 is not None:
        payload["image"] = {
            "media_type": result.image_media_type,
            "data": result.image_base64,
            "bytes": result.image_bytes,
        }
    payload.update(result.extra)
    return payload
