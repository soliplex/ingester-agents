"""Tests for the root CLI's opt-in tracing (``si-agent --otel ...``)."""

import logging
import shlex
import sys
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from opentelemetry.trace import StatusCode
from typer.testing import CliRunner

import soliplex.agents.server  # noqa: F401 -- configures itself at import; do that before the tests mock it
from soliplex.agents import cli as cli_module
from soliplex.agents import telemetry

runner = CliRunner()


@pytest.fixture(autouse=True)
def _keep_logging(monkeypatch):
    """The callback's configure_logging() would replace pytest's log capture."""
    monkeypatch.setattr(cli_module, "configure_logging", lambda: None)


@pytest.fixture
def configure(monkeypatch):
    """Record calls to telemetry.configure and pretend a token is configured."""
    mock = MagicMock(return_value=True)
    monkeypatch.setattr(telemetry, "configure", mock)
    return mock


def _invoke(monkeypatch, *args):
    # Click parses sys.argv in a real run; command_path reads it the same way.
    monkeypatch.setattr(sys, "argv", ["si-agent", *args])
    return runner.invoke(cli_module.cli, list(args))


def _manifest_file(tmp_path):
    path = tmp_path / "m.yml"
    path.write_text("id: m\nname: M\nsource: s\ncomponents:\n  - type: fs\n    name: c\n    path: /data\n")
    return str(path)


def test_without_otel_nothing_is_configured_or_traced(monkeypatch, configure, spans, tmp_path):
    with patch("soliplex.agents.manifest.runner.run_manifests", AsyncMock(return_value=[])):
        result = _invoke(monkeypatch, "manifest", "run", _manifest_file(tmp_path), "--no-load")
    assert result.exit_code == 0, result.output
    configure.assert_not_called()
    assert spans.named("cli") == []


def test_otel_opens_one_root_span_that_the_command_runs_under(monkeypatch, configure, spans, tmp_path):
    async def fake_run_manifests(path, load=False, load_on_error=None, allow_empty_load=None):
        # Spans opened by the command -- inside asyncio.run -- nest under it.
        with telemetry.span("inner", "inner"):
            pass
        return []

    manifest = _manifest_file(tmp_path)
    with patch("soliplex.agents.manifest.runner.run_manifests", side_effect=fake_run_manifests):
        result = _invoke(monkeypatch, "--otel", "manifest", "run", manifest, "--no-load")

    assert result.exit_code == 0, result.output
    configure.assert_called_once()
    (root,) = spans.named("cli")
    (inner,) = spans.named("inner")
    # The message is the command line (without --otel); cli.command just the path.
    assert root.attributes["logfire.msg"] == f"si-agent manifest run {shlex.quote(manifest)} --no-load"
    assert root.attributes[telemetry.CLI_COMMAND] == "manifest run"
    assert root.attributes[telemetry.CLI_EXIT_CODE] == 0
    assert root.status.status_code is StatusCode.UNSET
    assert inner.parent.span_id == root.context.span_id


def test_a_command_that_exits_non_zero_fails_the_span(monkeypatch, configure, spans, tmp_path):
    result = _invoke(monkeypatch, "--otel", "manifest", "run", str(tmp_path / "missing.yml"), "--no-load")
    assert result.exit_code == 1
    (root,) = spans.named("cli")
    assert root.attributes[telemetry.CLI_EXIT_CODE] == 1
    assert root.status.status_code is StatusCode.ERROR
    assert root.events == ()


def test_an_unhandled_exception_fails_the_span_with_it(monkeypatch, configure, spans, tmp_path):
    with patch("soliplex.agents.manifest.runner.run_manifests", AsyncMock(side_effect=RuntimeError("boom"))):
        result = _invoke(monkeypatch, "--otel", "manifest", "run", _manifest_file(tmp_path), "--no-load")
    assert isinstance(result.exception, RuntimeError)
    (root,) = spans.named("cli")
    assert root.status.status_code is StatusCode.ERROR
    assert root.events[0].attributes["exception.type"] == "RuntimeError"


def test_otel_without_a_token_warns_and_does_not_trace(monkeypatch, spans, tmp_path, caplog):
    monkeypatch.setattr(telemetry, "configure", MagicMock(return_value=False))
    with (
        patch("soliplex.agents.manifest.runner.run_manifests", AsyncMock(return_value=[])),
        caplog.at_level(logging.WARNING, logger="soliplex.agents.cli"),
    ):
        result = _invoke(monkeypatch, "--otel", "manifest", "run", _manifest_file(tmp_path), "--no-load")
    assert result.exit_code == 0, result.output
    assert "--otel given but no Logfire token is configured" in caplog.text
    assert spans.named("cli") == []


def test_serve_ignores_otel(monkeypatch, configure, spans):
    with patch("uvicorn.run") as mock_run:
        result = _invoke(monkeypatch, "--otel", "serve")
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    # The server configures Logfire itself (at import), token permitting.
    configure.assert_not_called()
    assert spans.named("cli") == []
