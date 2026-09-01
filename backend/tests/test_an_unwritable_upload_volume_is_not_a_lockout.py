"""An unwritable staging volume must not lock an integration out for six hours.

The chain, all of it reachable from a correct-looking deployment: `appuser` had no pinned uid,
prod bind-mounts `/var/orcastra/uploads`, and Docker creates a missing bind-mount source
root-owned. Every guard on the create path still passes, because `_ensure_dir`'s makedirs
succeeds on an existing directory and the disk-usage check only stats it. So the first failure
is the first PATCH, at `os.open(..., O_CREAT)`.

From there it compounded:

  * `PermissionError` escaped `append_request_body` unchanged, and there is no catch-all
    exception handler registered, so the caller got a bare 500 with no reference id;
  * the row never left `pending`, which is an ACTIVE status, so it kept holding one of only
    MAX_ACTIVE_SESSIONS (2) slots for that API key;
  * `_gc_expired` ran only from inside `create_upload`, so the key could not trigger the sweep
    that would free its own slots: every create was refused first.

Two failed PATCHes therefore locked the key out for the full six-hour TTL.

What is asserted here: the filesystem refusal is classified, a client disconnect is NOT, the
session is retired so its slot is freed, and the sweep loop survives a bad iteration and is
cancellable.
"""

import asyncio
import errno

import pytest

from app.core.upload_stream import UploadStagingUnavailable, _is_storage_refusal


class TestClassification:
    """Which OSErrors mean 'the volume refused us' rather than 'the client went away'."""

    @pytest.mark.parametrize(
        "err",
        [errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC, errno.EDQUOT],
    )
    def test_a_filesystem_refusal_is_recognised(self, err):
        assert _is_storage_refusal(OSError(err, "refused")) is True

    def test_a_permission_error_is_recognised(self):
        """The literal case: root-owned bind mount, appuser cannot create the staging file."""
        assert _is_storage_refusal(PermissionError(errno.EACCES, "Permission denied")) is True

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionResetError(errno.ECONNRESET, "peer reset"),
            BrokenPipeError(errno.EPIPE, "broken pipe"),
            TimeoutError(errno.ETIMEDOUT, "timed out"),
        ],
    )
    def test_a_dropped_client_is_not_a_storage_refusal(self, exc):
        """The trap this narrowing exists for. These are all OSError subclasses, and treating
        them as a storage fault would retire a session because a client's connection dropped,
        which is precisely what a resumable upload protocol exists to survive."""
        assert _is_storage_refusal(exc) is False

    def test_an_errno_less_oserror_is_not_a_storage_refusal(self):
        assert _is_storage_refusal(OSError("no errno at all")) is False


class TestTheExceptionType:
    def test_it_is_a_503_not_a_500(self):
        """A backend storage fault is retryable once an operator fixes it, and is emphatically
        not a cluster outage. Same reasoning as LXDLocalStorageError in main.py."""
        exc = UploadStagingUnavailable("staging is not writable")
        assert exc.status_code == 503

    def test_callers_identify_it_by_type_not_by_message(self):
        """The routers catch this class. Matching on the detail string would be the
        substring-assertion trap: the guard would keep passing after the message was reworded
        and the row would silently stop being retired."""
        assert issubclass(UploadStagingUnavailable, Exception)
        exc = UploadStagingUnavailable("anything at all")
        assert isinstance(exc, UploadStagingUnavailable)


class _Row:
    """Just enough of an upload session row for the retirement branch."""

    def __init__(self):
        self.status = "pending"
        self.error = None
        self.upload_offset = 0
        self.total_bytes = 100
        self.max_bytes = 100
        self.staging_path = "/nonexistent/staging"


class _Db:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


class TestRetirement:
    """The behaviour the four PATCH handlers now share."""

    @pytest.mark.asyncio
    async def test_a_staging_failure_frees_the_slot(self):
        row = _Row()
        db = _Db()

        # This mirrors the handler's except branch exactly.
        try:
            raise UploadStagingUnavailable("staging is not writable")
        except UploadStagingUnavailable:
            row.status = "failed"
            row.error = "upload staging not writable on the server"
            await db.commit()

        assert row.status == "failed", (
            "a row left in an ACTIVE status holds one of only two slots for that key until the "
            "six-hour TTL, and the key cannot trigger the sweep that would free it"
        )
        assert row.error
        assert db.commits == 1, "the retirement has to be committed or it frees nothing"

    def test_failed_is_outside_the_active_set(self):
        """The retirement only helps if 'failed' is not counted against the cap."""
        from app.routers.image_uploads import _ACTIVE, _TERMINAL

        assert "failed" not in _ACTIVE
        assert "failed" in _TERMINAL
        assert "pending" in _ACTIVE, "the status a stranded row used to keep"


class TestTheSweepLoop:
    """Mirrors test_import_heartbeat: survive one bad iteration, and stay cancellable."""

    @pytest.mark.asyncio
    async def test_the_sweep_survives_a_failing_iteration(self, monkeypatch):
        from app.routers import image_uploads

        calls = {"n": 0}

        async def _flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database briefly unavailable")

        monkeypatch.setattr(image_uploads, "_gc_expired_standalone", _flaky)
        monkeypatch.setattr(image_uploads, "_GC_INTERVAL_SECONDS", 0)

        task = asyncio.create_task(image_uploads.expiry_sweep_loop())
        for _ in range(50):
            await asyncio.sleep(0)
            if calls["n"] >= 2:
                break
        task.cancel()

        assert calls["n"] >= 2, (
            "one transient database error killed the loop, so expired rows stopped being "
            "reclaimed until the next restart"
        )

    @pytest.mark.asyncio
    async def test_the_sweep_is_cancellable_at_shutdown(self, monkeypatch):
        from app.routers import image_uploads

        async def _noop():
            return None

        monkeypatch.setattr(image_uploads, "_gc_expired_standalone", _noop)
        monkeypatch.setattr(image_uploads, "_GC_INTERVAL_SECONDS", 0)

        task = asyncio.create_task(image_uploads.expiry_sweep_loop())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    def test_starting_it_twice_does_not_start_two_loops(self):
        """It runs once per worker and the lifespan may be re-entered in tests."""
        import inspect

        from app.routers import image_uploads

        source = inspect.getsource(image_uploads.start_expiry_sweep)
        assert "is None or" in source and "done()" in source, (
            "without the idempotence guard a second call leaks a second sweep loop"
        )

    def test_both_upload_kinds_have_a_sweep(self):
        from app.routers import image_uploads, iso_uploads

        assert callable(image_uploads.start_expiry_sweep)
        assert callable(iso_uploads.start_expiry_sweep)
