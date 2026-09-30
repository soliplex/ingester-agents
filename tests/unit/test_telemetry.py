"""Tests for the shared span helpers — 100% branch coverage required."""

import logging

import logfire
import pytest
import typer
from opentelemetry.trace import StatusCode
from pydantic import SecretStr

from soliplex.agents import telemetry
from soliplex.agents.cli import cli as root_cli
from soliplex.agents.config import Manifest
from soliplex.agents.config import settings


class _FakeLogfireHandler(logging.Handler):
    def emit(self, record):
        pass


@pytest.fixture
def fake_logfire(monkeypatch):
    """Stand in for logfire.configure / its handler: nothing is exported."""
    calls = []
    monkeypatch.setattr(logfire, "configure", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(logfire, "LogfireLoggingHandler", _FakeLogfireHandler)
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(settings, "logfire_token", SecretStr("tok"), raising=False)
    root = logging.getLogger()
    yield calls
    for handler in [h for h in root.handlers if isinstance(h, _FakeLogfireHandler)]:
        root.removeHandler(handler)


def _fake_handlers():
    return [h for h in logging.getLogger().handlers if isinstance(h, _FakeLogfireHandler)]


class TestConfigure:
    def test_no_token_is_a_noop(self, fake_logfire, monkeypatch):
        monkeypatch.setattr(settings, "logfire_token", None, raising=False)
        assert telemetry.configure() is False
        assert fake_logfire == []
        assert _fake_handlers() == []

    def test_configures_once_and_attaches_one_handler(self, fake_logfire):
        assert telemetry.configure() is True
        assert telemetry.configure() is True
        assert len(fake_logfire) == 1
        assert fake_logfire[0]["token"] == "tok"
        assert fake_logfire[0]["service_name"] == settings.logfire_service_name
        # A second call must not add a second handler: every record would be sent twice.
        assert len(_fake_handlers()) == 1

    def test_reattaches_the_handler_after_configure_logging_clears_it(self, fake_logfire):
        telemetry.configure()
        for handler in _fake_handlers():
            logging.getLogger().removeHandler(handler)
        telemetry.configure()
        assert len(fake_logfire) == 1
        assert len(_fake_handlers()) == 1

    def test_a_failure_is_logged_not_raised(self, fake_logfire, monkeypatch, caplog):
        def boom(**kwargs):
            raise RuntimeError("bad token")

        monkeypatch.setattr(logfire, "configure", boom)
        with caplog.at_level(logging.ERROR, logger="soliplex.agents.telemetry"):
            assert telemetry.configure() is False
        assert "Failed to configure Logfire" in caplog.text


class TestCommandPath:
    @pytest.fixture
    def root(self):
        return typer.main.get_command(root_cli)

    def test_keeps_command_names_only(self, root):
        argv = ["--otel", "webdav", "run-inventory", "/docs", "--webdav-password", "hunter2"]
        assert telemetry.command_path(root, argv) == "webdav run-inventory"

    def test_option_values_between_commands_are_skipped(self, root):
        assert telemetry.command_path(root, ["manifest", "--bogus", "x", "run", "a.yml"]) == "manifest run"

    def test_no_command(self, root):
        assert telemetry.command_path(root, ["--otel"]) == ""


class TestCommandArgs:
    @pytest.fixture
    def root(self):
        return typer.main.get_command(root_cli)

    def test_positional_arguments_are_kept(self, root):
        argv = ["--otel", "manifest", "vacuum", "/manifests/test.yaml"]
        assert telemetry.command_args(root, argv) == "manifest vacuum /manifests/test.yaml"

    def test_secret_option_values_are_redacted(self, root):
        argv = ["webdav", "run-inventory", "/docs", "--webdav-password", "hunter2", "--webdav-username", "bob"]
        assert telemetry.command_args(root, argv) == (
            "webdav run-inventory /docs --webdav-password '[redacted]' --webdav-username bob"
        )

    def test_the_equals_form_is_redacted_too(self, root):
        argv = ["webdav", "check-status", "--webdav-password=hunter2"]
        assert telemetry.command_args(root, argv) == "webdav check-status '--webdav-password=[redacted]'"

    def test_arguments_are_shell_quoted(self, root):
        assert telemetry.command_args(root, ["manifest", "run", "/my manifests/a.yaml"]) == (
            "manifest run '/my manifests/a.yaml'"
        )

    def test_a_secret_option_at_the_end_redacts_nothing_further(self, root):
        assert telemetry.command_args(root, ["webdav", "check-status", "--webdav-password"]) == (
            "webdav check-status --webdav-password"
        )

    def test_hidden_input_and_secret_names_count_but_flags_do_not(self):
        import click

        @click.command()
        @click.option("--pin", hide_input=True)
        @click.option("--api-key-enabled", is_flag=True)
        @click.argument("path")
        def leaf(pin, api_key_enabled, path): ...

        root = click.Group(commands={"leaf": leaf})
        argv = ["leaf", "--pin", "1234", "--api-key-enabled", "/p"]
        # The flag takes no value, so the argument after it is not redacted.
        assert telemetry.command_args(root, argv) == "leaf --pin '[redacted]' --api-key-enabled /p"


class TestCliSpan:
    def test_success(self, spans):
        with telemetry.CliSpan("manifest run"):
            pass
        (span,) = spans.named("cli")
        assert span.attributes["logfire.msg"] == "si-agent manifest run"
        assert span.attributes[telemetry.CLI_COMMAND] == "manifest run"
        assert span.attributes[telemetry.CLI_ARGS] == "manifest run"
        assert span.attributes[telemetry.CLI_EXIT_CODE] == 0
        assert span.status.status_code is StatusCode.UNSET

    def test_an_exception_fails_it_with_the_exception(self, spans):
        with pytest.raises(RuntimeError), telemetry.CliSpan("fs run"):
            raise RuntimeError("boom")
        (span,) = spans.named("cli")
        assert span.attributes[telemetry.CLI_EXIT_CODE] == 1
        assert span.status.description == "exited with code 1"
        assert span.events[0].name == "exception"

    @pytest.mark.parametrize(
        "error, code",
        [(SystemExit(2), 2), (typer.Exit(3), 3), (SystemExit("message"), 1), (SystemExit(None), 1)],
    )
    def test_a_non_zero_exit_fails_it_without_an_exception_event(self, spans, error, code):
        with pytest.raises(type(error)), telemetry.CliSpan("x"):
            raise error
        (span,) = spans.named("cli")
        assert span.attributes[telemetry.CLI_EXIT_CODE] == code
        assert span.status.status_code is StatusCode.ERROR
        assert span.events == ()

    def test_exit_zero_is_success(self, spans):
        with pytest.raises(typer.Exit), telemetry.CliSpan("x"):
            raise typer.Exit(0)
        assert spans.named("cli")[0].status.status_code is StatusCode.UNSET

    def test_outcome_read_while_click_is_exiting_with_an_exception(self, spans):
        # Click closes resources with no exception details; the span must
        # still see the exception that is propagating.
        def exit_four():
            raise typer.Exit(4)

        cli_span = telemetry.CliSpan("x")
        cli_span.__enter__()
        try:
            exit_four()
        except typer.Exit:
            cli_span.__exit__(None, None, None)
        assert spans.named("cli")[0].attributes[telemetry.CLI_EXIT_CODE] == 4


def test_the_message_is_the_full_command_line(spans):
    with telemetry.CliSpan("manifest vacuum", "manifest vacuum /manifests/test.yaml"):
        pass
    (span,) = spans.named("cli")
    assert span.attributes["logfire.msg"] == "si-agent manifest vacuum /manifests/test.yaml"
    assert span.attributes[telemetry.CLI_COMMAND] == "manifest vacuum"
    assert span.attributes[telemetry.CLI_ARGS] == "manifest vacuum /manifests/test.yaml"


def test_empty_command_message(spans):
    with telemetry.CliSpan(""):
        pass
    assert spans.named("cli")[0].attributes["logfire.msg"] == "si-agent"


def _manifest():
    return Manifest(
        id="docs-site",
        name="Docs",
        source="docs-src",
        components=[
            {"type": "fs", "name": "a", "path": "/a"},
            {"type": "fs", "name": "b", "path": "/b"},
        ],
    )


class TestSpan:
    def test_sets_message_and_drops_none_attributes(self, spans):
        with telemetry.span("component", "component wiki (fs)", {"component.name": "wiki", "gone": None}):
            pass
        (span,) = spans.named("component")
        assert span.attributes["logfire.msg"] == "component wiki (fs)"
        assert span.attributes["component.name"] == "wiki"
        assert "gone" not in span.attributes
        assert span.status.status_code is StatusCode.UNSET

    def test_without_attributes(self, spans):
        with telemetry.span("x", "x message"):
            pass
        assert spans.named("x")[0].attributes == {"logfire.msg": "x message"}

    def test_an_escaping_exception_fails_the_span(self, spans):
        with pytest.raises(RuntimeError), telemetry.span("x", "x"):
            raise RuntimeError("boom")
        (span,) = spans.named("x")
        assert span.status.status_code is StatusCode.ERROR
        assert span.events[0].name == "exception"


class TestFail:
    def test_with_exception(self, spans):
        with telemetry.span("x", "x") as span:
            telemetry.fail(span, "it broke", ValueError("bad"))
        (finished,) = spans.named("x")
        assert finished.status.status_code is StatusCode.ERROR
        assert finished.status.description == "it broke"
        assert finished.events[0].attributes["exception.type"] == "ValueError"

    def test_without_exception(self, spans):
        with telemetry.span("x", "x") as span:
            telemetry.fail(span, "3 file errors")
        (finished,) = spans.named("x")
        assert finished.status.description == "3 file errors"
        assert finished.events == ()


class TestManifestSpan:
    def test_name_message_and_attributes(self, spans):
        with telemetry.manifest_span("docs-site", {telemetry.MANIFEST_PATH: "/m/docs.yml"}) as span:
            telemetry.describe_manifest(span, _manifest())
        (finished,) = spans.named("manifest run")
        assert finished.attributes["logfire.msg"] == "manifest docs-site"
        assert finished.attributes[telemetry.MANIFEST_ID] == "docs-site"
        assert finished.attributes[telemetry.MANIFEST_PATH] == "/m/docs.yml"
        assert finished.attributes[telemetry.MANIFEST_NAME] == "Docs"
        assert finished.attributes[telemetry.MANIFEST_SOURCE] == "docs-src"
        assert finished.attributes[telemetry.MANIFEST_COMPONENTS] == 2

    def test_without_extra_attributes(self, spans):
        with telemetry.manifest_span("m"):
            pass
        assert spans.named("manifest run")[0].attributes[telemetry.MANIFEST_ID] == "m"


class TestRecordSummary:
    def test_clean_run_copies_counts_and_stays_ok(self, spans):
        with telemetry.span("x", "x") as span:
            telemetry.record_summary(span, {"components": 2, "component_errors": 0, "file_errors": 0, "ingested": 5})
        (finished,) = spans.named("x")
        assert finished.attributes["manifest.components"] == 2
        assert finished.attributes["manifest.ingested"] == 5
        assert finished.status.status_code is StatusCode.UNSET

    @pytest.mark.parametrize(
        "component_errors, file_errors, description",
        [
            (1, 0, "1 component errors, 0 file errors"),
            (0, 3, "0 component errors, 3 file errors"),
        ],
    )
    def test_any_failure_fails_the_span(self, spans, component_errors, file_errors, description):
        summary = {"component_errors": component_errors, "file_errors": file_errors}
        with telemetry.span("x", "x") as span:
            telemetry.record_summary(span, summary)
        (finished,) = spans.named("x")
        assert finished.status.status_code is StatusCode.ERROR
        assert finished.status.description == description

    def test_empty_summary(self, spans):
        with telemetry.span("x", "x") as span:
            telemetry.record_summary(span, {})
        assert spans.named("x")[0].status.status_code is StatusCode.UNSET
