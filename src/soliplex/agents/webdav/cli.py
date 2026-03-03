"""WebDAV agent CLI commands."""

import asyncio
import json
import logging
import sys
from typing import Annotated

import httpx
import typer

from . import app

logger = logging.getLogger(__name__)

cli = typer.Typer(no_args_is_help=True)


@cli.command("validate-config")
def validate(
    config_path: Annotated[
        str,
        typer.Argument(help="WebDAV directory path (e.g., /documents)"),
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

    Scans the specified WebDAV directory recursively and validates discovered files.
    """
    try:
        asyncio.run(app.validate_config(config_path, webdav_url, webdav_username, webdav_password))
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.TimeoutException) as e:
        print(f"Connection error: Could not connect to WebDAV server: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        raise SystemExit(1) from None


@cli.command("export-urls")
def export_urls(
    config_path: Annotated[
        str,
        typer.Argument(help="WebDAV directory path (e.g., /documents)"),
    ],
    output: Annotated[
        str,
        typer.Argument(help="Output file path to write URLs to"),
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
    Export discovered URLs to a file.

    Scans the specified WebDAV directory recursively and writes one absolute
    WebDAV path per line. No file content is downloaded.
    """
    try:
        asyncio.run(app.export_urls(config_path, output, webdav_url, webdav_username, webdav_password))
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.TimeoutException) as e:
        print(f"Connection error: Could not connect to WebDAV server: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        raise SystemExit(1) from None


@cli.command("check-status")
def check_status(
    config_path: Annotated[
        str,
        typer.Argument(help="WebDAV directory path (e.g., /documents)"),
    ],
    source: Annotated[str, typer.Argument(help="source name")],
    detail: Annotated[bool, typer.Option(help="include detailed file list")] = False,
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
    Check the status of files in an inventory.

    Scans the specified WebDAV directory recursively and checks file status.
    """
    try:
        asyncio.run(app.status_report(config_path, source, detail, webdav_url, webdav_username, webdav_password))
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.TimeoutException) as e:
        print(f"Connection error: Could not connect to WebDAV server: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        raise SystemExit(1) from None


@cli.command("run-inventory")
def run(
    config_path: Annotated[
        str,
        typer.Argument(help="WebDAV directory path (e.g., /documents)"),
    ],
    source: Annotated[str, typer.Argument(help="source name")],
    start: Annotated[int, typer.Option(help="start index")] = 0,
    end: Annotated[int, typer.Option(help="end index")] = None,
    start_workflows: Annotated[bool, typer.Option(help="start workflows")] = False,
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
    metadata: Annotated[str, typer.Option(help="JSON string of extra metadata to attach to all documents")] = None,
):
    """
    Run an inventory ingestion.

    Scans the specified WebDAV directory recursively and ingests discovered files.
    """
    extra_metadata = json.loads(metadata) if metadata else None
    if start_workflows:
        if workflow_definition_id is None:
            raise Exception("workflow_definition_id is required when start_workflows is true")  # noqa: TRY002
        if param_set_id is None:
            raise Exception("param_set_id is required when start_workflows is true")  # noqa: TRY002
    print(f"loading {config_path} source={source}")
    try:
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
                extra_metadata=extra_metadata,
            )
        )
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.TimeoutException) as e:
        print(f"Connection error: Could not connect to WebDAV server: {e}", file=sys.stderr)
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
            if start_workflows:
                print("workflow result")
                print(json.dumps(res["workflow_result"], indent=2))


@cli.command("run-from-urls")
def run_from_urls(
    urls_file: Annotated[
        str,
        typer.Argument(help="Path to file containing WebDAV URLs (one per line)"),
    ],
    source: Annotated[str, typer.Argument(help="source name")],
    start: Annotated[int, typer.Option(help="start index")] = 0,
    end: Annotated[int, typer.Option(help="end index")] = None,
    start_workflows: Annotated[bool, typer.Option(help="start workflows")] = False,
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
    skip_hash_check: Annotated[
        bool,
        typer.Option(help="Skip hash check and ingest all URLs (avoids downloading files twice)"),
    ] = False,
    metadata: Annotated[str, typer.Option(help="JSON string of extra metadata to attach to all documents")] = None,
):
    """
    Run ingestion from a URL list file.

    Reads a file of WebDAV URLs (one per line) and ingests those specific files.
    """
    extra_metadata = json.loads(metadata) if metadata else None
    if start_workflows:
        if workflow_definition_id is None:
            raise Exception("workflow_definition_id is required when start_workflows is true")  # noqa: TRY002
        if param_set_id is None:
            raise Exception("param_set_id is required when start_workflows is true")  # noqa: TRY002
    print(f"loading URLs from {urls_file} source={source}")
    try:
        res = asyncio.run(
            app.load_inventory_from_urls(
                urls_file,
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
                skip_hash_check=skip_hash_check,
                extra_metadata=extra_metadata,
            )
        )
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.TimeoutException) as e:
        print(f"Connection error: Could not connect to WebDAV server: {e}", file=sys.stderr)
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
            if start_workflows:
                print("workflow result")
                print(json.dumps(res["workflow_result"], indent=2))


if __name__ == "__main__":
    cli()
