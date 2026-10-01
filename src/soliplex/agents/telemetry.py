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

import logging
import re
import shlex
import sys
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode

from soliplex.agents.config import settings

logger = logging.getLogger(__name__)

tracer = trace.get_tracer("soliplex.agents")

# Whether this process has configured Logfire (see `configure`).
_configured = False

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
PRE_RUN_METHOD = "pre_run.method"
PRE_RUN_INDEX = "pre_run.index"
INGESTER_EXIT_CODE = "haiku.returncode"


CLI_COMMAND = "cli.command"
CLI_ARGS = "cli.args"
CLI_EXIT_CODE = "cli.exit_code"

REDACTED = "[redacted]"
# An option whose value is a secret, going by its name.
_SECRET_OPTION = re.compile(r"pass|secret|token|key|auth|credential", re.IGNORECASE)
# Arguments left out of the recorded command line: they are about tracing
# itself, not about what the command did.
_UNRECORDED_ARGS = frozenset({"--otel"})


def configure() -> bool:
    """Configure Logfire for this process and route stdlib logging to it.

    Shared by the server and the CLI. Only acts when a Logfire token is
    available (``/run/secrets/logfire_token`` or ``LOGFIRE_TOKEN``), and never
    raises: a failure is logged and the process carries on untraced.

    Safe to call repeatedly. Logfire itself is configured the first time
    only; the log handler is attached whenever the root logger lacks one,
    because ``configure_logging()`` clears the root handlers -- so call this
    again after it. Attaching only when missing is what keeps a second call
    from sending every record twice.

    Returns:
        Whether Logfire is active in this process.
    """
    global _configured
    if settings.logfire_token is None:
        return False
    try:
        import logfire

        if not _configured:
            logfire.configure(
                token=settings.logfire_token.get_secret_value(),
                service_name=settings.logfire_service_name,
                send_to_logfire=True,
                console=False,
            )
            _configured = True
            logger.info("Logfire configured (service=%s)", settings.logfire_service_name)
        root = logging.getLogger()
        if not any(isinstance(handler, logfire.LogfireLoggingHandler) for handler in root.handlers):
            root.addHandler(logfire.LogfireLoggingHandler())
    except Exception:
        logger.exception("Failed to configure Logfire; continuing without it")
        return False
    return True


def command_path(root, argv: Sequence[str]) -> str:
    """The subcommand names in *argv*, walked against the command tree *root*.

    Only tokens that name a command are kept -- ``webdav check-status /docs
    src --webdav-password hunter2`` is ``webdav check-status`` -- which makes it
    the stable part of a command line, to group runs by. The full line, with
    secrets redacted, is :func:`command_args`.

    Args:
        root: The root ``click.Group`` (a Typer app's command).
        argv: The arguments after the program name.
    """
    path: list[str] = []
    command = root
    for token in argv:
        commands = getattr(command, "commands", None)
        if commands is None:
            break
        if token in commands:
            path.append(token)
            command = commands[token]
    return " ".join(path)


def _secret_options(root) -> frozenset[str]:
    """Every option string in the tree *root* whose value must not be recorded.

    An option qualifies when it takes a value and either hides its input or
    has a name that reads as a secret (``--webdav-password``). Taken from the
    commands' own metadata rather than guessed from the command line, so a
    flag named ``--api-key-enabled`` doesn't swallow the next argument.
    """
    secret: set[str] = set()
    stack = [root]
    while stack:
        command = stack.pop()
        for param in getattr(command, "params", ()):
            if getattr(param, "param_type_name", None) != "option" or getattr(param, "is_flag", False):
                continue
            names = [*param.opts, *param.secondary_opts]
            if getattr(param, "hide_input", False) or any(_SECRET_OPTION.search(name) for name in names):
                secret.update(names)
        stack.extend(getattr(command, "commands", {}).values())
    return frozenset(secret)


def command_args(root, argv: Sequence[str]) -> str:
    """*argv* as a shell-quoted command line, with secret option values redacted.

    ``webdav check-status /docs src --webdav-password hunter2`` becomes
    ``webdav check-status /docs src --webdav-password [redacted]``, and the
    ``--opt=value`` form is redacted the same way. ``--otel`` is left out.

    Args:
        root: The root ``click.Group`` (a Typer app's command).
        argv: The arguments after the program name.
    """
    secret = _secret_options(root)
    recorded: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            recorded.append(REDACTED)
            redact_next = False
        elif token in _UNRECORDED_ARGS:
            continue
        elif token in secret:
            recorded.append(token)
            redact_next = True
        elif "=" in token and token.split("=", 1)[0] in secret:
            recorded.append(f"{token.split('=', 1)[0]}={REDACTED}")
        else:
            recorded.append(token)
    return shlex.join(recorded)


def _exit_code(error: BaseException | None) -> int:
    """The exit code a CLI command is leaving with."""
    if error is None:
        return 0
    # click.exceptions.Exit / ClickException carry `exit_code`; sys.exit a `code`.
    code = getattr(error, "exit_code", None)
    if code is None and isinstance(error, SystemExit):
        code = error.code
    if code is None:
        return 1
    return code if isinstance(code, int) else 1


class CliSpan:
    """The root span for one CLI command, entered from the Typer callback.

    It is registered with ``ctx.with_resource``, so Click closes it when the
    command finishes. Click closes resources with no exception details, so the
    outcome comes from ``sys.exc_info()`` instead, which still holds the
    exception while Click's context is exiting with it. ``typer.Exit(0)`` counts
    as success; any other exit code, or an exception, fails the span.
    """

    def __init__(self, command: str, args: str | None = None) -> None:
        """*command* is the command path (``manifest vacuum``), stable enough to
        group runs by; *args* the full, redacted command line
        (``manifest vacuum /manifests/test.yaml``), which is the span's message.
        """
        args = command if args is None else args
        self._context = span("cli", f"si-agent {args}".rstrip(), {CLI_COMMAND: command, CLI_ARGS: args})
        self.span: Span | None = None

    def __enter__(self) -> Span:
        self.span = self._context.__enter__()
        return self.span

    def __exit__(self, exc_type, exc, tb) -> None:
        error = exc or sys.exc_info()[1]
        code = _exit_code(error)
        self.span.set_attribute(CLI_EXIT_CODE, code)
        if code:
            is_exit = hasattr(error, "exit_code") or isinstance(error, SystemExit)
            fail(self.span, f"exited with code {code}", None if is_exit else error)
        self._context.__exit__(None, None, None)


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
