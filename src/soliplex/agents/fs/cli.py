import asyncio
import json
import logging
import sys
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


@cli.command("run-inventory")
def run(
    config_file: Annotated[
        str,
        typer.Argument(help="path to document directory"),
    ],
    source: Annotated[str, typer.Argument(help="source name")],
    start: Annotated[int, typer.Option(help="start index")] = 0,
    end: Annotated[int, typer.Option(help="end index")] = None,
    do_json: Annotated[bool, typer.Option(help="output json")] = False,
    metadata: Annotated[str, typer.Option(help="JSON string of extra metadata to attach to all documents")] = None,
):
    """
    Run an inventory ingestion.

    The inventory is built by scanning the directory's contents.
    """
    extra_metadata = json.loads(metadata) if metadata else None
    print(f"loading {config_file} source={source}")
    try:
        res = asyncio.run(
            app.load_inventory(
                config_file,
                source,
                start,
                end,
                extra_metadata=extra_metadata,
            )
        )
    except (FileNotFoundError, NotADirectoryError) as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    if do_json:
        print(json.dumps(res, indent=2))
    else:
        if "errors" in res and len(res["errors"]) > 0:
            print(f"found {len(res['errors'])} errors:")
            for err in res["errors"]:
                print(err)
        else:
            print("no errors found")
            print(f"found {len(res['inventory'])} files")
            print(f"found {len(res['to_process'])} to process")
            if "ingested" in res and len(res["ingested"]) > 0:
                print(f"{len(res['ingested'])} ingested")
            else:
                print("no ingested files")


if __name__ == "__main__":
    app()
