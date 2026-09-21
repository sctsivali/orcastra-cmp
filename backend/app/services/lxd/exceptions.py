"""
Custom exceptions for LXD operations.
"""


class LXDConnectionError(Exception):
    """Exception raised when LXD connection fails."""
    pass


class LXDLocalUnavailableError(LXDConnectionError):
    """The control-plane worker could not start or finish a bounded LXD read.

    This is distinct from a remote cluster failure. Fleet callers must not cache it as an
    unreachable cluster because another worker, or the same worker after its background healer
    runs, may already have the connection needed to answer.
    """
    pass


class LXDAuthorizationError(LXDConnectionError):
    """The host answered our probe and refused our certificate.

    important: subclasses LXDConnectionError so every existing `except LXDConnectionError`
    handler keeps working unchanged. It exists only so a caller can tell "answered and said
    no" from "never answered" without matching on message text. That distinction is what
    decides whether a cluster reads as Pending Trust or Offline.
    """
    pass


class LXDLocalStorageError(Exception):
    """The backend could not write the TLS material a client needs, on its own disk.

    important: deliberately NOT a subclass of LXDConnectionError. Roughly sixty handlers catch
    that type and read it as a verdict about the cluster, and connect_with_vault_cert records it
    against the circuit breaker as KIND_OFFLINE. A full /tmp is neither: the peer was never
    contacted. Reported as a connection error it read as "every cluster is offline" while the
    hypervisors were healthy, and the breaker then suppressed the retries that would have shown
    otherwise. Keeping it off that hierarchy is what forces callers to say something true.
    """
    pass


class LXDOperationError(Exception):
    """Exception raised when an LXD operation fails."""
    pass


class LXDOperationUnknownError(LXDOperationError):
    """A project write may have reached LXD but exact readback could not prove it."""

    def __init__(self, operation: str, resource: str) -> None:
        self.operation = operation
        self.resource = resource
        # Keep transport, endpoint, and daemon details out of API responses and audit logs.
        super().__init__(
            f"The {operation} outcome for project '{resource}' could not be confirmed"
        )


class LXDGracefulTimeoutError(LXDOperationError):
    """A graceful stop/restart timed out waiting for the guest to shut down.

    Distinct from a generic operation failure because it is recoverable in a
    specific way: the caller can retry with force. It most often means the guest
    never answered the ACPI shutdown request -- e.g. a VM with no OS installed,
    or one sitting at a boot menu/UEFI shell, which has nothing listening.
    """
    pass


class LXDSnapshotExistsError(LXDOperationError):
    """A rebuild was blocked because the instance still has snapshots.

    LXD refuses to rebuild an instance that has snapshots. This is recoverable in
    a specific way (delete the snapshots first), so the router maps it to a clear
    409 that lists them rather than a generic failure.
    """
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        names = ", ".join(self.snapshots) if self.snapshots else ""
        super().__init__(
            f"Cannot rebuild: the instance has {len(self.snapshots)} snapshot(s) "
            f"({names}). Delete them first, then retry the rebuild."
        )


class LXDProjectLimitError(LXDOperationError):
    """A create was refused by a project limit (limits.cpu, limits.memory, an instance count).

    Recoverable in a specific way: the operator lowers the request or picks another project.
    The router maps it to a 400 whose detail names the limit, the project and the maximum, so
    the form can mark the field rather than show the daemon's sentence and nothing else.
    """

    def __init__(self, message: str, hit):
        self.hit = hit
        super().__init__(message)


class LXDProjectNotFoundError(LXDOperationError):
    """The daemon has no project by that name (deleted after the caller's policy last saw it).

    The router answers 404, the same as the visibility gate, so a stale name never reads as a
    server fault.
    """

    def __init__(self, project: str):
        self.project = project
        super().__init__(f"Project '{project}' not found")
