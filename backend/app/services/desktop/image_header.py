"""Reading an image's geometry from its container header, without decoding it.

A computer-use tool result over a vision model's limits is rejected with a validation error instead
of being downscaled, so the platform has to know a screenshot's size before returning it.

important: only the header is parsed, and Pillow is not a dependency. The bytes come from a guest
that may be compromised, and a decoder in the control plane would be the first parsing surface from
a guest into this process; reading two integers at a fixed offset cannot execute anything. Resizing
therefore happens in the guest, where the pixels and the tools already are.
"""

import logging
import math
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["ImageGeometry", "read_geometry", "within_model_limits"]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# SOF markers that carry dimensions. SOF4 (0xC4) is a Huffman table and SOF8/SOFC are not frames,
# so they are excluded rather than swept up by a range.
_JPEG_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


class ImageGeometry(tuple):
    """A (width, height) pair that also knows what it costs a model to look at."""

    __slots__ = ()

    def __new__(cls, width: int, height: int) -> "ImageGeometry":
        return super().__new__(cls, (width, height))

    @property
    def width(self) -> int:
        return self[0]

    @property
    def height(self) -> int:
        return self[1]

    @property
    def long_edge(self) -> int:
        return max(self.width, self.height)

    @property
    def visual_tokens(self) -> int:
        return _visual_tokens(self.width, self.height)


def _visual_tokens(width: int, height: int) -> int:
    """What a model spends to read an image of this size.

    The published formula ceil(w/28) * ceil(h/28), computed exactly so the refusal it feeds is
    right at the boundary.
    """
    if width <= 0 or height <= 0:
        return 0
    return math.ceil(width / 28) * math.ceil(height / 28)


def _png_geometry(payload: bytes) -> Optional[ImageGeometry]:
    # IHDR is the first chunk and is fixed-length, so width and height sit at known offsets.
    if len(payload) < 24 or not payload.startswith(_PNG_SIGNATURE):
        return None
    if payload[12:16] != b"IHDR":
        return None
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return ImageGeometry(width, height)


def _jpeg_geometry(payload: bytes) -> Optional[ImageGeometry]:
    if len(payload) < 4 or payload[0] != 0xFF or payload[1] != 0xD8:
        return None
    index = 2
    limit = len(payload)
    # Walk the marker chain rather than scanning for a byte pattern: 0xFFC0 occurs inside entropy
    # coded data often enough that a scan finds a false frame header on ordinary photographs.
    while index + 3 < limit:
        if payload[index] != 0xFF:
            index += 1
            continue
        marker = payload[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xFF:
            index += 1
            continue
        if index + 3 >= limit:
            return None
        segment_length = int.from_bytes(payload[index + 2 : index + 4], "big")
        if segment_length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if index + 9 > limit:
                return None
            height = int.from_bytes(payload[index + 5 : index + 7], "big")
            width = int.from_bytes(payload[index + 7 : index + 9], "big")
            if width <= 0 or height <= 0:
                return None
            return ImageGeometry(width, height)
        index += 2 + segment_length
    return None


def read_geometry(payload: bytes) -> Optional[ImageGeometry]:
    """The image's dimensions, or None when the bytes are not a container this understands.

    A caller must treat None as an image it cannot vouch for and refuse, instead of passing
    unverified bytes to a model.
    """
    if not payload:
        return None
    return _png_geometry(payload) or _jpeg_geometry(payload)


def within_model_limits(
    geometry: ImageGeometry, *, max_long_edge: int, max_visual_tokens: int
) -> Tuple[bool, str]:
    """Whether this image may be returned, and why not when it may not.

    Both bounds, because they fail independently: a 2000x200 strip is under the token budget and
    over the edge limit, and a 1500x1500 square is the other way round.
    """
    if geometry.long_edge > max_long_edge:
        return False, (
            f"the guest's display is {geometry.width}x{geometry.height}, whose long edge "
            f"({geometry.long_edge}px) exceeds the {max_long_edge}px a screenshot may be. Set the "
            f"guest's resolution lower, or install a capture tool that can resize."
        )
    if geometry.visual_tokens > max_visual_tokens:
        return False, (
            f"the guest's display is {geometry.width}x{geometry.height}, which costs "
            f"{geometry.visual_tokens} visual tokens against a limit of {max_visual_tokens}. Set "
            f"the guest's resolution lower, or install a capture tool that can resize."
        )
    return True, ""
