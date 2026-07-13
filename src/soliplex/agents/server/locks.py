"""Manifest execution lock registry.

Provides per-manifest-ID locks that prevent the same manifest from
running concurrently across cron jobs, HTTP routes, and startup tasks,
plus a process-global semaphore that serializes *all* manifest execution
so at most one manifest runs at a time within the process.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_manifest_locks: dict[str, asyncio.Lock] = {}

# Process-global semaphore (size 1) that serializes ALL manifest execution
# regardless of manifest ID. Where the per-ID locks above stop a single
# manifest from overlapping itself, this stops *different* manifests from
# running concurrently. It also relies on the server running as a single
# process (see ``soliplex.agents.cli.serve``); with multiple worker
# processes each would have its own semaphore and coordination would be lost.
_global_manifest_semaphore = asyncio.Semaphore(1)


def get_manifest_lock(manifest_id: str) -> asyncio.Lock:
    """Return the lock for *manifest_id*, creating it on first access."""
    if manifest_id not in _manifest_locks:
        _manifest_locks[manifest_id] = asyncio.Lock()
    return _manifest_locks[manifest_id]


def is_manifest_running(manifest_id: str) -> bool:
    """Check whether *manifest_id* is currently executing."""
    lock = _manifest_locks.get(manifest_id)
    return lock is not None and lock.locked()


def get_global_manifest_semaphore() -> asyncio.Semaphore:
    """Return the process-global manifest-execution semaphore.

    Acquire it (``async with``) around every manifest run so only one
    manifest executes at a time, no matter which ID it is.
    """
    return _global_manifest_semaphore


def is_any_manifest_running() -> bool:
    """Return True if any manifest currently holds the global semaphore."""
    return _global_manifest_semaphore.locked()


def reset_locks() -> None:
    """Clear all locks.  Intended for testing only."""
    global _global_manifest_semaphore
    _manifest_locks.clear()
    _global_manifest_semaphore = asyncio.Semaphore(1)
