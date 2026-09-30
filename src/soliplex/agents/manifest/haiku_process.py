"""Run one haiku CLI subprocess inside a span, buffering its output.

Shared by the batch load (:mod:`.haiku_loader`) and the maintenance verbs
(:mod:`.haiku_maint`), so every haiku run is traced and reported the same way.

Each run opens its own span naming the operation, the source and the exact
command line; log records emitted while it runs nest under it, and the span
ends with the exit status and the tail of each stream. The span goes through
the OpenTelemetry API rather than ``logfire.span``: once the server has
configured Logfire it is the global tracer provider, so the span reaches
Logfire, while CLI runs -- which never configure it -- get a no-op span
instead of a ``LogfireNotConfiguredWarning``.

Output is buffered rather than logged line by line. Streaming turned every
line of an hour-long run into its own log record, logged the child's own
errors at INFO, and discarded everything captured so far on a timeout. It
also read by line, and a progress bar redrawn with ``\\r`` never ends one:
past asyncio's 64 KiB line limit that raised out of the run and left the
child running. Reading in chunks has no such limit, and each stream keeps a
bounded tail, so the failure that matters is quoted in the error log and on
the span instead of being spread across thousands of INFO records.
"""

import asyncio
import logging
import shlex
import signal
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode

logger = logging.getLogger(__name__)

_tracer = trace.get_tracer("soliplex.agents.haiku")

# Bytes kept per stream: the most recent output, which is where a failure is.
OUTPUT_LIMIT = 1024 * 1024
# Lines of each stream quoted on the span, and of stderr in a failure's log.
TAIL_LINES = 40
_CHUNK = 64 * 1024


class _TailBuffer:
    """The last *limit* bytes of a stream, plus a count of everything seen."""

    def __init__(self, limit: int = OUTPUT_LIMIT) -> None:
        self._data = bytearray()
        self._limit = limit
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self._data += chunk
        excess = len(self._data) - self._limit
        if excess > 0:
            del self._data[:excess]

    def text(self) -> str:
        """The kept output, one right-stripped line per line.

        When output was dropped, the first kept line is a fragment of a longer
        one and is discarded too.
        """
        lines = [line.rstrip() for line in self._data.decode("utf-8", errors="replace").splitlines()]
        if self.total > len(self._data):
            lines = lines[1:]
        return "\n".join(lines)


async def _drain(reader, buffer: _TailBuffer) -> None:
    while chunk := await reader.read(_CHUNK):
        buffer.add(chunk)


def tail(text: str, lines: int = TAIL_LINES) -> str:
    """The last *lines* lines of *text*."""
    return "\n".join(text.splitlines()[-lines:])


@dataclass(frozen=True)
class HaikuRun:
    """Outcome of one haiku subprocess.

    ``returncode`` is ``None`` when the run timed out; ``stdout`` / ``stderr``
    hold whatever was captured either way, bounded by :data:`OUTPUT_LIMIT`.
    """

    returncode: int | None
    timed_out: bool
    stdout: str
    stderr: str


async def run_haiku(
    argv: list[str],
    *,
    operation: str,
    source: str,
    env: dict[str, str],
    cwd: str | None,
    timeout: float,
    attributes: dict[str, str] | None = None,
) -> HaikuRun:
    """Run *argv* to completion or *timeout*, in a span of its own.

    Failures and timeouts are logged at ERROR, quoting the tail of stderr, and
    mark the span as an error; they are reported in the result, not raised.
    Failing to start the process at all does raise, and is recorded on the
    span as it leaves.

    Args:
        argv: The command to run.
        operation: What the run is -- ``"load"`` or a maintenance verb.
        source: Manifest source, for the span name and log messages.
        env: Subprocess environment.
        cwd: Subprocess working directory (``None`` inherits ours).
        timeout: Seconds before the subprocess is killed.
        attributes: Extra span attributes (database, config, ...).

    Returns:
        A :class:`HaikuRun`.
    """
    cli = shlex.join(argv)
    span_attributes = {
        "haiku.operation": operation,
        "haiku.source": source,
        "haiku.cli": cli,
        "haiku.timeout": timeout,
        **(attributes or {}),
    }
    with _tracer.start_as_current_span(f"haiku {operation} {source}", attributes=span_attributes) as span:
        logger.info("Starting haiku %s for source '%s': %s", operation, source, cli)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = _TailBuffer(), _TailBuffer()
        timed_out = False
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(_drain(proc.stdout, out), _drain(proc.stderr, err))
                await proc.wait()
        except TimeoutError:
            # The buffers keep what was read before the cancellation.
            timed_out = True
            proc.kill()
            await proc.wait()

        run = HaikuRun(
            returncode=None if timed_out else proc.returncode,
            timed_out=timed_out,
            stdout=out.text(),
            stderr=err.text(),
        )
        span.set_attributes(
            {
                "haiku.timed_out": timed_out,
                "haiku.stdout_bytes": out.total,
                "haiku.stderr_bytes": err.total,
                "haiku.stdout_tail": tail(run.stdout),
                "haiku.stderr_tail": tail(run.stderr),
            }
        )
        _report(span, run, operation, source, timeout)
        return run


def _report(span, run: HaikuRun, operation: str, source: str, timeout: float) -> None:
    """Log the outcome of *run* and set the span's status to match."""
    stderr_tail = tail(run.stderr) or "(no stderr)"
    if run.timed_out:
        logger.error(
            "haiku %s for source '%s' timed out after %ss; last stderr:\n%s",
            operation,
            source,
            timeout,
            stderr_tail,
        )
        span.set_status(Status(StatusCode.ERROR, f"timed out after {timeout}s"))
        return

    span.set_attribute("haiku.returncode", run.returncode)
    if run.returncode == 0:
        logger.info("haiku %s for source '%s' completed", operation, source)
    elif run.returncode < 0:
        try:
            signame = signal.Signals(-run.returncode).name
        except ValueError:  # pragma: no cover - signal set is platform-specific
            signame = f"signal {-run.returncode}"
        logger.error(
            "haiku %s for source '%s' was killed by %s (rc=%s); a SIGKILL "
            "usually means the container exceeded its memory limit -- raise the "
            "memory limit or lower the haiku worker_count; last stderr:\n%s",
            operation,
            source,
            signame,
            run.returncode,
            stderr_tail,
        )
        span.set_status(Status(StatusCode.ERROR, f"killed by {signame}"))
    else:
        logger.error(
            "haiku %s for source '%s' failed (rc=%s); last stderr:\n%s",
            operation,
            source,
            run.returncode,
            stderr_tail,
        )
        span.set_status(Status(StatusCode.ERROR, f"exited with code {run.returncode}"))
