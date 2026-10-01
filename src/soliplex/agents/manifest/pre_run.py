"""Run a manifest's ``config.pre_run`` steps before any component starts.

Pre-run steps fire once per manifest run, ahead of every component, for
notifications and pre-checks. Each is called as ``method(context, **kwargs)``
with a :class:`PreRunContext`, and answers with a :class:`PreRunStatus` --
optionally with a message, as ``(status, message)``:

* ``CONTINUE`` (or ``None``) -- carry on;
* ``SKIP`` -- call this run off. No component runs, nothing is reconciled or
  pre-processed, and no haiku load (so no post-process) is queued. The next
  scheduled run happens as normal.

A step that raises or outlives its ``timeout`` is handled by its
``on_error``: ``continue`` logs it and moves on, ``skip`` turns it into a
SKIP, and ``fail`` (the default) fails the manifest just as a crashing
component would.

Outcomes are kept in the source's state DB (``pre_run`` table, the last
:data:`~soliplex.agents.local_state.PRE_RUN_HISTORY` runs) so a skipped run
can be explained afterwards.
"""

import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from typing import NamedTuple

from soliplex.agents import local_state
from soliplex.agents import telemetry
from soliplex.agents.config import Manifest
from soliplex.agents.config import PreRunStep
from soliplex.agents.manifest import callables

logger = logging.getLogger(__name__)

# Audit status for a step that raised or timed out (not one a step returns).
STATUS_ERROR = "error"


class PreRunStatus(enum.StrEnum):
    """What a pre-run step decided about the run."""

    CONTINUE = "continue"
    SKIP = "skip"


class PreRunResult(NamedTuple):
    """A pre-run step's full answer; ``(status, message)`` is the same thing."""

    status: PreRunStatus
    message: str | None = None


@dataclass(frozen=True)
class PreRunContext:
    """What a pre-run step is told about the run it may call off.

    ``manifest`` is a deep copy, so a step cannot change what runs. ``load``
    is the source's :class:`~soliplex.agents.manifest.context.LoadContext`
    (resolved target, store, sidecars). ``started_at`` is the run's ISO 8601
    start time, shared with its result and audit rows.
    """

    manifest: Manifest
    load: Any
    started_at: str


@dataclass(frozen=True)
class ResolvedPreRunStep:
    """A configured step with its method already imported."""

    index: int
    step: PreRunStep
    method: Callable


class PreRunFailed(RuntimeError):
    """A pre-run step failed under ``on_error: fail``."""


def resolve_steps(manifest: Manifest) -> list[ResolvedPreRunStep]:
    """Import every pre-run step for *manifest*, so a bad path fails before anything runs.

    Raises:
        ImportError, AttributeError: when a step's method cannot be imported.
    """
    steps = manifest.config.pre_run if manifest.config else []
    return [ResolvedPreRunStep(index, step, callables.resolve_method(step.method)) for index, step in enumerate(steps)]


def _error_message(exc: BaseException, timeout: float | None) -> str:
    if isinstance(exc, TimeoutError):
        return f"timed out after {timeout}s"
    return callables.describe_error(exc)


async def run_pre_run(manifest: Manifest, steps: list[ResolvedPreRunStep], *, load: Any, started_at: str) -> dict:
    """Run *steps* in order and decide whether the manifest runs.

    Returns:
        ``{"steps": [...], "skipped": None | {"method", "message"}}``, where
        each step entry is ``{"method", "status", "message", "duration_s"}``.

    Raises:
        PreRunFailed: when a step fails under ``on_error: fail``. The outcomes
            so far are recorded first.
    """
    outcomes: list[dict] = []
    skipped: dict | None = None
    try:
        for resolved in steps:
            step = resolved.step
            attributes = {
                telemetry.PRE_RUN_METHOD: step.method,
                telemetry.PRE_RUN_INDEX: resolved.index,
                telemetry.MANIFEST_ID: manifest.id,
            }
            with telemetry.span("pre-run", f"pre-run {step.method}", attributes) as step_span:
                context = PreRunContext(manifest=manifest.model_copy(deep=True), load=load, started_at=started_at)
                started = time.monotonic()
                try:
                    value = await callables.invoke(resolved.method, context, kwargs=dict(step.kwargs), timeout=step.timeout)
                    result = callables.normalize(value, PreRunResult, PreRunStatus, PreRunStatus)
                    status: str = result.status
                    message = result.message
                except Exception as e:
                    message = _error_message(e, step.timeout)
                    outcomes.append(_outcome(resolved, STATUS_ERROR, message, started))
                    if step.on_error == "fail":
                        logger.exception("pre-run %s failed for manifest '%s': %s", step.method, manifest.id, message)
                        # Leaving the span with the exception marks it failed.
                        raise PreRunFailed(f"pre-run {step.method} failed: {message}") from e
                    logger.warning(
                        "pre-run %s failed for manifest '%s': %s", step.method, manifest.id, message, exc_info=True
                    )
                    if step.on_error == "skip":
                        skipped = {"method": step.method, "message": message}
                        break
                    continue
                outcomes.append(_outcome(resolved, status, message, started))
                step_span.set_attribute("pre_run.status", str(status))
                if status == PreRunStatus.SKIP:
                    skipped = {"method": step.method, "message": message}
                    break
                if message:
                    logger.info("pre-run %s for manifest '%s': %s", step.method, manifest.id, message)
    finally:
        rows = []
        for outcome in outcomes:
            index = outcome.pop("_index")
            rows.append({"started_at": started_at, "step": index, **outcome})
        local_state.record_pre_run(manifest.source, rows)
    if skipped is not None:
        suffix = f": {skipped['message']}" if skipped["message"] else ""
        logger.info("manifest '%s' skipped by %s%s", manifest.id, skipped["method"], suffix)
    return {"steps": outcomes, "skipped": skipped}


def _outcome(resolved: ResolvedPreRunStep, status: str, message: str | None, started: float) -> dict:
    return {
        "_index": resolved.index,
        "method": resolved.step.method,
        "status": str(status),
        "message": message,
        "duration_s": round(time.monotonic() - started, 3),
    }
