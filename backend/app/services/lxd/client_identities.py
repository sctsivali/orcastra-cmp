"""The cluster-side half of a My Keys key, on each engine.

LXD (5.21 and later) holds a client as a TLS identity plus a group carrying its entitlements,
under /1.0/auth. Incus has no /1.0/auth at all (doc/rest-api.yaml at v6.0.7 and v7.6.0): its
client is a trust-store certificate, restricted to a list of projects or not. So a Standard key is
an LXD identity in a group with `operator` on one project, or an Incus certificate restricted to
that project; an Admin key is server `admin`, or an unrestricted certificate.

important: a restricted Incus certificate gets full control inside its projects, exec included,
which is what LXD's `operator` gives too. Neither reaches another project or the server config.
The guards before this module (services/lxd_key_bounds.py) decide whether the key may exist.

Blocking: every call here is a round trip to the cluster, so callers run it in a thread or from
an already-blocking route.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from pylxd import exceptions as lxd_exceptions

from app.core.cert_fingerprint import der_from_pem, is_lxd_fingerprint
from app.core.lxd_path import safe_segment
from app.core.type_utils import resolve_cluster_type

logger = logging.getLogger(__name__)

LXD = "lxd"
INCUS = "incus"
TIMEOUT = (5, 30)
# Tells a person reading the trust store what the entry is. A holder can rewrite it (and its
# certificate); the name it cannot (see incus_trust_name), so only the name ties it to a key.
INCUS_DESCRIPTION = "Orcastra My Keys credential"

# (common_name) -> (cert_pem, key_pem, fingerprint)
Mint = Callable[[str], Tuple[bytes, bytes, str]]


class EngineUnknown(Exception):
    """The cluster has not told a trusted client what it is; never guess LXD."""


class IssueFailed(Exception):
    """The cluster refused or mangled the credential. Whatever was created has been removed."""


@dataclass(frozen=True)
class Issued:
    engine: str
    common_name: str
    identity_id: str
    group_name: str
    cert_pem: bytes
    key_pem: bytes
    fingerprint: str
    trust_name: str = ""  # Incus: the trust entry's name, which a holder cannot change


def incus_trust_name(key_id: str) -> str:
    """The name an Incus trust entry carries for one key.

    important: Incus lets a restricted client replace its own certificate and description
    (certificate_self_renewal) but not its name, restriction or projects (cmd/incusd/certificates.go
    doCertificateUpdate, v7.6.0), so the name is what still ties a renewed entry to its key. It is a
    digest because a client can list the trust store and should not read other keys' names there.
    """
    return "orcastra-key-" + hashlib.sha256(key_id.encode("utf-8")).hexdigest()[:24]


def engine_of(client: Any) -> str:
    """lxd or incus, from the client's trusted /1.0. MicroCloud runs LXD."""
    family = resolve_cluster_type(None, client)
    if family is None:
        raise EngineUnknown("the cluster has not answered a trusted read yet")
    return INCUS if family.lower() == INCUS else LXD


def server_version(client: Any) -> str:
    host_info = getattr(client, "host_info", None) or {}
    return str((host_info.get("environment") or {}).get("server_version") or "")


def issue(
    client: Any,
    *,
    engine: str,
    key_id: str,
    name: str,
    auth_level: str,
    project: str,
    mint: Mint,
) -> Issued:
    """Create the cluster credential for one key. Raises IssueFailed with nothing left behind."""
    if engine == INCUS:
        return _issue_incus(client, key_id=key_id, name=name, auth_level=auth_level, project=project, mint=mint)
    return _issue_lxd(client, key_id=key_id, name=name, auth_level=auth_level, project=project, mint=mint)


def revoke(client: Any, record: Mapping[str, Any]) -> List[str]:
    """Remove the cluster credential of one stored key. Safe to repeat; returns cleanup errors.

    A record without `engine` was written before Incus keys existed, so it is LXD's.
    """
    if (record.get("engine") or LXD) == INCUS:
        return _revoke_incus(client, record)
    return _revoke_lxd(client, record)


# ── Incus: a trust-store certificate ──────────────────────────────────────────────────────────


def _incus_body(key_id: str, common_name: str, cert_pem: bytes, auth_level: str, project: str) -> Dict[str, Any]:
    restricted = auth_level != "admin"
    return {
        "type": "client",
        "name": incus_trust_name(key_id),
        # note: no key id or name; a restricted client can list every entry's description.
        "description": INCUS_DESCRIPTION,
        # note: base64 DER, which is what POST takes (CertificatesPost.certificate).
        "certificate": base64.b64encode(der_from_pem(cert_pem.decode("utf-8"))).decode("ascii"),
        "restricted": restricted,
        "projects": [project] if restricted else [],
    }


def _issue_incus(client: Any, *, key_id: str, name: str, auth_level: str, project: str, mint: Mint) -> Issued:
    common_name = f"{name}@orcastra"
    cert_pem, key_pem, fingerprint = mint(common_name)
    body = _incus_body(key_id, common_name, cert_pem, auth_level, project)
    try:
        _post_or_undo(lambda: client.api.certificates.post(json=body, timeout=TIMEOUT),
                      lambda: delete_certificate(client, fingerprint))
    except lxd_exceptions.Conflict:
        # A name another entry already uses: one retry under a fresh name and certificate, as LXD.
        common_name = f"{name}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}@orcastra"
        cert_pem, key_pem, fingerprint = mint(common_name)
        body = _incus_body(key_id, common_name, cert_pem, auth_level, project)
        try:
            _post_or_undo(lambda: client.api.certificates.post(json=body, timeout=TIMEOUT),
                          lambda: delete_certificate(client, fingerprint))
        except lxd_exceptions.Conflict as exc:
            raise IssueFailed(f"the cluster refused the certificate: {_reason(exc)}") from exc

    # important: read back what the trust store holds before handing out the key. A daemon that
    # dropped `restricted` or the project list would otherwise mint an admin credential silently.
    problem = _incus_entry_problem(client, fingerprint, body)
    if problem:
        delete_certificate(client, fingerprint)
        raise IssueFailed(problem)
    return Issued(INCUS, common_name, fingerprint, "", cert_pem, key_pem, fingerprint, incus_trust_name(key_id))


def _post_or_undo(post: Callable[[], Any], undo: Callable[[], Optional[str]]) -> Any:
    """Send one create; on any answer but a name conflict, remove what it may have written.

    important: an error answer does not mean nothing was written. On the Incus lab (2026-11-08)
    the daemon stored a trust entry cluster-wide, then answered 500 because a peer refused it
    ("failed to notify peer"), and a timed-out POST leaves the same doubt. The fingerprint was
    minted for this request alone, so removing it can only remove what this request made.
    A conflict is refused before anything is stored, and the caller retries under a new name.
    """
    try:
        return post()
    except lxd_exceptions.Conflict:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure here may have left an entry behind
        left = undo()
        detail = f"the cluster refused the certificate: {_reason(exc)}"
        if left:
            logger.error("A refused credential may still be trusted: %s", left)
            detail += f"; what it may have stored was not removed ({left})"
        raise IssueFailed(detail) from exc


def _incus_entry_problem(client: Any, fingerprint: str, body: Mapping[str, Any]) -> Optional[str]:
    try:
        entry = client.api.certificates[safe_segment(fingerprint)].get(timeout=TIMEOUT).json().get("metadata") or {}
    except lxd_exceptions.LXDAPIException as exc:
        return f"the new certificate could not be read back: {_reason(exc)}"
    if entry.get("fingerprint") != fingerprint:
        return "the cluster holds a different certificate than the one sent"
    if bool(entry.get("restricted")) != body["restricted"]:
        return "the cluster did not keep the certificate's restriction"
    if body["restricted"] and sorted(entry.get("projects") or []) != sorted(body["projects"]):
        return "the cluster did not keep the certificate's project"
    return None


def delete_certificate(client: Any, fingerprint: str) -> Optional[str]:
    """Delete the entry named exactly; None on success or when it is already gone."""
    if not is_lxd_fingerprint(fingerprint):
        return "refusing a fingerprint that is not 64 lowercase hex characters"
    try:
        found = client.api.certificates[safe_segment(fingerprint)].get(timeout=TIMEOUT).json().get("metadata") or {}
    except lxd_exceptions.NotFound:
        return None
    except Exception as exc:  # noqa: BLE001 - a timeout is an answer too, never "gone"
        return f"lookup failed: {_reason(exc)}"
    if found.get("fingerprint") != fingerprint:
        # The daemon resolves a prefix; anything but the exact entry is not ours to delete.
        return "the cluster resolved a different entry"
    try:
        client.api.certificates[safe_segment(fingerprint)].delete(timeout=TIMEOUT)
    except lxd_exceptions.NotFound:
        return None
    except Exception as exc:  # noqa: BLE001
        return f"removal failed: {_reason(exc)}"
    return None


def _revoke_incus(client: Any, record: Mapping[str, Any]) -> List[str]:
    # note: read again after each removal. A holder renewing between the read and the delete would
    # leave an entry under a new fingerprint while the delete answered "already gone".
    for _ in range(3):
        entry, problem = _entry_for(client, record)
        if problem:
            return [problem]
        if entry is None:
            return []
        error = delete_certificate(client, str(entry.get("fingerprint") or ""))
        if error:
            return [f"Certificate removal: {error}"]
    return ["the key's trust entry was still present after three removals; remove it by hand"]


def _entry_for(client: Any, record: Mapping[str, Any]) -> Tuple[Optional[Mapping[str, Any]], Optional[str]]:
    """(entry, problem): the one client entry carrying this key's trust name, None when gone.

    important: the name decides, never the stored fingerprint. A holder may replace an entry's
    certificate with any certificate it holds a key for, including another of its keys' original
    one, so the stored fingerprint can name someone else's entry (security review, 2026-11-08).
    Restriction and projects are not compared: a project renamed under a key changes the list,
    not whose entry it is, and a holder cannot change either.
    """
    name = str(record.get("trust_name") or incus_trust_name(str(record.get("id") or "")))
    try:
        entries = client.api.certificates.get(params={"recursion": "1"}, timeout=TIMEOUT).json().get("metadata") or []
    except Exception as exc:  # noqa: BLE001 - not knowing is not "gone"
        return None, f"Could not read the trust store: {_reason(exc)}"
    matches = [e for e in entries if isinstance(e, dict) and e.get("name") == name and e.get("type") == "client"]
    if len(matches) > 1:
        return None, f"more than one trust entry answers to {name}; remove the right one by hand"
    if matches and (record.get("project") and record.get("auth_level") != "admin"
                    and list(matches[0].get("projects") or []) != [record.get("project")]):
        logger.warning("Trust entry %s now lists projects %s", name, matches[0].get("projects"))
    return (matches[0] if matches else None), None


# ── LXD: a TLS identity in a group ──────────────────────────────────────────────────────────────


def _issue_lxd(client: Any, *, key_id: str, name: str, auth_level: str, project: str, mint: Mint) -> Issued:
    common_name = f"{name}@orcastra"
    cert_pem, key_pem, fingerprint = mint(common_name)
    identity = {"name": common_name, "type": "tls", "certificate": cert_pem.decode("utf-8")}
    try:
        response = _post_or_undo(lambda: client.api.auth.identities["tls"].post(json=identity, timeout=TIMEOUT),
                                 lambda: _delete_lxd_identity(client, fingerprint))
    except lxd_exceptions.Conflict:
        # Identity with this name already exists, generate a unique name with timestamp
        common_name = f"{name}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}@orcastra"
        cert_pem, key_pem, fingerprint = mint(common_name)
        identity = {"name": common_name, "type": "tls", "certificate": cert_pem.decode("utf-8")}
        try:
            response = _post_or_undo(lambda: client.api.auth.identities["tls"].post(json=identity, timeout=TIMEOUT),
                                     lambda: _delete_lxd_identity(client, fingerprint))
        except lxd_exceptions.Conflict as exc:
            raise IssueFailed(f"the cluster refused the identity: {_reason(exc)}") from exc

    # LXD returns metadata=null for TLS identity creation; the Location header names the id.
    metadata = (response.json() if response.text else {}).get("metadata")
    identity_id = metadata.get("id", "") if isinstance(metadata, dict) else ""
    if not identity_id:
        location = response.headers.get("Location", "")
        identity_id = location.split("/")[-1] if location else common_name

    group_name = f"{key_id}_group"
    if auth_level == "admin":
        permissions = [{"entity_type": "server", "url": "/1.0", "entitlement": "admin"}]
        description = f"Admin group for {name} with full server access"
    else:
        permissions = [{"entity_type": "project", "url": f"/1.0/projects/{project}", "entitlement": "operator"}]
        description = f"Group for {name} with operator access to project {project}"
    try:
        client.api.auth.groups.post(
            json={"name": group_name, "description": description, "permissions": permissions}, timeout=TIMEOUT
        )
    except lxd_exceptions.LXDAPIException as exc:
        _quietly(lambda: client.api.auth.identities["tls"][safe_segment(identity_id)].delete(timeout=TIMEOUT))
        raise IssueFailed(f"Could not create group: {_reason(exc)}") from exc

    try:
        # PATCH the identity's groups list; that is the membership LXD records.
        current = client.api.auth.identities["tls"][safe_segment(identity_id)].get(timeout=TIMEOUT)
        groups = list((current.json().get("metadata") or {}).get("groups") or [])
        if group_name not in groups:
            groups.append(group_name)
        client.api.auth.identities["tls"][safe_segment(identity_id)].patch(json={"groups": groups}, timeout=TIMEOUT)
        verify = client.api.auth.groups[safe_segment(group_name)].get(timeout=TIMEOUT).json().get("metadata") or {}
        if identity_id not in ((verify.get("identities") or {}).get("tls") or []):
            raise IssueFailed("identity not found in group after PATCH")
        if not verify.get("permissions"):
            raise IssueFailed("no permissions in group after creation")
    except Exception as exc:  # noqa: BLE001 - every failure here leaves a half-made key to remove
        logger.error("Failed to add identity to group: %s", exc)
        _quietly(lambda: client.api.auth.groups[safe_segment(group_name)].delete(timeout=TIMEOUT))
        _quietly(lambda: client.api.auth.identities["tls"][safe_segment(identity_id)].delete(timeout=TIMEOUT))
        raise IssueFailed(f"Could not configure group membership: {exc}") from exc
    return Issued(LXD, common_name, identity_id, group_name, cert_pem, key_pem, fingerprint)


def _delete_lxd_identity(client: Any, fingerprint: str) -> Optional[str]:
    """Delete the TLS identity whose id is this fingerprint; None when it is gone."""
    if not is_lxd_fingerprint(fingerprint):
        return "refusing a fingerprint that is not 64 lowercase hex characters"
    try:
        client.api.auth.identities["tls"][safe_segment(fingerprint)].delete(timeout=TIMEOUT)
    except lxd_exceptions.NotFound:
        return None
    except Exception as exc:  # noqa: BLE001
        return f"removal failed: {_reason(exc)}"
    return None


def _revoke_lxd(client: Any, record: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    identity_id = str(record.get("identity_id") or "")
    group_name = str(record.get("group_name") or "")
    # important: an LXD identity may replace its own certificate (PATCH tls_certificate; shown on
    # console-lab, LXD 5.21.9, 2026-11-08), which changes its id, so the stored id alone left it
    # trusted. The group is this key's own, so every identity in it is this key's.
    members: List[str] = []
    if group_name:
        try:
            group = client.api.auth.groups[safe_segment(group_name)].get(timeout=TIMEOUT).json().get("metadata") or {}
            members = list((group.get("identities") or {}).get("tls") or [])
        except lxd_exceptions.NotFound:
            pass
        except Exception as exc:  # noqa: BLE001 - not knowing the members is not "removed"
            return [f"Failed to read group {group_name}: {_reason(exc)}"]
    # Identities first (deleting one removes it from its groups), then the group.
    for member in dict.fromkeys([identity_id, *members]):
        if not member:
            continue
        try:
            client.api.auth.identities["tls"][safe_segment(member)].delete(timeout=TIMEOUT)
        except lxd_exceptions.NotFound:
            pass
        except Exception as exc:  # noqa: BLE001 - reported, the next step still runs
            errors.append(f"Failed to delete identity {member}: {exc}")
    if group_name:
        try:
            client.api.auth.groups[safe_segment(group_name)].delete(timeout=TIMEOUT)
        except lxd_exceptions.NotFound:
            pass
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Failed to delete group {group_name}: {exc}")
    return errors


def _quietly(call: Callable[[], Any]) -> None:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - a rollback step that fails is logged, not raised
        logger.warning("Rollback step failed: %s", exc)


def _reason(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    try:
        return str((response.json() or {}).get("error") or exc)[:300]
    except Exception:  # noqa: BLE001
        return str(exc)[:300]


__all__ = [
    "EngineUnknown",
    "INCUS",
    "IssueFailed",
    "Issued",
    "LXD",
    "delete_certificate",
    "engine_of",
    "issue",
    "revoke",
    "server_version",
]
