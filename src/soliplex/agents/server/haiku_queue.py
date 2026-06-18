"""Global FIFO queue that serializes haiku-rag loads.

Only one load may run at a time (capacity constraint), so manifest runs
enqueue their manifest here and a single background worker drains the
queue in order. Loads run outside any per-manifest lock; serialization is
guaranteed by the single worker.
"""

import asyncio
import logging

from soliplex.agents.config import Manifest
from soliplex.agents.manifest import haiku_loader

logger = logging.getLogger(__name__)

_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None


async def enqueue_load(manifest: Manifest) -> None:
    """Queue a haiku-rag load for *manifest*.

    No-op (with a warning) if the worker has not been started, so manifest
    runs never fail just because loads are disabled.
    """
    if _queue is None:
        logger.warning(
            "haiku load queue not started; skipping load for '%s'",
            manifest.source,
        )
        return
    await _queue.put(manifest)
    logger.info(
        "Queued haiku load for source '%s' (queue size=%d)",
        manifest.source,
        _queue.qsize(),
    )


async def _worker() -> None:
    """Drain the queue, running one load at a time."""
    assert _queue is not None
    while True:
        manifest = await _queue.get()
        try:
            await haiku_loader.run_load(manifest)
        except Exception:
            logger.exception(
                "Unhandled error during haiku load for '%s'",
                manifest.source,
            )
        finally:
            _queue.task_done()


def start_worker() -> None:
    """Create the queue and start the single load worker task."""
    global _queue, _worker_task
    if _worker_task is not None:
        return
    _queue = asyncio.Queue()
    _worker_task = asyncio.create_task(_worker(), name="haiku_load_worker")
    logger.info("Started haiku load worker")


async def stop_worker() -> None:
    """Cancel the worker task and reset queue state."""
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
    logger.info("Stopped haiku load worker")
