"""Stable refusals for the credentials routes. Codes the interface branches on, messages written here.

important: no message here ever contains what the caller sent or what the guest printed. A password
one character short, a pasted private key or a hostile guest's stderr would otherwise come back in
the response, the proxy log and the audit trail.
"""

from typing import Any, Dict, Optional

from app.services.instance_files.errors import GuestFileError


class CredentialError(GuestFileError):
    """Same shape and `to_http` as the file explorer's refusals, so one handler maps both."""


def refuse(code: str, status: int, message: str, extra: Optional[Dict[str, Any]] = None) -> CredentialError:
    return CredentialError(code, status, message, dict(extra or {}))


def not_running(status_text: str) -> CredentialError:
    if status_text == "Frozen":
        return refuse("instance_frozen", 409, "This instance is frozen. Resume it to manage its logins.")
    return refuse("instance_not_running", 409, "This instance is not running. Start it to manage its logins.")


def unsupported_os() -> CredentialError:
    return refuse(
        "guest_os_unsupported", 409,
        "Windows guests are not supported here. Manage their accounts from the Graphics console.",
    )


def agent_unavailable() -> CredentialError:
    return refuse("agent_unavailable", 503, "The guest agent is not reachable inside this virtual machine.")


def shell_missing() -> CredentialError:
    return refuse("guest_shell_missing", 409, "This guest has no /bin/sh, so its accounts cannot be managed here.")


def cluster_unreachable() -> CredentialError:
    return refuse("cluster_unreachable", 503, "The cluster did not answer.")


def cluster_not_pinned() -> CredentialError:
    return refuse(
        "cluster_not_pinned", 503, "This cluster's certificate could not be confirmed, so nothing was sent."
    )


def guest_timeout() -> CredentialError:
    return refuse("guest_timeout", 504, "The guest did not answer in time. It may be busy or still starting.")


def outcome_unknown() -> CredentialError:
    # important: the change was sent and no answer came back. Saying it failed could make someone
    # think an old password still works, so the answer says what is actually known.
    return refuse(
        "credential_outcome_unknown", 504,
        "The change was sent but the guest did not confirm it. Check the account before relying on it.",
    )


def busy(retry_after: int = 3) -> CredentialError:
    return refuse(
        "busy", 429, "Too many account changes are running. Trying again shortly usually works.",
        {"retry_after": retry_after},
    )


def rate_limited(retry_after: int = 60) -> CredentialError:
    return refuse(
        "rate_limited", 429, "Too many requests in a short time. Wait a moment and try again.",
        {"retry_after": retry_after},
    )


def guest_failed(code: str = "guest_command_failed") -> CredentialError:
    return refuse(code, 502, "The guest could not complete this change.")


def guest_answer_invalid() -> CredentialError:
    return refuse("guest_protocol_error", 502, "The guest answered in a way this service does not accept.")


def tool_missing(tool: str) -> CredentialError:
    # note: `tool` is one of this package's own names (useradd, chpasswd, ...), never guest text.
    return refuse("tool_missing", 409, "This guest lacks a tool this change needs.", {"tool": tool})


def user_not_found() -> CredentialError:
    return refuse("user_not_found", 404, "There is no local account with this name in the guest.")


def user_not_manageable() -> CredentialError:
    return refuse(
        "user_not_manageable", 409,
        "This account is a system account or has no login shell, so it is not managed here.",
    )


def user_exists() -> CredentialError:
    return refuse("user_exists", 409, "An account or group with this name already exists in the guest.")


def home_not_owned() -> CredentialError:
    return refuse(
        "home_not_owned", 409,
        "This account's home folder is missing or belongs to someone else, so no key was written.",
    )


def ssh_dir_not_owned() -> CredentialError:
    return refuse(
        "ssh_dir_not_owned", 409,
        "The .ssh folder of this account is not a folder it owns, so no key was written.",
    )


def key_file_is_link() -> CredentialError:
    return refuse(
        "key_file_is_link", 409,
        "authorized_keys of this account is a link. Replace it with a regular file to manage keys here.",
    )


def key_file_not_regular() -> CredentialError:
    return refuse("key_file_not_regular", 409, "authorized_keys of this account is not a regular file.")


def key_file_too_large(limit: int) -> CredentialError:
    return refuse(
        "key_file_too_large", 413, "authorized_keys of this account is larger than this panel handles.",
        {"limit_bytes": limit},
    )


def edit_conflict() -> CredentialError:
    return refuse(
        "edit_conflict", 409, "authorized_keys changed while this edit ran. Reload the list and try again.",
    )


def key_not_found() -> CredentialError:
    return refuse("key_not_found", 404, "No key with this fingerprint is installed for this account.")
