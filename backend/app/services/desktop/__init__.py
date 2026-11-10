"""Desktop control: letting an agent see and drive a guest's graphical session.

Kept outside services/lxd/: the package is built around a provider abstraction, and a framebuffer
provider for guests that cannot run capture tooling is not an LXD-API concern.

The package never installs anything into a guest and never decodes an image inside the control
plane. Captures stream through a pipeline; only the KDE `spectacle` chain needs a temporary file in
the guest, which its shell trap removes.
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
