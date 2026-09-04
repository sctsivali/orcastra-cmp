"""What the proxy operation vocabulary calls. One place where a desktop request becomes an action.

Runs on the LXD service's blocking side, like `exec_instance` and `get_file`, because everything
underneath it is pylxd. The router hands it an instance and validated params and gets back a plain
dict, which is the same contract the rest of that vocabulary keeps.
"""

import logging
import time
from dataclasses import asdict
from typing import Any, Dict, Optional

from fastapi import HTTPException

from .actions import CAPTURE_ACTIONS, CLIPBOARD_ACTIONS, INPUT_ACTIONS, build_action
from .guest.provider import GuestExecProvider
from .protocol import ActionResult, DesktopProvider, ProbeReport, Target
from .runtime import DESKTOP_CONTROL_ENABLED
from . import session_store

logger = logging.getLogger(__name__)

__all__ = [
    "OPERATION_CAPABILITY",
        "desktop_action",
    "desktop_probe",
]

# One proxy operation per capability, rather than one `desktop_action` operation covering all of
# them.
#
# important: GUEST_ACCESS_OPERATIONS maps an operation to exactly ONE capability, and it is what
# the router enforces. A single operation whose real cost varied with an `action` field would be
# gated at whatever that map named, so a caller granted only desktop.view could send
# `action: "type"` and get input for free. Splitting the operation is what makes the static map
# tell the truth, and `_assert_action_matches` closes the same hole from the other side.
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

    `wait` is the only member of the last group: it never reaches the guest, so it neither reads
    nor touches anything and pricing it would be theatre.
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

    One function rather than a constructor call at each site, because it is the seam a second
    provider arrives through. A framebuffer provider for guests that cannot run capture tooling
    is the documented gap, and it becomes a branch here rather than an edit at every call.
    """
    return GuestExecProvider(inst)


def _report_for(inst: Any, target: Target, *, refresh: bool = False) -> ProbeReport:
    if not refresh:
        cached = session_store.load(target)
        if cached is not None:
            return cached
    report = _provider(inst).probe(target, agent=_agent_present(inst))
    session_store.store(target, report)
    return report


def desktop_probe(inst: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """What this guest can do, and what is missing when it cannot."""
    _require_enabled()
    target = _target(params, inst)
    report = _report_for(inst, target, refresh=bool(params.get("refresh")))
    payload = asdict(report)
    payload["session_id"] = session_store.session_id(target)
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


def desktop_action(inst: Any, params: Dict[str, Any], operation: str = "desktop_view") -> Dict[str, Any]:
    """Perform one desktop action, re-checking the guest's readiness first."""
    _require_enabled()
    action = build_action(str(params.get("action") or ""), params)
    _assert_action_matches(operation, action.name)
    target = _target(params, inst)

    if action.name == "wait":
        # note: served here and never sent to the guest. A wait that occupies a pool thread is a
        # self-inflicted denial of service, and there is nothing for the guest to do anyway.
        time.sleep(action.duration)
        return {"ok": True, "action": "wait", "waited_seconds": action.duration}

    report = _report_for(inst, target)
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
