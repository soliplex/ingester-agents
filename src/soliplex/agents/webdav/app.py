"""WebDAV agent core functionality."""

import hashlib
import json
import logging
from datetime import UTC
from datetime import datetime
from io import BytesIO
from pathlib import Path

import aiofiles
from webdav4.client import Client as WebDAVClient

from soliplex.agents import client
from soliplex.agents.common.config import check_config
from soliplex.agents.common.config import detect_mime_type
from soliplex.agents.config import settings

logger = logging.getLogger(__name__)


def create_webdav_client(url: str = None, username: str = None, password: str = None) -> WebDAVClient:
    """
    Create a WebDAV client with credentials from settings or parameters.

    Args:
        url: WebDAV server URL (uses settings.webdav_url if not provided)
        username: WebDAV username (uses settings.webdav_username if not provided)
        password: WebDAV password (uses settings.webdav_password if not provided)

    Returns:
        Configured WebDAV client

    Raises:
        ValueError: If required credentials are missing
    """
    webdav_url = url or settings.webdav_url
    webdav_username = username or settings.webdav_username
    webdav_password = password or settings.webdav_password

    if not webdav_url:
        raise ValueError("WebDAV URL is required (set WEBDAV_URL environment variable)")

    auth = None
    if webdav_username and webdav_password:
        auth = (webdav_username, webdav_password)
    headers = {"User-Agent": "soliplex-agent/curl"}
    return WebDAVClient(webdav_url, auth=auth, verify=settings.ssl_verify, headers=headers)


async def validate_config(path: str, webdav_url: str = None, webdav_username: str = None, webdav_password: str = None):
    """
    Validate a configuration and print out validation results.

    Builds config from WebDAV directory contents and validates files.

    Args:
        path: WebDAV directory path to validate (e.g., /documents)
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password

    Returns:
        None
    """
    config = await build_config(path, webdav_url, webdav_username, webdav_password)
    validated = check_config(config)
    invalid = [row for row in validated if "valid" in row and not row["valid"]]
    print(f"Validation for {path}")
    print(f"Total files: {len(config)}")
    if invalid:
        print(f"Found {len(invalid)} Invalid files:")
        for row in invalid:
            print(row["path"], row["reason"], row["metadata"]["content-type"])


async def export_urls(
    path: str, output_path: str, webdav_url: str = None, webdav_username: str = None, webdav_password: str = None
):
    """
    Export discovered WebDAV URLs to a file without downloading content.

    Uses list_config (PROPFIND only) to discover files, then writes
    their absolute paths to the output file.

    Args:
        path: WebDAV directory path to scan (e.g., /documents)
        output_path: File path to write URLs to
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password

    Returns:
        None
    """
    config = await list_config(path, webdav_url, webdav_username, webdav_password)
    count = await export_urls_to_file(config, path, output_path)
    print(f"Found {len(config)} files in {path}")
    print(f"Exported {count} URLs to {output_path}")


async def export_urls_to_file(config: list[dict], base_path: str, output_path: str) -> int:
    """
    Export discovered URLs to a file, one absolute WebDAV path per line.

    Args:
        config: Config list with relative paths
        base_path: Base WebDAV path used during discovery
        output_path: File path to write URLs to

    Returns:
        Number of URLs written
    """
    normalized_base = base_path.rstrip("/")
    async with aiofiles.open(output_path, "w") as f:
        for item in config:
            absolute_path = f"{normalized_base}/{item['path']}"
            await f.write(absolute_path + "\n")
    return len(config)


async def build_config_from_urls(
    urls_file: str, webdav_url: str = None, webdav_username: str = None, webdav_password: str = None
) -> tuple[list[dict], list[dict]]:
    """
    Build config from a file containing one absolute WebDAV path per line.

    Each URL is processed independently; errors are captured per-URL
    so that one failure does not stop the whole list.

    Args:
        urls_file: Path to file containing WebDAV URLs (one per line)
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password

    Returns:
        Tuple of (config list, results list). The config list contains
        successfully processed files. The results list contains one entry
        per URL with status and optional error_message.
    """
    webdav_client = create_webdav_client(webdav_url, webdav_username, webdav_password)
    allowed_extensions = settings.extensions
    config = []
    results = []

    async with aiofiles.open(urls_file) as f:
        content = await f.read()

    lines = [line.strip() for line in content.splitlines() if line.strip()]

    for full_path in lines:
        ext = Path(full_path).suffix.lstrip(".")
        if ext not in allowed_extensions:
            logger.info(f"skipping {full_path}")
            results.append({"url": full_path, "status": "skipped", "error_message": f"Extension .{ext} not allowed"})
            continue

        try:
            buffer = BytesIO()
            webdav_client.download_fileobj(full_path, buffer)
            content_bytes = buffer.getvalue()
            sha256_hash = hashlib.sha256(content_bytes, usedforsecurity=False).hexdigest()
            mime_type = detect_mime_type(full_path)

            rec = {
                "path": full_path,
                "sha256": sha256_hash,
                "metadata": {
                    "size": len(content_bytes),
                    "content-type": mime_type,
                },
            }
            config.append(rec)
            results.append({"url": full_path, "status": "success", "error_message": None})
        except Exception as e:
            logger.exception(f"Error processing {full_path}")
            results.append({"url": full_path, "status": "error", "error_message": str(e)})

    return config, results


async def list_config(
    webdav_path: str, webdav_url: str = None, webdav_username: str = None, webdav_password: str = None
) -> list[dict]:
    """
    List files in a WebDAV directory without downloading content.

    Only uses PROPFIND to discover files. No GET requests are made.
    Suitable for validation and URL export where file content is not needed.

    Args:
        webdav_path: Path within WebDAV server (e.g., "/documents")
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password

    Returns:
        List of file configuration dictionaries (without sha256)
    """
    webdav_client = create_webdav_client(webdav_url, webdav_username, webdav_password)
    allowed_extensions = settings.extensions
    config = []

    files = await recursive_listdir_webdav(webdav_client, webdav_path)

    for file_info in files:
        full_path = file_info["path"]
        ext = Path(full_path).suffix.lstrip(".")

        if ext not in allowed_extensions:
            logger.info(f"skipping {full_path}")
            continue

        mime_type = detect_mime_type(full_path)

        normalized_base = webdav_path.strip("/")
        normalized_full = full_path.strip("/")

        if normalized_full.startswith(normalized_base + "/"):
            relative_path = normalized_full[len(normalized_base) + 1 :]
        elif normalized_full == normalized_base:
            relative_path = ""
        else:
            relative_path = normalized_full

        rec = {
            "path": relative_path,
            "metadata": {
                "size": file_info["size"],
                "content-type": mime_type,
            },
        }
        config.append(rec)

    return config


async def build_config(
    webdav_path: str, webdav_url: str = None, webdav_username: str = None, webdav_password: str = None
) -> list[dict]:
    """
    Scan a WebDAV directory and create inventory configuration.

    Args:
        webdav_path: Path within WebDAV server (e.g., "/documents")
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password

    Returns:
        List of file configuration dictionaries
    """
    client = create_webdav_client(webdav_url, webdav_username, webdav_password)
    allowed_extensions = settings.extensions
    config = []

    # Recursively list all files
    files = await recursive_listdir_webdav(client, webdav_path)

    for file_info in files:
        full_path = file_info["path"]  # This is the absolute WebDAV path
        ext = Path(full_path).suffix.lstrip(".")

        if ext not in allowed_extensions:
            logger.info(f"skipping {full_path}")
            continue

        # Get file content for hashing
        # Use download_fileobj to get content as bytes
        buffer = BytesIO()
        client.download_fileobj(full_path, buffer)
        content = buffer.getvalue()
        sha256_hash = hashlib.sha256(content, usedforsecurity=False).hexdigest()

        # Detect MIME type
        mime_type = detect_mime_type(full_path)

        # Make path relative to webdav_path
        # Normalize both paths for comparison - remove leading and trailing slashes
        normalized_base = webdav_path.strip("/")
        normalized_full = full_path.strip("/")

        logger.debug(f"Comparing - Full: '{normalized_full}', Base: '{normalized_base}'")

        # Check if full path starts with base path
        if normalized_full.startswith(normalized_base + "/"):
            # Path starts with base + slash
            relative_path = normalized_full[len(normalized_base) + 1 :]
        elif normalized_full == normalized_base:
            # Path equals base (shouldn't happen for files, but handle it)
            relative_path = ""
        else:
            # Path doesn't contain base - use the full normalized path
            relative_path = normalized_full

        logger.debug(f"Full WebDAV path: {full_path}, Base: {webdav_path}, Relative: {relative_path}")

        rec = {
            "path": relative_path,
            "sha256": sha256_hash,
            "metadata": {
                "size": file_info["size"],
                "content-type": mime_type,
            },
        }
        config.append(rec)

    return config


async def recursive_listdir_webdav(client: WebDAVClient, path: str) -> list[dict]:
    """
    Recursively list files in a WebDAV directory.

    Args:
        client: WebDAV client instance
        path: Directory path to list

    Returns:
        List of file info dictionaries with 'path' and 'size'
    """
    file_list = []

    logger.debug(f"Listing WebDAV directory: {path}")

    try:
        resources = client.ls(path, detail=True)
        for resource in resources:
            resource_path = resource["name"]
            logger.debug(f"Found resource: {resource_path}, type: {resource.get('type', 'unknown')}")

            # Skip the directory itself
            if resource_path.rstrip("/") == path.rstrip("/") or resource["name"].split("/")[-1] == "_data":
                continue

            if resource["type"] == "directory":
                # Recursively list subdirectory
                subdir_files = await recursive_listdir_webdav(client, resource_path)
                file_list.extend(subdir_files)
            else:
                rec = {"path": resource_path, "size": resource.get("content_length", 0)}
                for key in [x for x in resource.keys() if x not in ["href", "etag", "type", "name"]]:
                    rec[key] = resource.get(key)
                file_list.append(rec)
    except Exception:
        logger.exception(f"Error listing WebDAV directory {path}")

    return file_list


async def load_inventory(
    path: str,
    source: str,
    start: int = 0,
    end: int = None,
    skip_invalid: bool = False,
    workflow_definition_id: str | None = None,
    start_workflows: bool = False,
    param_set_id: str | None = None,
    priority: int = 0,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    config: list[dict] | None = None,
    skip_status_check: bool = False,
):
    """
    Load and process an inventory for ingestion.

    Builds config from WebDAV directory contents and ingests files.

    Args:
        path: WebDAV directory path to process (e.g., /documents)
        source: Source identifier for the batch
        start: Starting index for processing (default: 0)
        end: Ending index for processing (default: None, processes all)
        skip_invalid: Skip files that fail validation (default: False)
        workflow_definition_id: Optional workflow to start after ingestion
        start_workflows: Whether to start workflows (default: True)
        param_set_id: Parameter set for workflows
        priority: Workflow priority (default: 0)
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password
        endpoint_url: Optional Ingester API endpoint URL

    Returns:
        Dictionary with inventory, to_process, batch_id, ingested, errors,
        and workflow_result
    """

    client.validate_parameters(start_workflows, workflow_definition_id, param_set_id)
    if config is None:
        config = await build_config(path, webdav_url, webdav_username, webdav_password)
    base_path = path
    if skip_invalid:
        filtered = check_config(config)
        config = [x for x in filtered if x["valid"]]

    logger.info(f"found {len(config)} files in {path}")
    if skip_status_check:
        to_process = config
        logger.info(f"skipping status check, processing all {len(to_process)} files")
    else:
        to_process = await client.check_status(config, source)
        logger.info(f"found {len(to_process)} out of {len(config)} to process in {base_path}")
    if end is None:
        end = len(config)
        to_process = to_process[start:end]
    ret = {
        "inventory": config,
        "to_process": to_process,
        "batch_id": None,
        "ingested": [],
        "errors": [],
        "workflow_result": None,
    }
    if len(to_process) == 0:
        logger.info("nothing to process. exiting")
        return ret

    found_batch_id = await client.find_batch_for_source(source)
    if found_batch_id:
        logger.info(f"found batch {found_batch_id} for {source}")
        batch_id = found_batch_id
    else:
        logger.info(f"no batch found for {source}. creating")
        batch_id = await client.create_batch(source, source)
    ret["batch_id"] = batch_id
    ingested = []
    errors = []
    for row in to_process:
        meta = row["metadata"].copy()
        for k in [
            "path",
            "sha256",
            "size",
            "source",
            "batch_id",
            "source_uri",
        ]:
            if k in meta:
                del meta[k]
        logger.info(f"starting ingest for {row['path']}")
        mime_type = None
        if "metadata" in row and "content-type" in row["metadata"]:
            mime_type = row["metadata"]["content-type"]
        res = await do_ingest(
            base_path,
            row["path"],
            meta,
            source,
            batch_id,
            mime_type,
            webdav_url,
            webdav_username,
            webdav_password,
        )
        if "error" in res:
            logger.error(f"Error ingesting {row['path']}: {res['error']}")
            res["uri"] = row["path"]
            res["source"] = source
            res["batch_id"] = batch_id
            errors.append(res)
        else:
            ingested.append(res)
    wf_res = None
    if len(errors) == 0 and start_workflows:
        wf_res = await client.start_workflows_for_batch(
            batch_id,
            workflow_definition_id,
            param_set_id,
            priority,
        )
    ret["ingested"] = ingested
    ret["errors"] = errors
    ret["workflow_result"] = wf_res
    return ret


async def do_ingest(
    base_path: str,
    uri: str,
    meta: dict[str, str],
    source: str,
    batch_id: int,
    mime_type: str,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
):
    """
    Ingest a single file from WebDAV or local filesystem.

    Args:
        base_path: Base directory or WebDAV path
        uri: Relative file path
        meta: File metadata
        source: Source identifier
        batch_id: Batch ID for ingestion
        mime_type: MIME type of the file
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password

    Returns:
        Result dictionary from ingestion API
    """
    logger.info(f"base_path={base_path}, uri={uri}")

    # Check if base_path is a local directory
    if base_path and Path(base_path).exists():
        # Local file ingestion
        load_path = Path(base_path) / uri
        logger.debug(f"Loading from local path: {load_path}")
        async with aiofiles.open(load_path, "rb") as f:
            doc_body = await f.read()
    else:
        # WebDAV file ingestion
        webdav_client = create_webdav_client(webdav_url, webdav_username, webdav_password)
        full_path = f"{base_path.rstrip('/')}/{uri.lstrip('/')}"
        logger.info(f"Downloading from WebDAV: {full_path}")
        buffer = BytesIO()
        webdav_client.download_fileobj(full_path, buffer)
        doc_body = buffer.getvalue()

    return await client.do_ingest(
        doc_body,
        uri,
        meta,
        source,
        batch_id,
        mime_type,
    )


async def load_inventory_from_urls(
    urls_file: str,
    source: str,
    start: int = 0,
    end: int = None,
    skip_invalid: bool = False,
    workflow_definition_id: str | None = None,
    start_workflows: bool = False,
    param_set_id: str | None = None,
    priority: int = 0,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    skip_hash_check: bool = False,
):
    """
    Load and process an inventory from a URL list file.

    Reads URLs from file, builds config, then delegates to load_inventory.

    Args:
        urls_file: Path to file containing WebDAV URLs (one per line)
        source: Source identifier for the batch
        start: Starting index for processing (default: 0)
        end: Ending index for processing (default: None, processes all)
        skip_invalid: Skip files that fail validation (default: False)
        workflow_definition_id: Optional workflow to start after ingestion
        start_workflows: Whether to start workflows (default: False)
        param_set_id: Parameter set for workflows
        priority: Workflow priority (default: 0)
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password
        skip_hash_check: Skip downloading files for hash comparison and
            assume all URLs need ingestion (default: False)

    Returns:
        Dictionary with inventory, to_process, batch_id, ingested, errors,
        url_results, and url_results_path
    """
    if skip_hash_check:
        config, url_results = await _read_urls_as_config(urls_file)
    else:
        config, url_results = await build_config_from_urls(urls_file, webdav_url, webdav_username, webdav_password)

    # Write results to <urls_file>.results.<timestamp>.json
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    results_path = f"{urls_file}.results.{timestamp}.json"
    async with aiofiles.open(results_path, "w") as f:
        await f.write(json.dumps(url_results, indent=2))
    logger.info(f"URL results written to {results_path}")

    result = await load_inventory(
        path="",
        source=source,
        start=start,
        end=end,
        skip_invalid=skip_invalid,
        workflow_definition_id=workflow_definition_id,
        start_workflows=start_workflows,
        param_set_id=param_set_id,
        priority=priority,
        webdav_url=webdav_url,
        webdav_username=webdav_username,
        webdav_password=webdav_password,
        config=config,
        skip_status_check=skip_hash_check,
    )
    result["url_results"] = url_results
    result["url_results_path"] = results_path
    return result


async def _read_urls_as_config(urls_file: str) -> tuple[list[dict], list[dict]]:
    """
    Read URLs from file and build lightweight config without downloading.

    Filters by allowed extensions and detects MIME types from paths.
    No WebDAV connection or file downloads are performed.

    Args:
        urls_file: Path to file containing WebDAV URLs (one per line)

    Returns:
        Tuple of (config list, results list)
    """
    allowed_extensions = settings.extensions
    config = []
    results = []

    async with aiofiles.open(urls_file) as f:
        content = await f.read()

    lines = [line.strip() for line in content.splitlines() if line.strip()]

    for full_path in lines:
        ext = Path(full_path).suffix.lstrip(".")
        if ext not in allowed_extensions:
            logger.info(f"skipping {full_path}")
            results.append({"url": full_path, "status": "skipped", "error_message": f"Extension .{ext} not allowed"})
            continue

        mime_type = detect_mime_type(full_path)
        rec = {
            "path": full_path,
            "metadata": {
                "content-type": mime_type,
            },
        }
        config.append(rec)
        results.append({"url": full_path, "status": "success", "error_message": None})

    return config, results


async def status_report(
    config_path: str,
    source: str,
    detail: bool = False,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
):
    """
    Generate a status report for an inventory.

    Builds config from WebDAV directory contents and checks status.

    Args:
        config_path: WebDAV directory path (e.g., /documents)
        source: Source identifier to check against
        detail: Whether to print detailed file list (default: False)
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password

    """

    print(f"checking status for {config_path} source={source} ")
    config = await build_config(config_path, webdav_url, webdav_username, webdav_password)
    to_process = await client.check_status(config, source)
    print(f"Files to process: {len(to_process)}")
    print(f"Total files: {len(config)}")
    if detail and len(to_process) > 0:
        for row in to_process:
            print(row)
