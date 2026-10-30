"""Removing a tree a slice at a time, so no request outlives the proxy in front of it.

SFTP has no recursive remove and LXD's own Files API DELETE is one `Remove`, so a folder of 100,000
files is 100,000 round trips. A slice works until its budget and says whether anything is left, and
the client calls again. The tree itself is the progress record, which is why there is no job table.
"""

import asyncio
import logging
from contextlib import aclosing
from typing import Any, Dict, List

from .errors import GuestFileError
from .paths import MAX_PATH_BYTES, from_guest, is_safe_entry, refuse_protected, to_guest
from .sftp_client import Budget, BudgetSpent, SftpClient, is_directory

logger = logging.getLogger(__name__)

_PARALLEL = 32
_MAX_FAILED_REPORTED = 16


async def delete_path(
    guest: SftpClient,
    path: str,
    *,
    recursive: bool,
    budget_seconds: float = 20.0,
    max_deletions: int = 50_000,
) -> Dict[str, Any]:
    refuse_protected([path])
    raw = to_guest(path)
    attrs = await guest.lstat(raw)
    if not is_directory(attrs):
        await guest.remove(raw)
        return {"deleted": 1, "failed": [], "failed_count": 0, "remaining": False}
    if not recursive:
        await guest.rmdir(raw)
        return {"deleted": 1, "failed": [], "failed_count": 0, "remaining": False}

    budget = Budget(entries=max_deletions * 2, seconds=budget_seconds)
    gate = asyncio.Semaphore(_PARALLEL)
    deleted = 0
    failed: List[str] = []
    failed_count = 0
    remaining = False
    visited: List[bytes] = []
    # The real path, so a folder swapped for a link during the walk can be told from the original.
    base = await guest.realpath(raw)
    stack = [base]

    def note_failure(target: bytes) -> None:
        nonlocal failed_count
        failed_count += 1
        if len(failed) < _MAX_FAILED_REPORTED:
            failed.append(from_guest(target))

    async def remove(target: bytes) -> None:
        nonlocal deleted
        async with gate:
            try:
                await guest.remove(target)
                deleted += 1
            except GuestFileError as failure:
                if failure.status >= 500:
                    raise
                if failure.code != "not_found":
                    note_failure(target)

    try:
        while stack:
            if deleted >= max_deletions:
                raise BudgetSpent()
            folder = stack.pop()
            # important: LXD's server follows links in the middle of a path, so `rm -rf`'s classic
            # race applies: a folder swapped for a link to /etc would have /etc emptied. SFTP has no
            # unlinkat, so this narrows that window to one directory rather than closing it.
            if await guest.realpath(folder) != folder:
                note_failure(folder)
                continue
            visited.append(folder)
            children: List[bytes] = []
            async with aclosing(guest.iter_directory(folder, budget)) as batches:
                async for batch in batches:
                    for item in batch:
                        if not is_safe_entry(item.filename):
                            continue
                        child = folder.rstrip(b"/") + b"/" + item.filename
                        if len(child) > MAX_PATH_BYTES:
                            note_failure(folder)
                            continue
                        budget.hold(len(child))
                        # note: a link is removed, never entered: readdir reports it as a link.
                        if is_directory(item.attrs):
                            stack.append(child)
                        else:
                            children.append(child)
            await asyncio.gather(*(remove(child) for child in children))
    except BudgetSpent:
        remaining = True

    for folder in reversed(visited):
        try:
            await guest.rmdir(folder)
            deleted += 1
        except GuestFileError as failure:
            if failure.status >= 500:
                raise
            # Not empty is expected when the walk stopped early or a file refused to go.
            if failure.code not in ("directory_not_empty", "not_found") and not remaining:
                note_failure(folder)

    if remaining and deleted == 0:
        raise GuestFileError(
            "delete_stuck", 409, "Nothing here could be removed. Files may be immutable or in use.",
            {"failed": failed, "failed_count": failed_count},
        )
    return {"deleted": deleted, "failed": failed, "failed_count": failed_count, "remaining": remaining}
