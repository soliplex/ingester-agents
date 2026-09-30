import logging
import sys

import typer

import soliplex.agents.fs.cli as fs
import soliplex.agents.manifest.cli as manifest
import soliplex.agents.scm.cli as scm
import soliplex.agents.webdav.cli as webdav
from soliplex.agents import telemetry
from soliplex.agents.config import configure_logging

logger = logging.getLogger(__name__)


def init(
    ctx: typer.Context,
    otel: bool = typer.Option(
        False,
        "--otel",
        help=(
            "Send this command's traces and logs to Logfire. Needs a Logfire token "
            "(LOGFIRE_TOKEN or /run/secrets/logfire_token). Ignored by `serve`, "
            "which configures Logfire whenever a token is available."
        ),
    ),
):
    configure_logging()
    # `serve` configures Logfire itself, at import: with --reload the app runs
    # in a child process that never passes through this callback.
    if not otel or ctx.invoked_subcommand == "serve":
        return
    if not telemetry.configure():
        logger.warning(
            "--otel given but no Logfire token is configured (LOGFIRE_TOKEN or /run/secrets/logfire_token); not tracing"
        )
        return
    # One root span for the whole command, closed by Click when it finishes.
    # sys.argv is what Click parses; command_path keeps only command names.
    ctx.with_resource(telemetry.CliSpan(telemetry.command_path(ctx.find_root().command, sys.argv[1:])))


cli = typer.Typer(no_args_is_help=True, callback=init)

cli.add_typer(fs.cli, name="fs")
cli.add_typer(manifest.cli, name="manifest")
cli.add_typer(scm.cli, name="scm")
cli.add_typer(webdav.cli, name="webdav")


@cli.command("serve")
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "-h",
        "--host",
        help="Bind socket to this host",
    ),
    port: int = typer.Option(
        8001,
        "-p",
        "--port",
        help="Port number",
    ),
    reload: bool = typer.Option(
        False,
        "-r",
        "--reload",
        help="Reload on file changes",
    ),
    access_log: bool = typer.Option(
        None,
        "--access-log",
        help="Enable/Disable access log",
    ),
):
    """Run the Soliplex Agents API server.

    The server always runs as a single worker process. The manifest
    scheduler keeps its cron state and execution locks in memory, so
    multiple workers would each register every cron and run every manifest
    concurrently. Uvicorn defaults to one worker and multi-worker mode is
    intentionally not exposed (``WEB_CONCURRENCY`` is ignored).
    """
    import uvicorn

    import soliplex.agents.server as server

    uvicorn_kw = {
        "host": host,
        "port": port,
    }

    if access_log is not None:
        uvicorn_kw["access_log"] = access_log

    if reload:
        uvicorn.run(
            "soliplex.agents.server:app",
            factory=False,
            reload=True,
            **uvicorn_kw,
        )
    else:
        uvicorn.run(server.app, **uvicorn_kw)
