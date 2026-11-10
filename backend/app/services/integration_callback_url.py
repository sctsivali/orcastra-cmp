"""The callback URL an integration key's callbacks may use: only one its current secret stored.

A /sync carrying org.sync stores `app_url` in the key's metadata, and every request Orcastra makes
to that base carries the key's credential: the member push and removal push send the raw secret,
the lifecycle webhook signs with it, and the organization refresh sends its hash. A URL stored with
a secret that a rotation has since replaced must not receive the new one, or whoever planted it with
a leaked secret gets the secret issued to replace it. So the URL is stored with a tag of the secret
that set it, and is used only while that is still the key's secret. A URL without a tag is not used
until a sync with the current secret stores it again.

important: the tag is a one-way digest of the secret's hash, never the hash itself. The metadata
document is mirrored in the shared Redis cache, and the hash is a credential: the organization
refresh presents it to OrcaHub.
"""

import hashlib
import hmac
from typing import Any, Mapping, Optional

APP_URL_SECRET_TAG = "app_url_secret_tag"


def callback_url_tag(secret_hash: str) -> str:
    """What a stored callback URL records about the secret that stored it."""
    return hashlib.sha256(f"orcastra:callback-url:{secret_hash}".encode()).hexdigest()


def bound_callback_url(metadata: Mapping[str, Any], secret_hash: Optional[str]) -> Optional[str]:
    """The key's callback URL when the secret that stored it is the one hashed to `secret_hash`."""
    app_url = metadata.get("app_url")
    tag = metadata.get(APP_URL_SECRET_TAG)
    if not isinstance(app_url, str) or not app_url or not isinstance(tag, str) or not tag or not secret_hash:
        return None
    return app_url if hmac.compare_digest(tag, callback_url_tag(secret_hash)) else None
