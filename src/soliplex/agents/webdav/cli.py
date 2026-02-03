"""WebDAV agent CLI commands."""

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
    config_path: Annotated[
        str,
        typer.Argument(help="path to inventory file or WebDAV directory (e.g., /documents)"),
    ],
    webdav_url: Annotated[
        str,
        typer.Option(help="WebDAV server URL (uses WEBDAV_URL env var if not provided)"),
    ] = None,
    webdav_username: Annotated[
        str,
        typer.Option(help="WebDAV username (uses WEBDAV_USERNAME env var if not provided)"),
    ] = None,
    webdav_password: Annotated[
        str,
        typer.Option(help="WebDAV password (uses WEBDAV_PASSWORD env var if not provided)"),
    ] = None,
):
    """
    Validate a configuration.

    If a local file is provided, it will be treated as an inventory.json config file.
    If a WebDAV path is provided (e.g., /documents), a config will be built from the
    WebDAV directory contents.
    """
    asyncio.run(app.validate_config(config_path, webdav_url, webdav_username, webdav_password))


@cli.command("build-config")
def build_config(
    webdav_path: Annotated[str, typer.Argument(help="WebDAV directory path (e.g., /documents)")],
    webdav_url: Annotated[
        str,
        typer.Option(help="WebDAV server URL (uses WEBDAV_URL env var if not provided)"),
    ] = None,
    webdav_username: Annotated[
        str,
        typer.Option(help="WebDAV username (uses WEBDAV_USERNAME env var if not provided)"),
    ] = None,
    webdav_password: Annotated[
        str,
        typer.Option(help="WebDAV password (uses WEBDAV_PASSWORD env var if not provided)"),
    ] = None,
    output: Annotated[str, typer.Option(help="output file path")] = "inventory.json",
):
    """
    Build configuration from a WebDAV directory.

    Scans the specified WebDAV directory recursively and creates an inventory.json file
    containing metadata for all discovered files.
    """
    config = asyncio.run(app.build_config(webdav_path, webdav_url, webdav_username, webdav_password))
    with open(output, "w") as f:
        json.dump(config, f, indent=2)
    print(f"created {output} with {len(config)} files")


@cli.command("check-status")
def check_status(
    config_path: Annotated[
        str,
        typer.Argument(help="path to inventory file or WebDAV directory (e.g., /documents)"),
    ],
    source: Annotated[str, typer.Argument(help="source name")],
    detail: bool = False,
    webdav_url: Annotated[
        str,
        typer.Option(help="WebDAV server URL (uses WEBDAV_URL env var if not provided)"),
    ] = None,
    webdav_username: Annotated[
        str,
        typer.Option(help="WebDAV username (uses WEBDAV_USERNAME env var if not provided)"),
    ] = None,
    webdav_password: Annotated[
        str,
        typer.Option(help="WebDAV password (uses WEBDAV_PASSWORD env var if not provided)"),
    ] = None,
    endpoint_url: Annotated[
        str,
        typer.Option(help="Ingester API endpoint URL (uses ENDPOINT_URL env var if not provided)"),
    ] = None,
):
    """
    Check the status of files in an inventory.

    If a local file is provided, it will be treated as an inventory.json config file.
    If a WebDAV path is provided, a config will be built from the WebDAV directory contents.
    """
    asyncio.run(app.status_report(config_path, source, detail, webdav_url, webdav_username, webdav_password, endpoint_url))


@cli.command("run-inventory")
def run(
    config_path: Annotated[
        str,
        typer.Argument(help="path to inventory file or WebDAV directory (e.g., /documents)"),
    ],
    source: Annotated[str, typer.Argument(help="source name")],
    start: Annotated[int, typer.Option(help="start index")] = 0,
    end: Annotated[int, typer.Option(help="end index")] = None,
    start_workflows: Annotated[bool, typer.Option(help="start workflows")] = True,
    workflow_definition_id: Annotated[str, typer.Option(help="workflow definition id")] = None,
    param_set_id: Annotated[str, typer.Option(help="param set id")] = None,
    priority: Annotated[int, typer.Option(help="workflow priority")] = 0,
    do_json: Annotated[bool, typer.Option(help="output json")] = False,
    webdav_url: Annotated[
        str,
        typer.Option(help="WebDAV server URL (uses WEBDAV_URL env var if not provided)"),
    ] = None,
    webdav_username: Annotated[
        str,
        typer.Option(help="WebDAV username (uses WEBDAV_USERNAME env var if not provided)"),
    ] = None,
    webdav_password: Annotated[
        str,
        typer.Option(help="WebDAV password (uses WEBDAV_PASSWORD env var if not provided)"),
    ] = None,
    endpoint_url: Annotated[
        str,
        typer.Option(help="Ingester API endpoint URL (uses ENDPOINT_URL env var if not provided)"),
    ] = None,
):
    """
    Run an inventory ingestion.

    If a local file is provided, it will be treated as an inventory.json config file.
    If a WebDAV path is provided, a config will be built from the WebDAV directory contents.
    Path resolution is handled internally via resolve_config_path.
    """
    print(f"loading {config_path} source={source}")
    res = asyncio.run(
        app.load_inventory(
            config_path,
            source,
            start,
            end,
            workflow_definition_id=workflow_definition_id,
            param_set_id=param_set_id,
            start_workflows=start_workflows,
            priority=priority,
            webdav_url=webdav_url,
            webdav_username=webdav_username,
            webdav_password=webdav_password,
            endpoint_url=endpoint_url,
        )
    )
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
            print(f"{len(res['ingested'])} ingested")
            if start_workflows:
                print("workflow result")
                print(json.dumps(res["workflow_result"], indent=2))


if __name__ == "__main__":
    cli()
