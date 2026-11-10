"""One way to print a size: decimal, and labelled decimal, as the console does.

This is only for text a person reads. LXD receives whatever spelling it was given, and the
parsers accept every one of them.
"""

from decimal import Decimal

_UNITS = (("PB", 10**15), ("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("kB", 10**3))


def format_size(num_bytes: float) -> str:
    """``8589934592`` as ``8.59 GB``, the same text the console's formatBytes gives."""
    value = max(0, int(num_bytes))
    for unit, factor in _UNITS:
        if value >= factor:
            rendered = f"{Decimal(value) / Decimal(factor):.2f}".rstrip("0").rstrip(".")
            return f"{rendered} {unit}"
    return f"{value} B"
