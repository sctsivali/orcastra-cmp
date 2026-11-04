"""Shared migration worker contracts, context, and bounded I/O."""

from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from app.core.blocking import run_blocking

from .authority import ActorIdentityPin, MigrationAuthorityPin

logger = logging.getLogger("app.services.migrations.orchestrator")

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="migration")
_MAX_IN_FLIGHT = 4
_BLOCKING_TIMEOUT = 90
_HEARTBEAT_SECONDS = 30

class MigrationConflict(RuntimeError):
    pass


class MigrationNotFound(RuntimeError):
    pass


class MigrationAuthorityError(RuntimeError):
    def __init__(self, message: str, *, code: str = "authorization_unverifiable") -> None:
        super().__init__(message)
        self.code = code


class MigrationUnknown(RuntimeError):
    pass


class MigrationLeaseLost(RuntimeError):
    pass


class MigrationAuthorityUnavailable(MigrationUnknown):
    """The identity provider could not confirm authority right now. Retry later."""

    def __init__(self, message: str, *, code: str = "authority_unavailable") -> None:
        super().__init__(message)
        self.code = code


class MigrationClusterUnavailable(MigrationUnknown):
    """A cluster, or the store holding its certificate pin, did not answer. Nothing was learned.

    It says nothing about the job's outcome, so a job that had not changed anything yet waits
    and retries, and one that had backs off and reconciles, instead of entering manual recovery.
    """

    def __init__(self, message: str, *, code: str = "cluster_unavailable") -> None:
        super().__init__(message)
        self.code = code


class MigrationPreMutationRefusal(MigrationConflict):
    """The worker refused a queued job before it changed anything, so the job ends cleanly."""

    def __init__(self, message: str, *, code: str = "preflight_blocked") -> None:
        super().__init__(message)
        self.code = code


class TransferFailed(RuntimeError):
    """The daemon definitively refused or failed a step before any target was activated."""


class TargetStartRefused(TransferFailed):
    """The daemon refused to start a staged target, which is still stopped: it never ran."""

    code = "target_start_refused"


_worker_fence: contextvars.ContextVar[Optional[tuple[str, str, int]]] = (
    contextvars.ContextVar("migration_worker_fence", default=None)
)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _actor_id(user: Any) -> str:
    return str(getattr(user, "email", None) or getattr(user, "user_id", ""))


def _authority_values(authority: Any) -> dict[str, str]:
    pin = authority.pin
    return {
        "requester": pin.identity.email,
        "requester_role": authority.role.value,
        "actor_issuer": pin.identity.issuer,
        "actor_subject": pin.identity.subject,
        "actor_email": pin.identity.email,
        "actor_provider_uuid": pin.provider_uuid,
    }


def _authority_pin(row: Any) -> MigrationAuthorityPin:
    values = (
        row.actor_issuer,
        row.actor_subject,
        row.actor_email,
        row.actor_provider_uuid,
    )
    if any(not isinstance(value, str) or not value for value in values):
        raise MigrationAuthorityError(
            "The migration has no complete current-authority identity pin."
        )
    return MigrationAuthorityPin(
        identity=ActorIdentityPin(
            issuer=row.actor_issuer,
            subject=row.actor_subject,
            email=row.actor_email,
        ),
        provider_uuid=row.actor_provider_uuid,
    )


async def _blocking(fn: Any, *args: Any, label: str, timeout: int = _BLOCKING_TIMEOUT, **kwargs: Any) -> Any:
    return await run_blocking(
        fn,
        *args,
        timeout=timeout,
        label=label,
        executor=_EXECUTOR,
        max_in_flight=_MAX_IN_FLIGHT,
        **kwargs,
    )


def _assert_job_plan_pin(job: Any, plan: Any) -> None:
    """Require the immutable plan identity captured when the job was accepted."""
    job_hash = getattr(job, "plan_payload_hash", None)
    plan_hash = getattr(plan, "payload_hash", None)
    if (
        getattr(job, "plan_id", None) != getattr(plan, "plan_id", None)
        or getattr(job, "plan_revision", None) != getattr(plan, "revision", None)
        or not isinstance(job_hash, str)
        or not job_hash
        or not isinstance(plan_hash, str)
        or not plan_hash
        or job_hash != plan_hash
    ):
        raise MigrationAuthorityError(
            "The durable migration plan identity no longer matches the accepted job.",
            code="migration_plan_pin_mismatch",
        )
