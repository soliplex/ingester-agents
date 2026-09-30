"""Tests for the traced haiku subprocess runner — 100% branch coverage."""

import asyncio
import logging
import signal
import sys
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from soliplex.agents.config import settings
from soliplex.agents.manifest import haiku_process

_EXEC = "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec"
_LOGGER = "soliplex.agents.manifest.haiku_process"


class _FakeStream:
    """Stand-in for asyncio.StreamReader: one chunk per read, then EOF or a hang."""

    def __init__(self, chunks, hang=False):
        self._chunks = list(chunks)
        self._hang = hang

    async def read(self, n=-1):
        if self._chunks:
            return self._chunks.pop(0)
        if self._hang:
            await asyncio.Event().wait()
        return b""


def _fake_proc(returncode=0, stdout=(b"ok\n",), stderr=(), hang=False):
    proc = MagicMock()
    proc.stdout = _FakeStream(stdout, hang=hang)
    proc.stderr = _FakeStream(stderr)
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


@pytest.fixture
def spans(monkeypatch):
    """Route the module's spans to an in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(haiku_process, "_tracer", provider.get_tracer("test"))
    return exporter


def _stream(chunk_bytes=16, flush_seconds=1000.0, max_bytes=0):
    return haiku_process._StreamLog(
        "load", "src", "stderr", chunk_bytes=chunk_bytes, flush_seconds=flush_seconds, max_bytes=max_bytes
    )


def _parts(caplog):
    """The logged parts, as (header, body) pairs."""
    return [
        tuple(r.getMessage().split("\n", 1))
        for r in caplog.records
        if r.name == _LOGGER and " part " in r.getMessage().split("\n", 1)[0]
    ]


async def _run(proc, **kwargs):
    args = {"operation": "load", "source": "src", "env": {}, "cwd": None, "timeout": 30} | kwargs
    with patch(_EXEC, new_callable=AsyncMock, return_value=proc) as mock_exec:
        run = await haiku_process.run_haiku(["haiku-ingester", "--config=/my cfg.yaml", "run-batch"], **args)
    return run, mock_exec


class TestTailBuffer:
    def test_keeps_everything_under_the_limit(self):
        buffer = haiku_process._TailBuffer(limit=100)
        buffer.add(b"a  \r\nb\n")
        assert buffer.text() == "a\nb"
        assert buffer.total == 7

    def test_keeps_only_the_tail_and_drops_the_partial_line(self):
        buffer = haiku_process._TailBuffer(limit=8)
        buffer.add(b"first line\nsecond\n")
        # 18 bytes seen, the last 8 kept ("\nsecond\n"); its leading fragment is dropped.
        assert buffer.total == 18
        assert buffer.text() == "second"

    def test_undecodable_bytes_are_replaced(self):
        buffer = haiku_process._TailBuffer()
        buffer.add(b"\xff ok\n")
        assert buffer.text() == "� ok"


def test_tail_takes_last_lines():
    assert haiku_process.tail("a\nb\nc", lines=2) == "b\nc"


class TestStreamLog:
    """Output is logged in parts: nothing lost, nothing logged line by line."""

    def test_small_output_is_one_part_at_flush(self, caplog):
        stream = _stream()
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            stream.add(b"one\ntwo\n")
            assert _parts(caplog) == []
            stream.flush(final=True)
        assert _parts(caplog) == [("haiku load src stderr part 1:", "one\ntwo")]
        assert stream.parts == 1
        assert stream.logged_bytes == 8

    def test_full_chunks_split_at_the_last_line_break(self, caplog):
        stream = _stream(chunk_bytes=16)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            stream.add(b"aaaa\nbbbb\ncccc\ndddd\neeee\n")
            stream.flush(final=True)
        # Each part ends on a line: no line is split between two records.
        assert [body for _, body in _parts(caplog)] == ["aaaa\nbbbb\ncccc", "dddd\neeee"]

    def test_carriage_return_counts_as_a_line_break(self, caplog):
        stream = _stream(chunk_bytes=12)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            stream.add(b"10%\r20%\r30%\r40%\r")
            stream.flush(final=True)
        assert [body for _, body in _parts(caplog)] == ["10%\n20%\n30%", "40%"]

    def test_output_with_no_line_break_is_cut_at_the_limit(self, caplog):
        stream = _stream(chunk_bytes=4)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            stream.add(b"abcdefghij")
            stream.flush(final=True)
        assert [body for _, body in _parts(caplog)] == ["abcd", "efgh", "ij"]

    def test_a_character_cut_by_the_limit_is_completed_in_the_next_part(self, caplog):
        # "é" is two bytes; a 4-byte limit cuts it after its first byte.
        stream = _stream(chunk_bytes=4)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            stream.add("abcé".encode())
            stream.flush(final=True)
        assert [body for _, body in _parts(caplog)] == ["abc", "é"]

    def test_pending_output_is_flushed_after_the_interval(self, caplog):
        stream = _stream(chunk_bytes=1024, flush_seconds=30)
        clock = [1000.0]
        with (
            patch("soliplex.agents.manifest.haiku_process.time.monotonic", side_effect=lambda: clock[0]),
            caplog.at_level(logging.INFO, logger=_LOGGER),
        ):
            stream._last_flush = clock[0]
            stream.add(b"early\n")
            assert _parts(caplog) == []
            clock[0] += 31
            stream.add(b"later\n")
        assert [body for _, body in _parts(caplog)] == ["early\nlater"]

    def test_flush_with_nothing_pending_logs_nothing(self, caplog):
        # What a quiet reader triggers before the child has written anything.
        stream = _stream()
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            stream.flush()
        assert _parts(caplog) == []

    def test_blank_output_is_not_a_part(self, caplog):
        stream = _stream()
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            stream.add(b"\n\r\n")
            stream.flush(final=True)
            stream.flush(final=True)
        assert _parts(caplog) == []
        assert stream.parts == 0

    def test_cap_stops_logging_once_and_counts_the_rest(self, caplog):
        stream = _stream(chunk_bytes=4, max_bytes=8)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            stream.add(b"aaa\nbbb\nccc\nd")
            # A later part that would fit under the cap is still not logged.
            stream.add(b"\n")
            stream.flush(final=True)
        assert [body for _, body in _parts(caplog)] == ["aaa", "bbb"]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert warnings[0].getMessage() == (
            "haiku load src stderr exceeded 8 bytes of output; the rest is counted but not logged"
        )
        assert stream.dropped_bytes == 6
        assert stream.total == 14
        assert stream.tail.text() == "aaa\nbbb\nccc\nd"


class TestDrain:
    @pytest.mark.asyncio
    async def test_quiet_reader_flushes_pending_output(self, caplog):
        class _Quiet:
            """Delivers a chunk, goes quiet past the flush interval, then EOF."""

            def __init__(self):
                self._reads = 0

            async def read(self, n=-1):
                self._reads += 1
                if self._reads == 1:
                    return b"progress\n"
                if self._reads == 2:
                    await asyncio.sleep(1)  # outlasts the 0.01s interval
                return b""

        stream = _stream(chunk_bytes=1024, flush_seconds=0.01)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await haiku_process._drain(_Quiet(), stream, 0.01)
        # Logged while the reader was quiet, before EOF.
        assert [body for _, body in _parts(caplog)] == ["progress"]


class TestRunHaiku:
    @pytest.mark.asyncio
    async def test_success_opens_a_span_and_logs_output_in_parts(self, spans, caplog):
        proc = _fake_proc(returncode=0, stdout=[b"step 1\n", b"done\n"], stderr=[b"note\n"])
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            run, mock_exec = await _run(proc, attributes={"haiku.db": "/db"})

        assert run == haiku_process.HaikuRun(returncode=0, timed_out=False, stdout="step 1\ndone", stderr="note")
        assert mock_exec.call_args.args == ("haiku-ingester", "--config=/my cfg.yaml", "run-batch")
        assert sorted(_parts(caplog)) == [
            ("haiku load src stderr part 1:", "note"),
            ("haiku load src stdout part 1:", "step 1\ndone"),
        ]
        (span,) = spans.get_finished_spans()
        assert span.name == "haiku load src"
        assert span.attributes["haiku.cli"] == "haiku-ingester '--config=/my cfg.yaml' run-batch"
        assert span.attributes["haiku.operation"] == "load"
        assert span.attributes["haiku.source"] == "src"
        assert span.attributes["haiku.db"] == "/db"
        assert span.attributes["haiku.returncode"] == 0
        assert span.attributes["haiku.timed_out"] is False
        assert span.attributes["haiku.stdout_bytes"] == len(b"step 1\ndone\n")
        assert span.attributes["haiku.stdout_parts"] == 1
        assert span.attributes["haiku.stderr_parts"] == 1
        assert span.attributes["haiku.output_dropped_bytes"] == 0
        # Output is in log messages, not attributes: Logfire's scrubber blanks
        # an attribute value containing "auth", "session", ...
        assert "haiku.stderr_tail" not in span.attributes
        assert span.status.status_code is StatusCode.UNSET
        assert "Starting haiku load for source 'src': haiku-ingester" in caplog.text
        assert "haiku load for source 'src' completed" in caplog.text

    @pytest.mark.asyncio
    async def test_settings_shape_the_parts(self, spans, caplog, monkeypatch):
        monkeypatch.setattr(settings, "haiku_output_chunk_bytes", 4, raising=False)
        monkeypatch.setattr(settings, "haiku_output_max_bytes", 8, raising=False)
        proc = _fake_proc(stdout=[b"aaa\nbbb\nccc\n"])
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            await _run(proc)
        assert [body for _, body in _parts(caplog)] == ["aaa", "bbb"]
        (span,) = spans.get_finished_spans()
        assert span.attributes["haiku.stdout_parts"] == 2
        assert span.attributes["haiku.output_dropped_bytes"] == 4

    @pytest.mark.asyncio
    async def test_failure_quotes_stderr_and_marks_span_error(self, spans, caplog):
        proc = _fake_proc(returncode=3, stdout=[], stderr=[b"Traceback\n", b"KeyError: x\n"])
        with caplog.at_level(logging.ERROR, logger=_LOGGER):
            run, _ = await _run(proc, operation="vacuum")

        assert run.returncode == 3
        assert "haiku vacuum for source 'src' failed (rc=3); last stderr:\nTraceback\nKeyError: x" in caplog.text
        (span,) = spans.get_finished_spans()
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description == "exited with code 3"

    @pytest.mark.asyncio
    async def test_failure_without_stderr_says_so(self, spans, caplog):
        with caplog.at_level(logging.ERROR, logger=_LOGGER):
            await _run(_fake_proc(returncode=1, stdout=[]))
        assert "last stderr:\n(no stderr)" in caplog.text

    @pytest.mark.asyncio
    async def test_signal_kill_reports_oom_hint(self, spans, caplog):
        with caplog.at_level(logging.ERROR, logger=_LOGGER):
            run, _ = await _run(_fake_proc(returncode=-9))
        assert run.returncode == -9
        # SIGKILL off POSIX is just "signal 9", so assert what holds on both.
        signame = "SIGKILL" if hasattr(signal, "SIGKILL") else "signal 9"
        assert f"was killed by {signame} (rc=-9)" in caplog.text
        assert "memory limit" in caplog.text
        (span,) = spans.get_finished_spans()
        assert span.status.description == f"killed by {signame}"
        assert span.attributes["haiku.signal"] == signame

    @pytest.mark.asyncio
    async def test_timeout_logs_and_keeps_partial_output(self, spans, caplog):
        proc = _fake_proc(stdout=[b"halfway\n"], stderr=[b"slow step\n"], hang=True)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            run, _ = await _run(proc, timeout=0.05)

        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()
        assert run == haiku_process.HaikuRun(returncode=None, timed_out=True, stdout="halfway", stderr="slow step")
        # Read before the cancellation, so logged rather than lost.
        assert ("haiku load src stdout part 1:", "halfway") in _parts(caplog)
        assert "timed out after 0.05s; last stderr:\nslow step" in caplog.text
        (span,) = spans.get_finished_spans()
        assert span.attributes["haiku.timed_out"] is True
        assert "haiku.returncode" not in span.attributes
        assert span.status.status_code is StatusCode.ERROR

    @pytest.mark.asyncio
    async def test_spawn_failure_raises_and_is_recorded(self, spans):
        with pytest.raises(FileNotFoundError):
            await haiku_process.run_haiku(
                ["definitely-not-a-haiku-binary-7f3a"],
                operation="load",
                source="src",
                env={},
                cwd=None,
                timeout=30,
            )
        (span,) = spans.get_finished_spans()
        assert span.status.status_code is StatusCode.ERROR
        assert span.events[0].name == "exception"

    @pytest.mark.asyncio
    async def test_real_subprocess_with_an_unterminated_line_past_64k(self, spans, caplog):
        # A progress bar redrawn with '\r' never ends a line; line-based reads
        # raise past asyncio's 64 KiB limit. Chunked reads must not.
        script = "import sys; sys.stderr.write('\\r'.join(['x' * 100] * 2000)); sys.exit(4)"
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            run = await haiku_process.run_haiku(
                [sys.executable, "-c", script],
                operation="load",
                source="src",
                env=None,
                cwd=None,
                timeout=60,
            )
        assert run.returncode == 4
        assert run.timed_out is False
        assert len(run.stderr) > 64 * 1024
        # ~200 KB of output in 64 KiB parts, with every redraw logged somewhere.
        bodies = [body for _, body in _parts(caplog)]
        assert len(bodies) >= 3
        assert sum(body.count("x" * 100) for body in bodies) == 2000
