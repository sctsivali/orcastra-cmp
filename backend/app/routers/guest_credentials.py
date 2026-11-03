"""Guest credentials: passwords, local users and SSH public keys of one running Linux instance.

Every handler calls `verify_guest_access` first, so the cluster-scope ratchet can see it, then
reads its body with `read_json_body` (a 422 never echoes a password or a pasted key), validates
what needs no guest, and only then enters `admitted`, which adds break glass, the migration fence,
the budget, the slots and the instance checks. Reads are POST so a Partner cluster past its paid
term refuses them as it refuses the console.

A session JWT is required: nothing here is reachable with an integration key.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.config import get_settings
from app.core.dependencies import check_read_only_mode, require_tenant
from app.core.instance_access import verify_guest_access
from app.core.json_body import body_spec, read_json_body
from app.core.lxd_names import LXD_NAME_PATTERN
from app.core.org_scope import org_scope_dependency
from app.models.guest_credential_schemas import (
    AccountBody,
    CreateUserBody,
    CreateUserResponse,
    KeyAddBody,
    KeyAddResponse,
    KeyRemoveBody,
    KeyRemoveResponse,
    KeysResponse,
    OverviewResponse,
    PasswordResponse,
    PasswordSetBody,
    SshServerResponse,
    SudoBody,
    SudoResponse,
)
from app.routers.instance_files import release_request_db
from app.services.guest_credentials import accounts, authorized_keys, errors, flows
from app.services.guest_credentials.audit import audited, record_view
from app.services.guest_credentials.gates import admitted, write_lock
from app.services.instance_files.session import http_failures

logger = logging.getLogger(__name__)


async def require_credentials_enabled() -> None:
    if not get_settings().guest_credentials_enabled:
        raise HTTPException(
            status_code=503,
            detail={"error": "feature_disabled", "message": "Guest credentials are turned off on this deployment."},
        )


router = APIRouter(
    prefix="/guest-credentials",
    tags=["Guest Credentials"],
    dependencies=[
        Depends(require_tenant()),
        Depends(require_credentials_enabled),
        Depends(org_scope_dependency),
        Depends(release_request_db),
    ],
)

_TARGET = {"pattern": LXD_NAME_PATTERN}


def _target(record: Dict[str, Any]) -> Optional[int]:
    return record.get("break_glass_grant_id")


@router.post("/overview", response_model=OverviewResponse, summary="Read a guest's accounts and tools")
async def overview(
    host: str = Query(..., max_length=255),
    project: str = Query("default", max_length=255),
    instance_name: str = Query(..., **_TARGET),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_guest_access(host, project, instance_name)
    record: Dict[str, Any] = {}
    async with admitted(user, host, bound, instance_name, kind="read", record=record) as guest:
        facts = await flows.facts_of(guest, with_sudo=True)
    await record_view(user, host, bound, instance_name, _target(record), "overview")
    return flows.overview(guest, facts)


@router.post("/ssh-server", response_model=SshServerResponse, summary="Read a guest's SSH server state")
async def ssh_server(
    host: str = Query(..., max_length=255),
    project: str = Query("default", max_length=255),
    instance_name: str = Query(..., **_TARGET),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_guest_access(host, project, instance_name)
    record: Dict[str, Any] = {}
    async with admitted(user, host, bound, instance_name, kind="read", record=record) as guest:
        facts = await flows.ssh_server(guest)
    await record_view(user, host, bound, instance_name, _target(record), "ssh-server")
    return facts


@router.post("/keys/list", response_model=KeysResponse, summary="List one account's SSH keys",
             openapi_extra=body_spec(AccountBody))
async def list_keys(
    request: Request,
    host: str = Query(..., max_length=255),
    project: str = Query("default", max_length=255),
    instance_name: str = Query(..., **_TARGET),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_guest_access(host, project, instance_name)
    body = await read_json_body(request, AccountBody)
    record: Dict[str, Any] = {}
    async with admitted(user, host, bound, instance_name, kind="read", record=record) as guest:
        facts = await flows.facts_of(guest)
        account = flows.account_or_refuse(facts, body.user)
        keyfile = await authorized_keys.read(guest, account)
    await record_view(user, host, bound, instance_name, _target(record), "keys")
    return {
        "user": account.name, "state": keyfile.state,
        "entries": [flows.entry_view(entry) for entry in keyfile.entries], "warnings": keyfile.warnings,
    }


@router.post("/keys/add", response_model=KeyAddResponse, summary="Add one SSH public key to an account",
             dependencies=[Depends(check_read_only_mode)], openapi_extra=body_spec(KeyAddBody))
async def add_key(
    request: Request,
    host: str = Query(..., max_length=255),
    project: str = Query("default", max_length=255),
    instance_name: str = Query(..., **_TARGET),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_guest_access(host, project, instance_name)
    body = await read_json_body(request, KeyAddBody)
    async with audited("guest.authorized_key.add", host=host, project=bound, instance=instance_name,
                       details={"user": body.user}) as record:
        async with http_failures():
            key = flows.key_or_refuse(body.public_key)
        record.update(fingerprint=key.fingerprint, key_type=key.key_type, bits=key.bits)
        async with admitted(user, host, bound, instance_name, kind="write", record=record) as guest:
            async with write_lock(host, bound, instance_name):
                facts = await flows.facts_of(guest)
                account = flows.account_or_refuse(facts, body.user)
                record.update(uid=account.uid, kind=account.kind)
                return await flows.add_key(guest, facts, account, key, record)


@router.post("/keys/remove", response_model=KeyRemoveResponse, summary="Remove one SSH key from an account",
             dependencies=[Depends(check_read_only_mode)], openapi_extra=body_spec(KeyRemoveBody))
async def remove_key(
    request: Request,
    host: str = Query(..., max_length=255),
    project: str = Query("default", max_length=255),
    instance_name: str = Query(..., **_TARGET),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_guest_access(host, project, instance_name)
    body = await read_json_body(request, KeyRemoveBody)
    async with audited("guest.authorized_key.remove", host=host, project=bound, instance=instance_name,
                       details={"user": body.user, "fingerprint": body.fingerprint}) as record:
        async with admitted(user, host, bound, instance_name, kind="write", record=record) as guest:
            async with write_lock(host, bound, instance_name):
                facts = await flows.facts_of(guest)
                account = flows.account_or_refuse(facts, body.user)
                record.update(uid=account.uid, kind=account.kind)
                return await flows.remove_key(guest, facts, account, body.fingerprint, record)


@router.post("/password/set", response_model=PasswordResponse, summary="Set an account's password",
             dependencies=[Depends(check_read_only_mode)], openapi_extra=body_spec(PasswordSetBody))
async def set_password(
    request: Request,
    host: str = Query(..., max_length=255),
    project: str = Query("default", max_length=255),
    instance_name: str = Query(..., **_TARGET),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_guest_access(host, project, instance_name)
    body = await read_json_body(request, PasswordSetBody)
    async with audited("guest.password.set", host=host, project=bound, instance=instance_name,
                       details={"user": body.user, "must_change": body.must_change}) as record:
        async with http_failures():
            flows.password_or_refuse(body.password.get_secret_value(), context=[body.user, instance_name])
        async with admitted(user, host, bound, instance_name, kind="password", record=record) as guest:
            async with write_lock(host, bound, instance_name):
                facts = await flows.facts_of(guest)
                account = flows.account_or_refuse(facts, body.user)
                record.update(uid=account.uid, kind=account.kind)
                result = await flows.set_password(
                    guest, facts, account, body.password.get_secret_value(), body.must_change, instance_name,
                )
                record["change"] = "set"
                return result


@router.post("/password/lock", response_model=PasswordResponse, summary="Lock an account's password",
             dependencies=[Depends(check_read_only_mode)], openapi_extra=body_spec(AccountBody))
async def lock_password(
    request: Request,
    host: str = Query(..., max_length=255),
    project: str = Query("default", max_length=255),
    instance_name: str = Query(..., **_TARGET),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_guest_access(host, project, instance_name)
    body = await read_json_body(request, AccountBody)
    async with audited("guest.password.lock", host=host, project=bound, instance=instance_name,
                       details={"user": body.user}) as record:
        async with admitted(user, host, bound, instance_name, kind="write", record=record) as guest:
            async with write_lock(host, bound, instance_name):
                facts = await flows.facts_of(guest)
                account = flows.account_or_refuse(facts, body.user)
                record.update(uid=account.uid, kind=account.kind)
                await accounts.lock(guest, facts, account)
                record["change"] = "locked"
                warnings = await flows.lock_warnings(guest)
    return {"user": account.name, "password": "locked", "must_change_applied": False, "warnings": warnings}


@router.post("/users/sudo", response_model=SudoResponse, summary="Grant or revoke an account's sudo group",
             dependencies=[Depends(check_read_only_mode)], openapi_extra=body_spec(SudoBody))
async def set_sudo(
    request: Request,
    host: str = Query(..., max_length=255),
    project: str = Query("default", max_length=255),
    instance_name: str = Query(..., **_TARGET),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_guest_access(host, project, instance_name)
    body = await read_json_body(request, SudoBody)
    async with audited("guest.account.sudo", host=host, project=bound, instance=instance_name,
                       details={"user": body.user, "sudo": body.enabled}) as record:
        async with admitted(user, host, bound, instance_name, kind="write", record=record) as guest:
            async with write_lock(host, bound, instance_name):
                facts = await flows.facts_of(guest)
                account = flows.account_or_refuse(facts, body.user)
                if account.kind != "human":
                    raise errors.refuse("sudo_not_applicable", 409, "This account is already root.")
                record.update(uid=account.uid, kind=account.kind)
                await accounts.set_sudo(guest, facts, account, body.enabled)
                facts = await flows.facts_of(guest)
                account = flows.account_or_refuse(facts, body.user)
                verdict = await flows.sudo_verdict(guest, facts, account)
                record["outcome"] = verdict
    return {"user": account.name, "sudo": verdict, "admin_groups": account.admin_groups}


@router.post("/users/create", response_model=CreateUserResponse, status_code=201,
             summary="Create a local account", dependencies=[Depends(check_read_only_mode)],
             openapi_extra=body_spec(CreateUserBody))
async def create_user(
    request: Request,
    host: str = Query(..., max_length=255),
    project: str = Query("default", max_length=255),
    instance_name: str = Query(..., **_TARGET),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_guest_access(host, project, instance_name)
    body = await read_json_body(request, CreateUserBody)
    password = body.password.get_secret_value() if body.password is not None else None
    async with audited("guest.account.create", host=host, project=bound, instance=instance_name,
                       details={"user": body.name, "sudo": body.sudo, "must_change": body.must_change}) as record:
        async with http_failures():
            flows.name_or_refuse(body.name)
            if password is not None:
                flows.password_or_refuse(password, context=[body.name, instance_name])
            key = flows.key_or_refuse(body.public_key) if body.public_key else None
        if key is not None:
            record.update(fingerprint=key.fingerprint, key_type=key.key_type)
        kind = "password" if password is not None else "write"
        async with admitted(user, host, bound, instance_name, kind=kind, record=record) as guest:
            async with write_lock(host, bound, instance_name):
                facts = await flows.facts_of(guest)
                result = await flows.create_user(
                    guest, facts, body.name, sudo=body.sudo, password=password, must_change=body.must_change,
                    key=key, instance=instance_name,
                )
                record["steps"] = [f"{s['step']}:{s['result']}" for s in result["steps"]]
                if result["account"] is not None:
                    record["uid"] = result["account"]["uid"]
                return result
