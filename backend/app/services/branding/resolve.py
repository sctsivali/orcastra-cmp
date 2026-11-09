"""One pure resolver: the branding an account sees, from the layers that apply to it.

Layers, lowest first: the deployment default, the organization that sold to the account's
organization (one hop, never its provider's provider), and the organization's own row. A later
layer fills or replaces what an earlier one set, key by key, so a buyer that only changed its
colours keeps its seller's logo. An empty value (None or "") never replaces anything. The
built-in look under all of them is the frontend's DEFAULT_PERSONALIZATION, and an account's own
theme and illustrations choices are laid over the top by the frontend (user-preferences), so
neither is here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

SECTIONS = ("logo", "background", "branding", "colors")
# Single values that a later layer replaces whole, as long as it sets them.
CHOICES = ("theme", "illustrations")


@dataclass(frozen=True)
class Layer:
    kind: str  # deployment | provider | organization
    settings: Dict[str, Any]
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    organization_id: Optional[int] = None


@dataclass(frozen=True)
class Resolved:
    settings: Dict[str, Any]
    # The layer whose values are on top, or "default" when no layer holds anything.
    source: str
    version: str
    updated_at: Optional[datetime]
    layers: Tuple[Layer, ...] = field(default_factory=tuple)


def _filled(value: Any) -> bool:
    return value is not None and value != ""


def merge(layers: Iterable[Layer]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for layer in layers:
        settings = layer.settings or {}
        for section in SECTIONS:
            values = settings.get(section)
            if isinstance(values, dict):
                filled = {key: value for key, value in values.items() if _filled(value)}
                if filled:
                    out[section] = {**out.get(section, {}), **filled}
        for key in CHOICES:
            if _filled(settings.get(key)):
                out[key] = settings[key]
    return out


def version_token(subject: str, layers: Iterable[Layer]) -> str:
    """Changes whenever any layer, the link between them or the subject changes; nothing else."""
    parts = [subject]
    for layer in layers:
        parts.append(
            f"{layer.kind}:{layer.organization_id or ''}:"
            f"{layer.updated_at.isoformat() if layer.updated_at else ''}"
        )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]


def resolve(subject: str, layers: Iterable[Layer]) -> Resolved:
    present = tuple(layer for layer in layers if layer.settings)
    stamps = [layer.updated_at for layer in present if layer.updated_at]
    return Resolved(
        settings=merge(present),
        source=present[-1].kind if present else "default",
        version=version_token(subject, present),
        updated_at=max(stamps) if stamps else None,
        layers=present,
    )
