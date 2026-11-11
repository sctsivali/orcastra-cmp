"""One way to print a size, in the reader's byte units: decimal (GB) or binary (GiB).

This is only for text a person reads. LXD receives whatever spelling it was given, and the
parsers accept every one of them. The console sends the account's choice as X-Byte-Units
(middleware/byte_units.py); a caller that sends nothing reads decimal, as it always has.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from decimal import Decimal
from typing import Mapping, Optional

DECIMAL = "decimal"
BINARY = "binary"
BYTE_UNITS_HEADER = "x-byte-units"

_UNITS = {
    DECIMAL: (("PB", 10**15), ("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("kB", 10**3)),
    BINARY: (("PiB", 1024**5), ("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)),
}

# Per request: contextvars keep one request's choice away from every other request in flight.
_reader_units: ContextVar[str] = ContextVar("reader_byte_units", default=DECIMAL)

_SIZE_TOKEN = re.compile(r"\{(\w+)\}")


def byte_units_from_header(value: Optional[str]) -> str:
    """Exactly `binary` reads binary; anything else, absent included, reads decimal."""
    return BINARY if value == BINARY else DECIMAL


def use_byte_units(units: str) -> Token:
    return _reader_units.set(units if units in _UNITS else DECIMAL)


def reset_byte_units(token: Token) -> None:
    _reader_units.reset(token)


def format_size(num_bytes: float, units: Optional[str] = None) -> str:
    """``8589934592`` as ``8.59 GB`` or ``8 GiB``, the same text the console's formatBytes gives.

    Without ``units`` the reader's choice applies. Pass a system for text that is stored or hashed
    and must not depend on who asked.
    """
    scale = _UNITS.get(units or _reader_units.get(), _UNITS[DECIMAL])
    value = max(0, int(num_bytes))
    for unit, factor in scale:
        if value >= factor:
            rendered = f"{Decimal(value) / Decimal(factor):.2f}".rstrip("0").rstrip(".")
            return f"{rendered} {unit}"
    return f"{value} B"


def render_sized(
    template: str,
    sizes: Mapping[str, Optional[float]],
    units: Optional[str] = None,
    missing: str = "unknown",
    texts: Optional[Mapping[str, str]] = None,
) -> str:
    """``template`` with each ``{name}`` that ``sizes`` holds printed as a size.

    For stored text that is shown later: the template and the byte counts are kept, and each reader
    gets the sizes in their own units. Only names in ``sizes`` or ``texts`` are replaced; any other
    brace text is left exactly as written.

    important: a name from outside (a pool, a path) goes in ``texts``, never into the template. One
    pass inserts it, and an inserted value is not scanned again, so a pool named ``{free}`` stays
    a name.
    """

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name in sizes:
            value = sizes[name]
            return missing if value is None else format_size(value, units)
        if texts is not None and name in texts:
            return texts[name]
        return match.group(0)

    return _SIZE_TOKEN.sub(replace, template)
