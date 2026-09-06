"""Values for the configuration fields a manifest declared.

The dashboard renders the form from the manifest, so this is where the values it collects are
validated and stored. Two rules carry the weight.

important: a secret field is write-only. Its value goes to Vault beside the installation's other
credential material and is never returned by any read. What a read returns instead is the NAMES of
the secret fields that hold a value, which is enough for a form to show a field as configured
without the server handing back what was typed into it. Grafana's `secureJsonFields` is the same
shape and for the same reason.

important: omitting a secret key on an update leaves the stored value alone. Without that, saving
any other field would silently blank every credential the operator did not retype, which is a data
loss bug disguised as a save button.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import HTTPException

from app.core.url_safety import describe_url_risk

logger = logging.getLogger(__name__)

VAULT_EXTENSIONS_PATH = "extensions"


def _settings_path(install_id: int) -> str:
    return f"secret/data/{VAULT_EXTENSIONS_PATH}/installations/{install_id}/settings"


def _refuse(field_key: str, why: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{field_key}: {why}")


def validate_values(
    schema: Optional[Sequence[Dict[str, Any]]],
    values: Dict[str, Any],
    *,
    already_configured: Sequence[str] = (),
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Check submitted values against the declared fields.

    Returns (plain_values, secret_values). Raises 422 naming the field that was wrong, because a
    settings form with one generic error message is a form nobody can fix.
    """
    fields = {field["key"]: field for field in (schema or [])}

    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"this add-on does not declare these settings: {', '.join(unknown)}",
        )

    plain: Dict[str, Any] = {}
    secrets_: Dict[str, str] = {}

    for key, field in fields.items():
        kind = field["type"]
        present = key in values
        value = values.get(key)

        if not present:
            if field.get("required") and kind == "secret" and key not in already_configured:
                raise _refuse(key, "is required")
            if field.get("required") and kind != "secret" and field.get("default") is None:
                raise _refuse(key, "is required")
            if field.get("default") is not None:
                plain[key] = field["default"]
            continue

        if value is None or value == "":
            if field.get("required"):
                raise _refuse(key, "is required")
            if kind != "secret":
                plain[key] = None
            continue

        if kind == "secret":
            if not isinstance(value, str):
                raise _refuse(key, "must be text")
            secrets_[key] = value
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise _refuse(key, "must be true or false")
            plain[key] = value
        elif kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise _refuse(key, "must be a whole number")
            if field.get("min") is not None and value < field["min"]:
                raise _refuse(key, f"must be at least {field['min']}")
            if field.get("max") is not None and value > field["max"]:
                raise _refuse(key, f"must be at most {field['max']}")
            plain[key] = value
        elif kind == "enum":
            if value not in (field.get("options") or []):
                raise _refuse(key, f"must be one of: {', '.join(field.get('options') or [])}")
            plain[key] = value
        elif kind == "url":
            if not isinstance(value, str):
                raise _refuse(key, "must be text")
            # The same guard every stored URL gets. An add-on's configured endpoint is not
            # fetched by this server today, but a setting that later becomes one must not be the
            # place the rule was skipped.
            reason = describe_url_risk(value, require_https=True, resolve=False)
            if reason is not None:
                raise _refuse(key, reason)
            plain[key] = value
        else:  # string
            if not isinstance(value, str):
                raise _refuse(key, "must be text")
            if len(value) > 4096:
                raise _refuse(key, "is too long")
            plain[key] = value

    return plain, secrets_


def store_secrets(vault_client, install_id: int, secrets_: Dict[str, str]) -> List[str]:
    """Merge new secret values into the stored set and return every configured name.

    Merge, not replace: a form that submits one field must not blank the others.
    """
    if not secrets_:
        return list(read_secret_names(vault_client, install_id))
    # important: a read failure must NOT become an empty dict. Swallowing it turned the merge
    # this function exists for into a replace: one Vault hiccup during a settings save destroyed
    # every other stored secret for that installation and answered 200 with a shortened list.
    # This module's own docstring calls that "a data loss bug disguised as a save button", and
    # the swallow was sitting three lines under it.
    #
    # important: "nothing stored yet" is not "unavailable", and conflating them broke the very
    # first save on every installation. `vault_read` raises a 404 for a path that has never been
    # written, which is exactly the empty case this merge wants, so it is the one failure that
    # must not become a refusal. Everything else still refuses, which is the guard above intact.
    try:
        current: Dict[str, Any] = vault_client.vault_read(_settings_path(install_id)) or {}
    except Exception as exc:  # noqa: BLE001
        # A string match, reluctantly: `vault_read` raises `VaultClientError(...)` without
        # `from e`, so there is no `__cause__` to inspect and no status on the exception. The
        # message is `Vault read failed: 404 Client Error: ...` from requests, so the full phrase
        # is matched rather than a bare "404" that could appear in a path or a body.
        if "404 Client Error" not in str(exc):
            raise HTTPException(
                status_code=503,
                detail=(
                    "The secret store is unavailable, so nothing was saved. Existing values are "
                    "untouched. Try again once it is reachable."
                ),
            ) from exc
        current = {}
    current.update(secrets_)
    vault_client.vault_kv_put(_settings_path(install_id), current)
    return sorted(current)


def read_secret_names(vault_client, install_id: int) -> List[str]:
    """The presence map. Names only, never values."""
    try:
        stored = vault_client.vault_read(_settings_path(install_id)) or {}
    except Exception:
        return []
    return sorted(stored)


def read_resolved_settings(
    vault_client, install_id: int, plain: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Everything the add-on itself reads back, secrets included.

    Only ever served to the add-on presenting its own credential. The dashboard reads the plain
    values and the presence map instead.
    """
    resolved = dict(plain or {})
    try:
        resolved.update(vault_client.vault_read(_settings_path(install_id)) or {})
    except Exception:
        logger.debug("No stored secret settings for installation %s", install_id)
    return resolved


def destroy_settings(vault_client, install_id: int) -> bool:
    """True when nothing is left behind. See `destroy_signing_secret` for why it answers."""
    from app.core.vault_client import VaultClientError

    try:
        vault_client.vault_delete(_settings_path(install_id))
        return True
    except VaultClientError:
        return True
    except Exception:
        logger.warning(
            "Could not delete stored settings for installation %s", install_id, exc_info=True
        )
        return False
