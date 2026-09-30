"""OpenTelemetry spans for manifest runs.

Spans go through the OpenTelemetry API, not ``logfire.span``. Once the server
has configured Logfire it is the global tracer provider, so these spans reach
Logfire, and every stdlib log record emitted inside one -- including the
per-file errors the agents already log -- nests under it. CLI runs never
configure Logfire and get no-op spans instead of a
``LogfireNotConfiguredWarning``.

Span *names* are low-cardinality (``manifest run``, ``component``) so they
stay queryable; the ``logfire.msg`` attribute gives each one a readable
message in the Logfire UI (``manifest docs-site``).

The trace a server run produces::

    manifest run  [manifest docs-site]         manifest_queue._worker
    |- component  [component wiki (webdav)]    runner._run_components
    |   `- (log) Failed to write /a.pdf ...     emitted by the agent
    |- delete stale
    `- (log) Manifest 'docs-site' finished: ...
    post-process  [post-process pkg:fn]        haiku_queue._worker, later,
                                               under the manifest's context
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode

tracer = trace.get_tracer("soliplex.agents")

# Attribute names, defined once so queries and tests don't drift.
MANIFEST_ID = "manifest.id"
MANIFEST_NAME = "manifest.name"
MANIFEST_SOURCE = "manifest.source"
MANIFEST_PATH = "manifest.path"
MANIFEST_COMPONENTS = "manifest.components"
QUEUE_WAIT = "manifest.queue_wait_s"
COMPONENT_NAME = "component.name"
COMPONENT_TYPE = "component.type"
POST_PROCESS_METHOD = "post_process.method"
POST_PROCESS_INDEX = "post_process.index"
INGESTER_EXIT_CODE = "haiku.returncode"


@contextmanager
def span(name: str, message: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
    """Open a span named *name*, shown in Logfire as *message*.

    ``None`` attribute values are dropped (OpenTelemetry rejects them). An
    exception leaving the span is recorded on it and sets status ERROR; one
    that is caught inside must be reported with :func:`fail`.
    """
    attrs = {key: value for key, value in (attributes or {}).items() if value is not None}
    attrs["logfire.msg"] = message
    with tracer.start_as_current_span(name, attributes=attrs) as current:
        yield current


def fail(target: Span, description: str, exc: BaseException | None = None) -> None:
    """Mark *target* as failed, recording *exc* when there is one."""
    if exc is not None:
        target.record_exception(exc)
    target.set_status(Status(StatusCode.ERROR, description))


@contextmanager
def manifest_span(manifest_id: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
    """The span for one manifest run, from the server's queue or the CLI."""
    with span("manifest run", f"manifest {manifest_id}", {MANIFEST_ID: manifest_id, **(attributes or {})}) as current:
        yield current


def describe_manifest(target: Span, manifest) -> None:
    """Add what is only known once the manifest file has loaded."""
    target.set_attributes(
        {
            MANIFEST_NAME: manifest.name,
            MANIFEST_SOURCE: manifest.source,
            MANIFEST_COMPONENTS: len(manifest.components),
        }
    )


def record_summary(target: Span, summary: dict[str, Any]) -> None:
    """Copy a run's outcome counts onto *target*, failing it if anything failed.

    *summary* is the ``summary`` a manifest run returns; a run fails when any
    component raised or any file failed.
    """
    target.set_attributes({f"manifest.{key}": value for key, value in summary.items()})
    if summary.get("component_errors") or summary.get("file_errors"):
        fail(
            target,
            f"{summary.get('component_errors', 0)} component errors, {summary.get('file_errors', 0)} file errors",
        )
