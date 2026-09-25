"""Which /integrations/api-keys edits an add-on credential refuses.

An add-on installation's credential is an ordinary integration key in Vault, so every
/integrations/api-keys route reaches it once the caller's clusters overlap its grant. The
installation owns that grant: install writes it under the operator's consent, rescope rewrites it,
rotation copies it onto the new key and the reconcile sweep narrows it. An edit through the key
routes changed what a third party holds outside that consent, and the next rescope withdrew it
again without anyone deciding so.

Per route, for a key carrying extension_install_id:
  PUT    /api-keys/{id}/capabilities    refused, 409. Rescope the installation.
  PUT    /api-keys/{id}/permissions     refused, 409. Rescope the installation.
  DELETE /api-keys/{id}                 refused, 409. Rotation and rescope read the key before they
                                        write, so a deleted key breaks both with a 503 that says the
                                        store is down. Revoke stops the key now and uninstall
                                        removes it with its signing secret and settings.
  POST   /api-keys/{id}/revoke          allowed. The kill switch, for whoever may revoke today.
                                        Rescope keeps the revocation (it rewrites two fields) and
                                        rotation issues a working key again.
  GET    /api-keys, /api-keys/{id}      allowed, reads.
  POST   /api-keys/{id}/users           allowed. A user mapping is only listed and counted, and
                                        nothing that authorizes a request reads it.
  GET    /api-keys/{id}/users           allowed, a read.
  DELETE /api-keys/{id}/users/{user}    allowed. Admin only, and it removes a mapping.
  POST   /api-keys                      not keyed. It cannot write extension_install_id.

important: each check runs after _authorize_api_key, so a key outside the caller's clusters still
answers 404 and the 409 never tells anyone an id they cannot see exists.
"""

from typing import Any, Mapping

from fastapi import HTTPException

GRANT_EDIT_DETAIL = (
    "This key is an add-on credential. Its add-on installation owns the grant, so its "
    "capabilities and cluster permissions change only by rescoping that installation."
)
DELETE_DETAIL = (
    "This key is an add-on credential and its add-on installation owns it. Revoke it to stop it "
    "now, or uninstall the add-on to remove it."
)


def is_add_on_credential(metadata: Mapping[str, Any]) -> bool:
    return metadata.get("extension_install_id") is not None


def refuse_grant_edit(metadata: Mapping[str, Any]) -> None:
    """409 for a capability or cluster permission edit of an add-on credential."""
    if is_add_on_credential(metadata):
        raise HTTPException(status_code=409, detail=GRANT_EDIT_DETAIL)


def refuse_delete(metadata: Mapping[str, Any]) -> None:
    """409 for a delete of an add-on credential."""
    if is_add_on_credential(metadata):
        raise HTTPException(status_code=409, detail=DELETE_DETAIL)
