"""Tests for the traced console-script wrapper — 100% branch coverage required."""

import contextvars
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from opentelemetry import trace

from soliplex.agents import traced_run

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{SPAN_ID}-01"


def _isolated(fn, *args, **kwargs):
    """Run *fn* in a copy of the current context.

    ``attach_parent`` attaches for the life of the process; outside a copy it
    would leak into every later test on this thread.
    """
    return contextvars.copy_context().run(fn, *args, **kwargs)


def _current_ids():
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"


@pytest.mark.parametrize(
    "command, name",
    [
        ("haiku-ingester", "haiku-ingester"),
        ("/app/.venv/bin/haiku-ingester", "haiku-ingester"),
        (r"C:\app\.venv\Scripts\haiku-ingester.EXE", "haiku-ingester"),
    ],
)
def test_script_name(command, name):
    assert traced_run.script_name(command) == name


def test_find_entry_point():
    # This package's own console script is installed in the test environment.
    assert traced_run.find_entry_point("si-agent").value == "soliplex.agents.cli:cli"
    assert traced_run.find_entry_point("definitely-not-a-script-7f3a") is None


class TestAttachParent:
    def test_attaches_a_valid_parent(self):
        def attach_and_read():
            assert traced_run.attach_parent({"TRACEPARENT": TRACEPARENT}) is True
            return _current_ids()

        assert _isolated(attach_and_read) == (TRACE_ID, SPAN_ID)

    def test_carries_tracestate(self):
        def attach_and_read():
            traced_run.attach_parent({"TRACEPARENT": TRACEPARENT, "TRACESTATE": "vendor=x"})
            return trace.get_current_span().get_span_context().trace_state.get("vendor")

        assert _isolated(attach_and_read) == "x"

    @pytest.mark.parametrize("environ", [{}, {"TRACEPARENT": ""}, {"TRACESTATE": "vendor=x"}])
    def test_nothing_to_attach(self, environ):
        assert _isolated(traced_run.attach_parent, environ) is False

    def test_a_malformed_traceparent_attaches_nothing(self):
        def attach_and_read():
            attached = traced_run.attach_parent({"TRACEPARENT": "not-a-traceparent"})
            return attached, trace.get_current_span().get_span_context().is_valid

        assert _isolated(attach_and_read) == (False, False)


class TestMain:
    def test_no_command_is_a_usage_error(self, capsys):
        assert traced_run.main([]) == 2
        assert traced_run.USAGE in capsys.readouterr().err

    def test_an_unknown_command_is_127(self, capsys):
        assert traced_run.main(["nope-7f3a", "--x"]) == 127
        assert "nope-7f3a is not a console script in this environment" in capsys.readouterr().err

    def test_runs_the_entry_point_under_the_parent_with_the_original_argv(self, monkeypatch):
        seen = {}

        def fake_cli():
            seen["ids"] = _current_ids()
            seen["argv"] = list(sys.argv)
            return 0

        monkeypatch.setattr(traced_run, "find_entry_point", lambda name: SimpleNamespace(load=lambda: fake_cli))
        monkeypatch.setenv("TRACEPARENT", TRACEPARENT)
        monkeypatch.setattr(sys, "argv", list(sys.argv))  # restored after the test

        result = _isolated(traced_run.main, ["/venv/bin/haiku-ingester", "run-batch", "--db=/x"])

        assert result == 0
        assert seen["ids"] == (TRACE_ID, SPAN_ID)
        assert seen["argv"] == ["/venv/bin/haiku-ingester", "run-batch", "--db=/x"]

    def test_reads_sys_argv_by_default(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["traced_run"])
        assert traced_run.main() == 2


def test_real_subprocess_runs_the_console_script():
    """End to end: `python -m soliplex.agents.traced_run si-agent --help`."""
    result = subprocess.run(
        [sys.executable, "-m", "soliplex.agents.traced_run", "si-agent", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "TRACEPARENT": TRACEPARENT},
    )
    assert result.returncode == 0, result.stderr
    assert "Usage" in result.stdout
