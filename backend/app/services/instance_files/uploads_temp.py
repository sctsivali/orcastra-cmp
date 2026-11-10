"""Names of the temporary files this service leaves beside a destination while writing it.

A temp lives in the destination's own directory so the final rename stays on one filesystem. Its
expiry is in its name because nothing else records it: a later upload to the same folder, or a
person pressing purge, removes one a crashed browser left behind. Nothing sweeps guests in the
background, because a system actor writing into a tenant's guest is the silent access this feature
refuses.
"""

import re
import secrets
from typing import Optional

TEMP_LIFETIME_SECONDS = 24 * 3600
_TEMP = re.compile(r"^\.orcastra-upload-(\d{10,12})-[0-9a-f]{16}\.part$")


def temp_name(expires_at: int) -> str:
    return f".orcastra-upload-{expires_at}-{secrets.token_hex(8)}.part"


def _expiry(name: str) -> Optional[int]:
    match = _TEMP.match(name)
    return int(match.group(1)) if match else None


def is_upload_temp(name: str) -> bool:
    return _TEMP.match(name) is not None


def is_expired_temp(name: str, now: float) -> bool:
    expiry = _expiry(name)
    return expiry is not None and expiry < now
