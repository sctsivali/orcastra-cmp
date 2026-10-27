"""Submit one Vault unseal key share, or reset an attempt, on an Administrator's behalf.

Vault's `sys/unseal` is unauthenticated: anyone who reaches Vault can add a share. What the
dashboard adds is reach (an operator without a shell on the Vault host), progress several key
holders can watch, and an audit record of who submitted when. It never keeps the share: the value
lives in this request's memory, goes to Vault once, and appears in no log, audit row, error or cache.

important: a bare `requests.put`, not VaultClient. That session retries PUT on connect errors and
5xx, and a retried share is a share Vault may have counted already. `allow_redirects=False` so a
standby node's redirect can never forward a share to another address.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Literal, Optional, Tuple
from urllib.parse import urlparse

import requests

from app.core import vault_probe
from app.core.vault_probe import SealFacts, SealProbe

Outcome = Literal["accepted", "duplicate", "unsealed", "reset"]

# A Shamir share from `vault operator init` is base64 (44 characters) or hex (66). Anything far
# outside that is a paste mistake, refused before Vault ever sees it.
KEY_MIN_LENGTH = 16
KEY_MAX_LENGTH = 256
_KEY_PATTERN = re.compile(r"^(?:[A-Za-z0-9+/]+={0,2}|[0-9a-fA-F]+)$")
_TIMEOUT = (2.0, 10.0)


class UnsealError(Exception):
    """A refusal with a fixed, input-free message. `status` is the HTTP answer for the route."""

    def __init__(self, status: int, code: str, message: str, probe: Optional[SealProbe] = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.probe = probe


@dataclass(frozen=True)
class UnsealResult:
    outcome: Outcome
    probe: SealProbe


def normalise_key(raw: str) -> str:
    """The share without surrounding whitespace, or UnsealError when it cannot be a share."""
    key = raw.strip()
    if not (KEY_MIN_LENGTH <= len(key) <= KEY_MAX_LENGTH) or not _KEY_PATTERN.match(key):
        raise UnsealError(422, "invalid_key", "That is not an unseal key. Paste the whole key share, nothing else.")
    return key


def insecure_transport(vault_addr: str) -> bool:
    return urlparse(vault_addr or "").scheme == "http"


def availability(settings, probe: Optional[SealProbe]) -> Tuple[bool, Optional[str]]:
    """Whether the dashboard may offer an unseal now, and why not."""
    if not getattr(settings, "vault_unseal_ui_enabled", False):
        return False, "Unsealing from the dashboard is turned off (VAULT_UNSEAL_UI_ENABLED=false)."
    if not getattr(settings, "auth_enabled", False):
        # require_roles fabricates an Administrator when sign-in is off; nobody could be held to account.
        return False, "Unsealing from the dashboard requires sign-in (AUTH_ENABLED)."
    if not settings.vault_enabled or not settings.vault_addr:
        return False, "Vault is not configured."
    if probe is None or probe.state != "sealed":
        return False, "Vault is not sealed."
    facts = probe.facts or SealFacts()
    if facts.recovery_seal or (facts.seal_type and facts.seal_type != "shamir"):
        return False, "This Vault unseals itself through its configured seal (auto-unseal); key shares do not apply."
    return True, None


def _refuse_unless_unsealable(settings, probe: SealProbe) -> None:
    if probe.state == "unreachable":
        raise UnsealError(503, "vault_unreachable", "Vault did not answer. Nothing was submitted.", probe)
    if probe.state == "uninitialized":
        raise UnsealError(409, "vault_uninitialized",
                          "Vault is not initialized, so no unseal keys exist yet. Initialize it with the installer.", probe)
    if probe.state == "ok":
        raise UnsealError(409, "vault_not_sealed", "Vault is already unsealed.", probe)
    allowed, reason = availability(settings, probe)
    if not allowed:
        raise UnsealError(409, "unseal_not_available", reason or "Unsealing is not available.", probe)


def submit(settings, *, key: Optional[str], reset: bool, nonce: Optional[str]) -> UnsealResult:
    """Blocking: one probe, one PUT, and one more probe only when Vault refused. Off the event loop."""
    before = vault_probe.ask_now()
    _refuse_unless_unsealable(settings, before)
    facts = before.facts or SealFacts()
    if nonce and nonce != facts.nonce:
        raise UnsealError(
            409, "unseal_attempt_changed",
            "The unseal attempt changed (someone reset it or started a new one). Your key was not submitted.",
            before,
        )
    payload = {"reset": True} if reset else {"key": key}
    try:
        response = requests.put(
            f"{settings.vault_addr.rstrip('/')}/v1/sys/unseal",
            json=payload, timeout=_TIMEOUT, allow_redirects=False,
        )
    except requests.RequestException:
        # The share may have reached Vault before the answer was lost (a read timeout, a reset
        # connection). Ask Vault what it holds now rather than guessing either way.
        return _settle_unanswered(reset, facts)
    finally:
        payload.clear()

    if response.status_code == 200:
        body = _json(response)
        sealed = body.get("sealed")
        if not isinstance(sealed, bool):
            raise UnsealError(502, "vault_error", "Vault answered the unseal with an unexpected body.", vault_probe.peek())
        after = SealProbe("sealed" if sealed else "ok", time.time(),
                          "Vault reports it is sealed" if sealed else None, vault_probe.facts_from(body))
        vault_probe.record(after)
        return UnsealResult(_outcome(reset, sealed, facts, after.facts or SealFacts()), after)

    if response.status_code == 400 and not reset:
        after = vault_probe.ask_now()
        threshold = facts.threshold or 1
        if (facts.progress or 0) + 1 >= threshold:
            # The share completed the threshold, Vault combined the shares and could not decrypt
            # with the result: it discards the attempt. One of the shares in it was wrong.
            raise UnsealError(
                422, "unseal_rejected",
                "Vault combined the keys and rejected them, so this attempt was discarded. "
                "One of the keys entered was wrong. Start again with the correct keys.",
                after,
            )
        raise UnsealError(422, "invalid_key",
                          "Vault did not accept that as an unseal key. Check that you copied the whole key.", after)
    raise UnsealError(502, "vault_error", f"Vault refused the request (HTTP {response.status_code}).", vault_probe.peek())


def _settle_unanswered(reset: bool, before: SealFacts) -> UnsealResult:
    after = vault_probe.ask_now()
    now = after.facts or SealFacts()
    if after.state == "ok":
        return UnsealResult("unsealed", after)
    if after.state == "sealed":
        if reset and not now.progress:
            return UnsealResult("reset", after)
        same_attempt = not before.nonce or now.nonce == before.nonce
        if not reset and same_attempt and (now.progress or 0) > (before.progress or 0):
            return UnsealResult("accepted", after)
        raise UnsealError(
            503, "vault_unconfirmed",
            f"Vault did not confirm the request in time. Progress is {now.progress or 0} of "
            f"{now.threshold or '?'}; check it before entering the key again.",
            after,
        )
    raise UnsealError(503, "vault_unreachable",
                      "Vault stopped answering. Check the progress before entering the key again.", after)


def _outcome(reset: bool, sealed: bool, before: SealFacts, after: SealFacts) -> Outcome:
    if reset:
        return "reset"
    if not sealed:
        return "unsealed"
    # Vault ignores a share it already holds for this attempt: same attempt, same progress.
    if (before.progress or 0) > 0 and after.progress == before.progress and after.nonce == before.nonce:
        return "duplicate"
    return "accepted"


def _json(response: requests.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
