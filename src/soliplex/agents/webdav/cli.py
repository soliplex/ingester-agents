"""WebDAV agent CLI commands."""

import asyncio
import logging
import sys
from typing import Annotated

import aiohttp
import typer

from soliplex.agents import WebDAVListingError

from . import app
from .async_client import ClientError

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
    exclude: Annotated[
        list[str] | None,
        typer.Option(help="Glob, relative to the path, of a folder or file to skip (repeatable)"),
    ] = None,
):
    """
    Validate a configuration.

    Scans the specified WebDAV directory recursively and validates discovered files.
    """
    try:
        asyncio.run(app.validate_config(config_path, webdav_url, webdav_username, webdav_password, exclude))
    except WebDAVListingError as e:
        print(f"Listing error: {e} (skip a folder on purpose with --exclude)", file=sys.stderr)
        raise SystemExit(1) from None
    except (aiohttp.ClientConnectorError, ClientError, TimeoutError) as e:
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
    exclude: Annotated[
        list[str] | None,
        typer.Option(help="Glob, relative to the path, of a folder or file to skip (repeatable)"),
    ] = None,
):
    """
    Export discovered URLs to a file.

    Scans the specified WebDAV directory recursively and writes one absolute
    WebDAV path per line. No file content is downloaded.
    """
    try:
        asyncio.run(app.export_urls(config_path, output, webdav_url, webdav_username, webdav_password, exclude))
    except WebDAVListingError as e:
        print(f"Listing error: {e} (skip a folder on purpose with --exclude)", file=sys.stderr)
        raise SystemExit(1) from None
    except (aiohttp.ClientConnectorError, ClientError, TimeoutError) as e:
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
    exclude: Annotated[
        list[str] | None,
        typer.Option(help="Glob, relative to the path, of a folder or file to skip (repeatable)"),
    ] = None,
):
    """
    Check the status of files in an inventory.

    Scans the specified WebDAV directory recursively and checks file status.
    """
    try:
        asyncio.run(
            app.status_report(
                config_path, source, detail, webdav_url, webdav_username, webdav_password, exclude_paths=exclude
            )
        )
    except WebDAVListingError as e:
        print(f"Listing error: {e} (skip a folder on purpose with --exclude)", file=sys.stderr)
        raise SystemExit(1) from None
    except (aiohttp.ClientConnectorError, ClientError, TimeoutError) as e:
        print(f"Connection error: Could not connect to WebDAV server: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
