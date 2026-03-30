"""Manifest execution lock registry.

Provides per-manifest-ID locks that prevent the same manifest from
running concurrently across cron jobs, HTTP routes, and startup tasks.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_manifest_locks: dict[str, asyncio.Lock] = {}

_scheduler_lock = asyncio.Lock()


def get_manifest_lock(manifest_id: str) -> asyncio.Lock:
    """Return the lock for *manifest_id*, creating it on first access."""
    if manifest_id not in _manifest_locks:
        _manifest_locks[manifest_id] = asyncio.Lock()
    return _manifest_locks[manifest_id]


def is_manifest_running(manifest_id: str) -> bool:
    """Check whether *manifest_id* is currently executing."""
    lock = _manifest_locks.get(manifest_id)
    return lock is not None and lock.locked()


def reset_locks() -> None:
    """Clear all locks.  Intended for testing only."""
    _manifest_locks.clear()
