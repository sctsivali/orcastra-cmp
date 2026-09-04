"""Desktop control: letting an agent see and drive a guest's graphical session.

Deliberately NOT under services/lxd/. The provider abstraction is the point of the package, and a
framebuffer provider for guests that cannot run capture tooling is not an LXD-API concern.

What this package does not do, stated here because the absence is a decision rather than an
omission: it never installs anything into a guest, it never writes a capture to the guest's
filesystem, and it never decodes an image inside the control plane.
"""

from .actions import ACTIONS, CAPTURE_ACTIONS, INPUT_ACTIONS, build_action
from .protocol import Action, ActionResult, Display, ProbeReport, Target

__all__ = [
    "ACTIONS",
    "CAPTURE_ACTIONS",
    "INPUT_ACTIONS",
    "Action",
    "ActionResult",
    "Display",
    "ProbeReport",
    "Target",
    "build_action",
]
