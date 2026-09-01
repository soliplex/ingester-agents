"""Global FIFO queue that serializes manifest runs.

Only one manifest may run at a time. The scheduler enqueues due manifests
here and a single background worker drains the queue in order, so mutual
exclusion is *structural* -- the worker does one thing at a time -- rather
than enforced by a lock that callers have to remember to take. A manifest
whose turn comes up while another is running therefore waits behind it
instead of being dropped, and no scheduled occurrence is silently lost.

A manifest already queued or running is coalesced rather than queued a
second time: the pending run will pick up the same work from disk anyway.
That also bounds the queue at the number of manifests on disk, so a cron
faster than its own runs cannot grow a backlog.
"""

import asyncio
import logging

from soliplex.agents.config import settings

from .haiku_queue import enqueue_load

logger = logging.getLogger(__name__)

_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None
# Ids queued but not yet finished, including the one currently running, so a
# manifest is never represented in the queue twice.
_pending: set[str] = set()


async def enqueue_manifest(manifest_id: str, path: str) -> None:
    """Queue *manifest_id* to run, unless it is already queued or running.

    No-op (with a warning) if the worker has not been started, so a
    reconcile pass never fails just because the scheduler is disabled.

    Args:
        manifest_id: The manifest's id, used for coalescing and logging.
        path: Path to the manifest YAML, reloaded fresh when it runs.
    """
    if _queue is None:
        logger.warning(
            "manifest run queue not started; dropping run for '%s'",
            manifest_id,
        )
        return
    if manifest_id in _pending:
        logger.info(
            "Manifest '%s' already queued or running; coalescing this run",
            manifest_id,
        )
        return
    _pending.add(manifest_id)
    await _queue.put((manifest_id, path))
    logger.info(
        "Queued manifest '%s' (queue size=%d)",
        manifest_id,
        _queue.qsize(),
    )


def pending_manifests() -> frozenset[str]:
    """Ids currently queued or running (snapshot, for tests and diagnostics)."""
    return frozenset(_pending)


async def run_manifest_now(manifest_id: str, path: str) -> None:
    """Load *path* fresh and execute it, then queue its haiku load.

    Raises on failure; the worker owns the logging and recovery.

    Args:
        manifest_id: The manifest's id (for logging).
        path: Path to the manifest YAML file.
    """
    from soliplex.agents.manifest import runner as manifest_runner

    loaded = manifest_runner.load_manifest(path)
    result = await manifest_runner.run_manifest(loaded)
    logger.info(
        "Manifest '%s' completed: %d components",
        manifest_id,
        len(result.get("results", [])),
    )
    if settings.haiku_load_enabled:
        await enqueue_load(loaded)


async def _worker() -> None:
    """Drain the queue, running one manifest at a time."""
    assert _queue is not None
    while True:
        manifest_id, path = await _queue.get()
        try:
            await run_manifest_now(manifest_id, path)
        except Exception:
            logger.exception("Error running manifest '%s'", manifest_id)
        finally:
            # Clear the id before task_done so a manifest that is due again
            # can be re-queued as soon as this run is off the queue.
            _pending.discard(manifest_id)
            _queue.task_done()


def start_worker() -> None:
    """Create the queue and start the single manifest-run worker task."""
    global _queue, _worker_task
    if _worker_task is not None:
        return
    _queue = asyncio.Queue()
    _pending.clear()
    _worker_task = asyncio.create_task(_worker(), name="manifest_run_worker")
    logger.info("Started manifest run worker")


async def stop_worker() -> None:
    """Cancel the worker task and reset queue state.

    Cancelling interrupts the in-flight manifest rather than waiting for it,
    so shutdown stays bounded; queued manifests are dropped and will be
    picked up again by the reconciler on the next start. The worker is
    awaited after cancellation, so the run is torn down deterministically
    instead of being orphaned.
    """
    global _queue, _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except asyncio.CancelledError:
        pass
    _worker_task = None
    _queue = None
    _pending.clear()
    logger.info("Stopped manifest run worker")
