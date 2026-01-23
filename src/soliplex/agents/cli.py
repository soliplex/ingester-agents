import logging
import os

import typer

import soliplex.agents.fs.cli as fs
import soliplex.agents.scm.cli as scm
import soliplex.agents.webdav.cli as webdav

logger = logging.getLogger(__name__)


def init():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


cli = typer.Typer(no_args_is_help=True, callback=init)

cli.add_typer(fs.cli, name="fs")
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
    workers: int = typer.Option(
        None,
        "--workers",
        envvar="WEB_CONCURRENCY",
        help="Number of worker processes",
    ),
    access_log: bool = typer.Option(
        None,
        "--access-log",
        help="Enable/Disable access log",
    ),
):
    """Run the Soliplex Agents API server."""
    import uvicorn

    import soliplex.agents.server as server

    uvicorn_kw = {
        "host": host,
        "port": port,
    }

    if workers is not None:
        uvicorn_kw["workers"] = workers

    if access_log is not None:
        uvicorn_kw["access_log"] = access_log

    if reload or workers:
        uvicorn.run(
            "soliplex.agents.server:app",
            factory=False,
            reload=reload,
            **uvicorn_kw,
        )
    else:
        uvicorn.run(server.app, **uvicorn_kw)
