"""Tests for the traced, buffered haiku subprocess runner — 100% branch coverage."""

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

from soliplex.agents.manifest import haiku_process

_EXEC = "soliplex.agents.manifest.haiku_process.asyncio.create_subprocess_exec"


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


class TestRunHaiku:
    @pytest.mark.asyncio
    async def test_success_opens_a_span_naming_the_cli(self, spans, caplog):
        proc = _fake_proc(returncode=0, stdout=[b"step 1\n", b"done\n"], stderr=[b"note\n"])
        with caplog.at_level(logging.INFO, logger="soliplex.agents.manifest.haiku_process"):
            run, mock_exec = await _run(proc, attributes={"haiku.db": "/db"})

        assert run == haiku_process.HaikuRun(returncode=0, timed_out=False, stdout="step 1\ndone", stderr="note")
        assert mock_exec.call_args.args == ("haiku-ingester", "--config=/my cfg.yaml", "run-batch")
        (span,) = spans.get_finished_spans()
        assert span.name == "haiku load src"
        assert span.attributes["haiku.cli"] == "haiku-ingester '--config=/my cfg.yaml' run-batch"
        assert span.attributes["haiku.operation"] == "load"
        assert span.attributes["haiku.source"] == "src"
        assert span.attributes["haiku.db"] == "/db"
        assert span.attributes["haiku.returncode"] == 0
        assert span.attributes["haiku.timed_out"] is False
        assert span.attributes["haiku.stdout_bytes"] == len(b"step 1\ndone\n")
        assert span.attributes["haiku.stdout_tail"] == "step 1\ndone"
        assert span.attributes["haiku.stderr_tail"] == "note"
        assert span.status.status_code is StatusCode.UNSET
        # One start line and one outcome line: nothing per line of output.
        assert "Starting haiku load for source 'src': haiku-ingester" in caplog.text
        assert "haiku load for source 'src' completed" in caplog.text
        assert "step 1" not in caplog.text

    @pytest.mark.asyncio
    async def test_failure_quotes_stderr_and_marks_span_error(self, spans, caplog):
        proc = _fake_proc(returncode=3, stdout=[], stderr=[b"Traceback\n", b"KeyError: x\n"])
        with caplog.at_level(logging.ERROR, logger="soliplex.agents.manifest.haiku_process"):
            run, _ = await _run(proc, operation="vacuum")

        assert run.returncode == 3
        assert "haiku vacuum for source 'src' failed (rc=3); last stderr:\nTraceback\nKeyError: x" in caplog.text
        (span,) = spans.get_finished_spans()
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description == "exited with code 3"

    @pytest.mark.asyncio
    async def test_failure_without_stderr_says_so(self, spans, caplog):
        with caplog.at_level(logging.ERROR, logger="soliplex.agents.manifest.haiku_process"):
            await _run(_fake_proc(returncode=1, stdout=[]))
        assert "last stderr:\n(no stderr)" in caplog.text

    @pytest.mark.asyncio
    async def test_signal_kill_reports_oom_hint(self, spans, caplog):
        with caplog.at_level(logging.ERROR, logger="soliplex.agents.manifest.haiku_process"):
            run, _ = await _run(_fake_proc(returncode=-9))
        assert run.returncode == -9
        # SIGKILL off POSIX is just "signal 9", so assert what holds on both.
        signame = "SIGKILL" if hasattr(signal, "SIGKILL") else "signal 9"
        assert f"was killed by {signame} (rc=-9)" in caplog.text
        assert "memory limit" in caplog.text
        (span,) = spans.get_finished_spans()
        assert span.status.description == f"killed by {signame}"

    @pytest.mark.asyncio
    async def test_timeout_keeps_partial_output(self, spans, caplog):
        proc = _fake_proc(stdout=[b"halfway\n"], stderr=[b"slow step\n"], hang=True)
        with caplog.at_level(logging.ERROR, logger="soliplex.agents.manifest.haiku_process"):
            run, _ = await _run(proc, timeout=0.05)

        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()
        assert run == haiku_process.HaikuRun(returncode=None, timed_out=True, stdout="halfway", stderr="slow step")
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
    async def test_real_subprocess_with_an_unterminated_line_past_64k(self, spans):
        # A progress bar redrawn with '\r' never ends a line; line-based reads
        # raise past asyncio's 64 KiB limit. Chunked reads must not.
        script = "import sys; sys.stderr.write('\\r'.join(['x' * 100] * 2000)); sys.exit(4)"
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
