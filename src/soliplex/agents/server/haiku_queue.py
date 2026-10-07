"""Global FIFO queue that serializes haiku-rag loads.

Only one load may run at a time (capacity constraint), so manifest runs
enqueue their manifest here and a single background worker drains the
queue in order. Loads run outside any per-manifest lock; serialization is
guaranteed by the single worker.
"""

import asyncio
import logging
import time

from opentelemetry import context as otel_context

from soliplex.agents import alerts
from soliplex.agents.config import Manifest
from soliplex.agents.manifest import haiku_loader
from soliplex.agents.manifest import runner
from soliplex.agents.manifest.callables import describe_error

logger = logging.getLogger(__name__)

_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None


async def enqueue_load(manifest: Manifest, run_result: dict | None = None) -> None:
    """Queue a haiku-rag load for *manifest*.

    *run_result* -- the manifest run that asked for the load -- travels with it
    and reaches post-process callbacks that accept ``run_result``.

    No-op (with a warning) if the worker has not been started, so manifest
    runs never fail just because loads are disabled.

    The caller's trace context travels with the manifest: the load runs later,
    on another task, and attaching that context there makes the load's spans
    children of the manifest run that queued it, in the same trace.
    """
    if _queue is None:
        logger.warning(
            "haiku load queue not started; skipping load for '%s'",
            manifest.source,
        )
        return
    await _queue.put((manifest, run_result, otel_context.get_current(), time.monotonic()))
    logger.info(
        "Queued haiku load for source '%s' (queue size=%d)",
        manifest.source,
        _queue.qsize(),
    )


async def _worker() -> None:
    """Drain the queue, running one load at a time."""
    assert _queue is not None
    while True:
        manifest, run_result, parent, enqueued_at = await _queue.get()
        token = otel_context.attach(parent)
        try:
            queue_wait_s = time.monotonic() - enqueued_at
            logger.info(
                "Starting queued haiku load for source '%s' after %.1fs in the queue",
                manifest.source,
                queue_wait_s,
            )
            result = await haiku_loader.run_load(manifest, queue_wait_s=queue_wait_s, run_result=run_result)
        except Exception as e:
            logger.exception(
                "Unhandled error during haiku load for '%s'",
                manifest.source,
            )
            alerts.manifest_failed(
                manifest_id=manifest.id,
                path=manifest.manifest_path,
                stage=alerts.Stage.HAIKU_LOAD,
                reasons=[f"haiku load: {describe_error(e)}"],
                source=manifest.source,
            )
        else:
            if result.get("post_process_error"):
                # The step's traceback is already logged where it raised.
                logger.error("Post-process failed for '%s': %s", manifest.source, result["post_process_error"])
            _report_outcome(manifest, run_result, result)
        finally:
            otel_context.detach(token)
            _queue.task_done()


def _report_outcome(manifest: Manifest, run_result: dict | None, load_result: dict) -> None:
    """Emit the operator alert for a manifest whose queued load has finished.

    A failed load (or post-process step) alerts on its own. Otherwise the
    manifest completed -- unless its run had failures, which
    :mod:`.manifest_queue` alerted on when the run finished (the load ran
    only because ``HAIKU_LOAD_ON_ERROR`` let it), and which a clean load does
    not undo.
    """
    stage, reasons = runner.load_failures(load_result)
    if reasons:
        alerts.manifest_failed(
            manifest_id=manifest.id,
            path=manifest.manifest_path,
            stage=stage,
            reasons=reasons,
            source=manifest.source,
        )
        return
    run_result = run_result or {}
    _, run_reasons = runner.run_failures(run_result)
    if not run_reasons:
        runner.report_outcome(manifest, {**run_result, "haiku_load": load_result})


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
