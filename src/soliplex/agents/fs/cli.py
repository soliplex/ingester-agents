import asyncio
import json
import logging
from typing import Annotated

import typer

from . import app

logger = logging.getLogger(__name__)


cli = typer.Typer(no_args_is_help=True)


@cli.command("validate-config")
def validate(
    config_file: Annotated[
        str,
        typer.Argument(help="path to document directory"),
    ],
):
    """
    Validate a configuration.

    The inventory is built by scanning the directory's contents.
    """
    asyncio.run(app.validate_config(config_file))


@cli.command("build-config")
def build_config(path: Annotated[str, typer.Argument(help="path to document directory")]):
    """Scan a directory and print the inventory it would ingest, as JSON."""
    config = asyncio.run(app.build_config(path))
    print(json.dumps(config, indent=2))


@cli.command("check-status")
def check_status(
    config_file: Annotated[
        str,
        typer.Argument(help="path to document directory"),
    ],
    source: Annotated[str, typer.Argument(help="source name")],
    detail: Annotated[bool, typer.Option(help="include detailed file list")] = False,
):
    """
    Check the status of files in an inventory.

    The inventory is built by scanning the directory's contents.
    """
    asyncio.run(app.status_report(config_file, source, detail=detail))


if __name__ == "__main__":
    cli()
