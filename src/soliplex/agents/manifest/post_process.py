"""Invoke a manifest's ``config.post_process`` callbacks after a load.

Each step names a dotted-path callable (``pkg.mod:func`` or ``pkg.mod.func``)
that is invoked as ``method(source, **kwargs)`` -- ``source`` is the manifest's
source, ``kwargs`` are the configured extra args. Steps run **in order** after
``haiku-ingester`` finishes (see :func:`haiku_loader.run_load`).

Three things a step does not have to arrange for itself:

* **config auto-inject** -- when a step omits ``config`` and the callable
  accepts one (an explicit ``config`` parameter or ``**kwargs``), the manifest's
  resolved haiku config path is passed so the callback opens the store with the
  same config the load used;
* **context auto-inject** -- likewise for ``context``, which receives the
  :class:`~soliplex.agents.manifest.context.LoadContext` for the source: the
  resolved download target, store, and sidecar facade. A callback that needs
  storage does not have to rediscover it from the environment;
* **outcome auto-inject** -- likewise for ``ingester``, the load's
  :class:`~soliplex.agents.manifest.haiku_process.HaikuRun` (exit code,
  ``timed_out``, and the last part of its stdout / stderr), and for
  ``ingester_exit_code``, just the exit code (``None`` on timeout). Callbacks
  fire whatever the load did, so a step that only makes sense after a clean
  load has to be able to ask;
* **run auto-inject** -- likewise for ``run_result``, the result of the
  manifest run that queued this load (``None`` when the load was started
  without one). Under the server, loads are queued, so a later run of the
  same manifest may already have started by the time this one's callbacks
  fire.

A step that raises stops the chain: the exception is logged, the steps after
it do not run, and :class:`PostProcessFailed` carries every step's outcome. See :func:`run_post_process`, and the README's
"Post-process callbacks" for the configuration side.
"""

import logging
import os
import time
from contextlib import contextmanager

from soliplex.agents import telemetry
from soliplex.agents.config import Manifest
from soliplex.agents.manifest import callables
from soliplex.agents.manifest.context import LoadContext
from soliplex.agents.manifest.haiku_loader import resolve_haiku_cfg
from soliplex.agents.manifest.haiku_process import HaikuRun

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_NOT_RUN = "not_run"


# Module-level names so a test can swap the import for a registry.
_resolve_method = callables.resolve_method
_accepts_kwarg = callables.accepts_kwarg


@contextmanager
def _load_env(manifest: Manifest, context: LoadContext):
    """Temporarily expose the env vars ``run_load`` injects into the load
    subprocess (``SOURCE`` / ``DOWNLOAD_DIR``).

    In-process callbacks load the same haiku config the load used, and that
    config interpolates ``${SOURCE}`` / ``${DOWNLOAD_DIR}`` (which
    ``load_yaml_config`` expands eagerly). Those two are only ever set in the
    subprocess env, so mirror them here for the duration of the callbacks. Other
    ``${VAR}`` references (``STATE_DIR``, embedder URLs, ...) are expected in the
    inherited environment, exactly as they are for the subprocess. Loads are
    serialized, so the temporary global mutation does not race.
    """
    overrides = {key: context.env({})[key] for key in ("SOURCE", "DOWNLOAD_DIR", "DOWNLOAD_URI")}
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class PostProcessFailed(Exception):
    """A post-process step raised; the chain stopped there.

    Raised from the step's exception, which is the ``__cause__``. *steps*
    holds one outcome per configured step: ``ok`` for those that ran,
    ``error`` for the one that raised, ``not_run`` for those after it.
    """

    def __init__(self, message: str, steps: list[dict]):
        super().__init__(message)
        self.steps = steps


def _outcome(method: str, status: str, error: str | None = None, started: float | None = None) -> dict:
    """One step's entry in :func:`run_post_process`'s result."""
    return {
        "method": method,
        "status": status,
        "ok": status == STATUS_OK,
        "error": error,
        "duration_s": None if started is None else round(time.monotonic() - started, 3),
    }


async def run_post_process(
    manifest: Manifest,
    *,
    ingester: HaikuRun | None = None,
    run_result: dict | None = None,
) -> list[dict]:
    """Run ``manifest.config.post_process`` callbacks in order.

    ``ingester`` is the haiku-ingester load's outcome. Callbacks run regardless
    of it, so a step can inspect it: it is auto-injected as ``ingester``, and
    its exit code (``None`` on timeout, or when there is no load) as
    ``ingester_exit_code``, for callables that accept them. ``run_result`` --
    the manifest run that queued the load -- is injected the same way.

    Runs the steps in order and **terminates on the first error**: a step that
    raises is logged, the remaining steps do not run, and
    :class:`PostProcessFailed` is raised carrying every step's outcome.

    Returns:
        One ``{"method", "status", "ok", "error", "duration_s"}`` per step,
        all ``ok``.

    Raises:
        PostProcessFailed: when a step raises (chained from its exception).
    """
    if manifest.config is None or not manifest.config.post_process:
        return []

    steps = manifest.config.post_process
    results: list[dict] = []
    context = LoadContext.for_source(manifest.source)
    ingester_exit_code = ingester.returncode if ingester is not None else None
    available = {
        "ingester": ingester,
        "ingester_exit_code": ingester_exit_code,
        "run_result": run_result,
        "context": context,
    }
    with _load_env(manifest, context):
        for index, step in enumerate(steps):
            attributes = {
                telemetry.POST_PROCESS_METHOD: step.method,
                telemetry.POST_PROCESS_INDEX: index,
                telemetry.MANIFEST_ID: manifest.id,
                telemetry.MANIFEST_SOURCE: manifest.source,
                telemetry.INGESTER_EXIT_CODE: ingester_exit_code,
            }
            started = time.monotonic()
            try:
                # A step that raises leaves the span with the exception, which
                # marks it as failed; no later step gets a span, since none runs.
                with telemetry.span("post-process", f"post-process {step.method}", attributes):
                    logger.info(
                        "Running post-process '%s' for source '%s'",
                        step.method,
                        manifest.source,
                    )
                    method = _resolve_method(step.method)
                    kwargs = dict(step.kwargs)
                    # Resolved only when wanted: it raises without HAIKU_PATH.
                    if "config" not in kwargs and _accepts_kwarg(method, "config"):
                        kwargs["config"] = resolve_haiku_cfg(manifest)
                    kwargs = callables.inject(method, kwargs, available)
                    # Inline, not in a thread: existing callbacks may expect
                    # to run on the event loop's thread.
                    await callables.invoke(method, manifest.source, kwargs=kwargs, in_thread=False)
            except Exception as e:
                message = callables.describe_error(e)
                logger.exception(
                    "Post-process '%s' failed for source '%s'; terminating",
                    step.method,
                    manifest.source,
                )
                results.append(_outcome(step.method, STATUS_ERROR, message, started))
                results.extend(_outcome(later.method, STATUS_NOT_RUN) for later in steps[index + 1 :])
                raise PostProcessFailed(f"post-process {step.method} failed: {message}", results) from e
            logger.info("Post-process '%s' completed", step.method)
            results.append(_outcome(step.method, STATUS_OK, started=started))
    return results
