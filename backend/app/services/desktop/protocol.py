"""The shapes a desktop provider speaks, and nothing about how any provider works.

The point of the abstraction is that the MCP tool surface depends only on `Action` and
`ActionResult`. A second provider - a framebuffer one for guests that cannot run capture tooling,
which is the documented gap - is then a new file and a selection rule, and changes nothing a model
sees.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

__all__ = [
    "Action",
    "ActionResult",
    "DesktopProvider",
    "Display",
    "ProbeReport",
    "Target",
]


@dataclass(frozen=True)
class Target:
    """Which guest. Never model-supplied: the MCP server holds this binding.

    important: threading cluster, project and instance through the model on every action is three
    fields it will eventually get wrong, and getting them wrong means acting inside somebody
    else's guest.
    """

    cluster_id: str
    project: str
    instance: str

    @property
    def key(self) -> str:
        return f"{self.cluster_id}|{self.project}|{self.instance}"


@dataclass(frozen=True)
class Display:
    """The geometry a coordinate is measured against.

    `generation` changes whenever a capture observes a different geometry. A coordinate action may
    quote the generation it was planning against, and a stale one is refused rather than clicked
    somewhere arbitrary.
    """

    width: int
    height: int
    scale: float = 1.0
    generation: int = 1

    def to_guest(self, x: int, y: int) -> tuple[int, int]:
        """A screenshot coordinate in guest pixels.

        The model always works in screenshot pixels and is never told about scale. When a capture
        was downscaled, this is where that is undone.
        """
        if self.scale in (0, 1.0):
            return x, y
        return int(round(x / self.scale)), int(round(y / self.scale))

    def to_screenshot(self, x: int, y: int) -> tuple[int, int]:
        """A guest coordinate in screenshot pixels: the inverse, and it must exist.

        important: anything READ from the guest comes back in guest pixels, and everything the
        model sends is in screenshot pixels. Reporting one in the other's units is a bug that only
        appears when the two differ, so it survives every test on a display that happens to match
        the target. Measured on a 1920x1080 guest: a pointer moved to screenshot (400,300) landed
        correctly at guest (600,450) and was then reported back as (600,450), which the model would
        read as the pointer having moved somewhere it did not.
        """
        if self.scale in (0, 1.0):
            return x, y
        return int(round(x * self.scale)), int(round(y * self.scale))


@dataclass(frozen=True)
class Action:
    """One validated, provider-agnostic desktop action."""

    name: str
    coordinate: Optional[tuple[int, int]] = None
    end_coordinate: Optional[tuple[int, int]] = None
    text: Optional[str] = None
    modifiers: List[str] = field(default_factory=list)
    amount: int = 1
    direction: Optional[str] = None
    duration: float = 0.0
    region: Optional[tuple[int, int, int, int]] = None
    display_generation: Optional[int] = None


@dataclass
class ActionResult:
    """What an action produced, in the one envelope every action shares."""

    ok: bool
    action: str
    display: Optional[Display] = None
    image_base64: Optional[str] = None
    image_media_type: Optional[str] = None
    image_bytes: int = 0
    cursor: Optional[tuple[int, int]] = None
    detail: Optional[str] = None
    error_code: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeReport:
    """What the guest can and cannot do, and what is missing when it cannot."""

    ready: bool
    agent: bool
    session_type: str = "none"
    compositor: Optional[str] = None
    session_user: Optional[str] = None
    display: Optional[Display] = None
    capture_tool: Optional[str] = None
    input_tool: Optional[str] = None
    # Probed rather than assumed. The Ubuntu noble xdotool manpage documents neither
    # `getdisplaygeometry` nor `type --file`, while Debian's documents the latter, so version
    # drift across distributions is real and each is tested once at probe time.
    supports_type_file: bool = False
    supports_display_geometry: bool = False
    missing: List[str] = field(default_factory=list)
    install_hint: Optional[str] = None
    reason: Optional[str] = None
    environment: Dict[str, str] = field(default_factory=dict)


class DesktopProvider(Protocol):
    """Deliberately two methods. Anything wider becomes a provider-specific surface by accident."""

    name: str

    def probe(self, target: Target) -> ProbeReport: ...

    def perform(self, target: Target, action: Action, report: ProbeReport) -> ActionResult: ...
