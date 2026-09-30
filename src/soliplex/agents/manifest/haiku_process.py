"""Run one haiku CLI subprocess inside a span, logging its output in parts.

Shared by the batch load (:mod:`.haiku_loader`) and the maintenance verbs
(:mod:`.haiku_maint`), so every haiku run is traced and reported the same way.

Each run opens its own span naming the operation, the source and the exact
command line; log records emitted while it runs nest under it, and the span
ends with the exit status and the size of each stream. The span goes through
the OpenTelemetry API rather than ``logfire.span``: once the server has
configured Logfire it is the global tracer provider, so the span reaches
Logfire, while CLI runs -- which never configure it -- get a no-op span
instead of a ``LogfireNotConfiguredWarning``.

The span's context is exported to the subprocess as ``TRACEPARENT`` /
``TRACESTATE``, so a child that reads it joins this trace. With
``HAIKU_TRACE_WRAPPER`` on, a console-script command runs through
:mod:`soliplex.agents.traced_run`, which reads it on the child's behalf.

Output is forwarded to the log in *parts*: one record per
``settings.haiku_output_chunk_bytes`` (split at a line break where there is
one), and at least every ``settings.haiku_output_flush_seconds`` while output
is pending. Nothing is truncated, memory stays bounded, and a long run shows
progress as it goes, without the one-record-per-line flood that streaming
used to produce. Output is read in chunks rather than by line, so a progress
bar redrawn with ``\\r`` -- which never ends a line -- cannot overrun
asyncio's 64 KiB line limit. That overrun used to raise out of the run and
leave the child running.

The output goes in the message text, not in span attributes: Logfire's
scrubber replaces an attribute value that contains a word such as ``auth``
or ``session`` wholesale, which error output routinely does, but it leaves a
log record's formatted message intact.
"""

import asyncio
import codecs
import logging
import os
import shlex
import signal
import sys
import time
from dataclasses import dataclass

from opentelemetry import propagate
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode

from soliplex.agents import telemetry
from soliplex.agents import traced_run
from soliplex.agents.config import settings

logger = logging.getLogger(__name__)

# Bytes of each stream kept in memory for the result and the failure quote;
# the full output is in the logged parts.
OUTPUT_LIMIT = 1024 * 1024
# Lines of stderr quoted in a failure's log message.
TAIL_LINES = 40
_READ_SIZE = 64 * 1024


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


def _split_point(data: bytearray, limit: int) -> int:
    """Where to end a part: after the last line break within *limit* bytes.

    ``\\r`` counts as a line break, so a redrawn progress bar splits cleanly;
    output with no break at all is cut at *limit*.
    """
    cut = max(data.rfind(b"\n", 0, limit), data.rfind(b"\r", 0, limit)) + 1
    return cut if cut > 0 else limit


class _StreamLog:
    """Forward one stream of a haiku run to the log, one part at a time.

    Parts are flushed when they reach ``chunk_bytes`` or when output has been
    pending for ``flush_seconds``. ``max_bytes`` (0 = unlimited) caps what is
    logged per stream -- a guard against a runaway child -- after which
    output is still counted and kept in the tail, but no longer logged.
    """

    def __init__(
        self,
        operation: str,
        source: str,
        stream: str,
        *,
        chunk_bytes: int,
        flush_seconds: float,
        max_bytes: int,
    ) -> None:
        self._label = (operation, source, stream)
        self._chunk_bytes = chunk_bytes
        self._flush_seconds = flush_seconds
        self._max_bytes = max_bytes
        # Decodes across part boundaries, so a multi-byte character cut by a
        # hard split is completed in the next part instead of mangled.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = bytearray()
        self._last_flush = time.monotonic()
        self.tail = _TailBuffer()
        self.parts = 0
        self.logged_bytes = 0
        self.dropped_bytes = 0

    @property
    def total(self) -> int:
        return self.tail.total

    def add(self, chunk: bytes) -> None:
        self.tail.add(chunk)
        self._pending += chunk
        while len(self._pending) >= self._chunk_bytes:
            cut = _split_point(self._pending, self._chunk_bytes)
            self._emit(bytes(self._pending[:cut]))
            del self._pending[:cut]
        if self._pending and time.monotonic() - self._last_flush >= self._flush_seconds:
            self.flush()

    def flush(self, final: bool = False) -> None:
        """Log whatever is pending; *final* also completes a cut character."""
        if self._pending or final:
            data = bytes(self._pending)
            self._pending.clear()
            self._emit(data, final=final)
        self._last_flush = time.monotonic()

    def _emit(self, data: bytes, final: bool = False) -> None:
        # Once over the cap, stay over it: a later part that happens to fit
        # would leave a gap in the logged output with no marker.
        if self.dropped_bytes or (self._max_bytes and self.logged_bytes + len(data) > self._max_bytes):
            if not self.dropped_bytes:
                logger.warning(
                    "haiku %s %s %s output exceeded %d bytes; the rest is counted but not logged",
                    *self._label,
                    self._max_bytes,
                )
            self.dropped_bytes += len(data)
            return
        text = self._decoder.decode(data, final=final)
        text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        if not text:
            return
        self.parts += 1
        self.logged_bytes += len(data)
        # "output" marks these as the agent's copy of the subprocess output, not
        # records haiku-ingester sent itself -- both can land in one trace.
        logger.info("haiku %s %s %s output part %d:\n%s", *self._label, self.parts, text)


async def _drain(reader, stream: _StreamLog, flush_seconds: float) -> None:
    """Read *reader* to EOF into *stream*, flushing it whenever reads go quiet."""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(_READ_SIZE), timeout=flush_seconds)
        except TimeoutError:
            stream.flush()
            continue
        if not chunk:
            return
        stream.add(chunk)


def tail(text: str, lines: int = TAIL_LINES) -> str:
    """The last *lines* lines of *text*."""
    return "\n".join(text.splitlines()[-lines:])


def _with_trace_context(env: dict[str, str] | None) -> dict[str, str] | None:
    """*env* plus ``TRACEPARENT`` / ``TRACESTATE`` naming the current span.

    Always exported, whatever the child: a child that doesn't read them is
    unaffected, and one that does -- haiku-rag once it reads ``TRACEPARENT``,
    or any command run through :mod:`soliplex.agents.traced_run` -- joins
    this trace. With no active span (tracing off) nothing is added and *env*
    is returned as given; ``None`` still means "inherit ours".
    """
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    if "traceparent" not in carrier:
        return env
    env = dict(os.environ if env is None else env)
    env["TRACEPARENT"] = carrier["traceparent"]
    if "tracestate" in carrier:
        env["TRACESTATE"] = carrier["tracestate"]
    else:
        # Don't pass on a tracestate that belongs to some other trace.
        env.pop("TRACESTATE", None)
    return env


def _with_trace_wrapper(argv: list[str]) -> tuple[list[str], bool]:
    """Route *argv* through :mod:`soliplex.agents.traced_run` when asked to.

    Only with ``HAIKU_TRACE_WRAPPER`` on, and only for a console script
    installed in this environment -- the wrapper runs its entry point
    in-process. Anything else, including a custom command template that
    isn't Python, runs unchanged.

    Returns:
        The argv to execute, and whether it was wrapped.
    """
    if not settings.haiku_trace_wrapper:
        return argv, False
    if traced_run.find_entry_point(traced_run.script_name(argv[0])) is None:
        logger.debug(
            "HAIKU_TRACE_WRAPPER is on, but %s is not a console script in this environment; running it unwrapped",
            argv[0],
        )
        return argv, False
    return [sys.executable, "-m", "soliplex.agents.traced_run", *argv], True


@dataclass(frozen=True)
class HaikuRun:
    """Outcome of one haiku subprocess.

    ``returncode`` is ``None`` when the run timed out; ``stdout`` / ``stderr``
    hold the last :data:`OUTPUT_LIMIT` bytes of each stream either way. The
    full output is in the log, in parts.
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

    Output is logged in parts as it arrives (see the module docstring).
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
    exec_argv, wrapped = _with_trace_wrapper(argv)
    span_attributes = {
        "haiku.operation": operation,
        "haiku.source": source,
        "haiku.cli": cli,
        "haiku.timeout": timeout,
        "haiku.trace_wrapper": wrapped,
        **(attributes or {}),
    }
    span_attributes = {key: value for key, value in span_attributes.items() if value is not None}
    span_attributes["logfire.msg"] = f"haiku {operation} {source}"
    # Looked up on the module at call time, so tests can route it elsewhere.
    with telemetry.tracer.start_as_current_span(f"haiku {operation}", attributes=span_attributes) as span:
        logger.info("Starting haiku %s for source '%s': %s", operation, source, cli)
        proc = await asyncio.create_subprocess_exec(
            *exec_argv,
            cwd=cwd,
            env=_with_trace_context(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        flush_seconds = settings.haiku_output_flush_seconds
        out, err = (
            _StreamLog(
                operation,
                source,
                name,
                chunk_bytes=settings.haiku_output_chunk_bytes,
                flush_seconds=flush_seconds,
                max_bytes=settings.haiku_output_max_bytes,
            )
            for name in ("stdout", "stderr")
        )
        timed_out = False
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(_drain(proc.stdout, out, flush_seconds), _drain(proc.stderr, err, flush_seconds))
                await proc.wait()
        except TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
        # Also after a timeout: what was read before the cancellation is logged
        # rather than lost.
        out.flush(final=True)
        err.flush(final=True)

        run = HaikuRun(
            returncode=None if timed_out else proc.returncode,
            timed_out=timed_out,
            stdout=out.tail.text(),
            stderr=err.tail.text(),
        )
        span.set_attributes(
            {
                "haiku.timed_out": timed_out,
                "haiku.stdout_bytes": out.total,
                "haiku.stderr_bytes": err.total,
                "haiku.stdout_parts": out.parts,
                "haiku.stderr_parts": err.parts,
                "haiku.output_dropped_bytes": out.dropped_bytes + err.dropped_bytes,
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
        span.set_attribute("haiku.signal", signame)
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
