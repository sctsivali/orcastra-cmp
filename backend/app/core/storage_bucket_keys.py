"""Storage bucket keys: refused on the raw proxy for every key and every method.

LXD serves storage-pools/<pool>/buckets/<bucket>/keys and keys/<name> to anyone who may view the
bucket, and each key answers with its S3 access key and secret key. A key granted `read` on one
project could collect the credentials of every bucket there, which outlive the API key and reach the
bucket without passing this proxy. So the path and everything below it are refused like the trust
store (`/certificates`), whatever the grant. Bucket reads other than the keys are left as they were.

note: read off canonical_proxy_path, so a case, a percent-encoding, a trailing slash or a query
cannot present a second spelling. `/proxy` and MCP have no bucket operation, which
tests/test_storage_bucket_keys_are_never_proxied.py holds with a ratchet.
"""

import logging
from typing import Tuple

from fastapi import HTTPException

from app.core.raw_proxy_path import canonical_proxy_path

__all__ = ["assert_bucket_keys_unreached", "reaches_bucket_keys"]

logger = logging.getLogger(__name__)

_BUCKET_KEYS_DETAIL = (
    "The proxy does not expose storage bucket keys (storage-pools/<pool>/buckets/<bucket>/keys): each "
    "one is an S3 access key and secret key for the bucket."
)


def _is_bucket_keys(segments: Tuple[str, ...]) -> bool:
    return len(segments) >= 5 and segments[0] == "storage-pools" and segments[2] == "buckets" and segments[4] == "keys"


def reaches_bucket_keys(path: str) -> bool:
    """Whether a raw-proxy path addresses a bucket's keys or anything below them."""
    return _is_bucket_keys(canonical_proxy_path(path).lowered)


def assert_bucket_keys_unreached(path: str) -> None:
    """Raise 403 for a raw-proxy path under a storage bucket's keys, for every grant and method."""
    if reaches_bucket_keys(path):
        logger.warning("Refusing a proxy request to storage bucket keys")
        raise HTTPException(status_code=403, detail=_BUCKET_KEYS_DETAIL)
