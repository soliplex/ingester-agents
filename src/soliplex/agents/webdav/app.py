"""WebDAV agent core functionality."""

import hashlib
import logging
from io import BytesIO
from pathlib import Path

import aiofiles
from webdav4.client import Client as WebDAVClient

from soliplex.agents import client
from soliplex.agents.common.config import check_config
from soliplex.agents.common.config import detect_mime_type
from soliplex.agents.common.config import read_config
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

    return WebDAVClient(webdav_url, auth=auth)


async def validate_config(path: str, webdav_url: str = None, webdav_username: str = None, webdav_password: str = None):
    """
    Validate a configuration and print out validation results.

    If path is a file, treats it as a config file.
    If path is a WebDAV path, builds config from WebDAV directory contents.

    Args:
        path: Path to either a config file or WebDAV directory to validate
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password

    Returns:
        None
    """
    config, _ = await resolve_config_path(path, webdav_url, webdav_username, webdav_password)
    validated = check_config(config)
    invalid = [row for row in validated if "valid" in row and not row["valid"]]
    print(f"Validation for {path}")
    print(f"Total files: {len(config)}")
    if invalid:
        print(f"Found {len(invalid)} Invalid files:")
        for row in invalid:
            print(row["path"], row["reason"], row["metadata"]["content-type"])


async def resolve_config_path(
    path: str, webdav_url: str = None, webdav_username: str = None, webdav_password: str = None
) -> tuple[list[dict], str]:
    """
    Resolve a path to a configuration.

    If the path is a local file that exists, treat it as an inventory.json config file.
    Otherwise, treat it as a WebDAV path and build config.

    Args:
        path: Path to either a config file or WebDAV directory
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password

    Returns:
        Tuple of (config list, base_path) where base_path is the WebDAV directory
        or parent directory of config file

    Raises:
        FileNotFoundError: If the local path doesn't exist
        ValidationError: If the config file format is invalid
    """
    # Only treat as local file if it actually exists on the filesystem
    # This prevents WebDAV paths like "/documents" from being interpreted
    # as Windows paths on Windows systems
    path_obj = Path(path)
    try:
        if path_obj.exists() and path_obj.is_file():
            logger.info(f"Using {path} as local config file")
            config = await read_config(path)
            base_path = str(path_obj.parent)
            return config, base_path
    except (OSError, ValueError):
        # Path might be invalid for local filesystem (e.g., contains invalid chars)
        # Treat as WebDAV path
        pass

    # Treat as WebDAV path
    logger.info(f"Building config from WebDAV path {path}")
    config = await build_config(path, webdav_url, webdav_username, webdav_password)
    return config, path


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
        sha256_hash = hashlib.sha256(content).hexdigest()

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

    # Fix for Git Bash on Windows: paths starting with / get converted to Windows paths
    # If path looks like a Windows path (contains :), it was likely converted
    if ":" in path and path.startswith("C:"):
        # Extract the original WebDAV path
        # C:/Program Files/Git/Plone/docs -> /Plone/docs
        parts = path.split("Git")
        if len(parts) > 1:
            path = parts[1].replace("\\", "/")
            logger.warning(f"Detected Git Bash path conversion, using: {path}")

    logger.debug(f"Listing WebDAV directory: {path}")

    try:
        resources = client.ls(path, detail=True)
        for resource in resources:
            resource_path = resource["name"]
            logger.debug(f"Found resource: {resource_path}, type: {resource.get('type', 'unknown')}")

            # Skip the directory itself
            if resource_path.rstrip("/") == path.rstrip("/"):
                continue

            if resource["type"] == "directory":
                # Recursively list subdirectory
                subdir_files = await recursive_listdir_webdav(client, resource_path)
                file_list.extend(subdir_files)
            else:
                file_list.append({"path": resource_path, "size": resource.get("size", 0)})
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
    start_workflows: bool = True,
    param_set_id: str | None = None,
    priority: int = 0,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    endpoint_url: str = None,
):
    """
    Load and process an inventory for ingestion.

    If path is a local file, treats it as a config file.
    If path is a WebDAV path, builds config from WebDAV directory contents.

    Args:
        path: Path to either a config file or WebDAV directory to process
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
    # Temporarily override endpoint_url if provided
    original_endpoint = settings.endpoint_url
    if endpoint_url:
        settings.endpoint_url = endpoint_url

    try:
        config, base_path = await resolve_config_path(path, webdav_url, webdav_username, webdav_password)
        if skip_invalid:
            filtered = check_config(config)
            config = [x for x in filtered if x["valid"]]

        logger.info(f"found {len(config)} files in {path}")
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
            wf_res = await client.do_start_workflows(
                batch_id,
                workflow_definition_id,
                param_set_id,
                priority,
            )
        ret["ingested"] = ingested
        ret["errors"] = errors
        ret["workflow_result"] = wf_res
        return ret
    finally:
        # Restore original endpoint_url
        settings.endpoint_url = original_endpoint


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
    if Path(base_path).exists():
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


async def status_report(
    config_path: str,
    source: str,
    detail: bool = False,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    endpoint_url: str = None,
):
    """
    Generate a status report for an inventory.

    If config_path is a local file, treats it as a config file.
    If config_path is a WebDAV path, builds config from WebDAV directory contents.

    Args:
        config_path: Path to either a config file or WebDAV directory
        source: Source identifier to check against
        detail: Whether to print detailed file list (default: False)
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password
        endpoint_url: Optional Ingester API endpoint URL
    """
    # Temporarily override endpoint_url if provided
    original_endpoint = settings.endpoint_url
    if endpoint_url:
        settings.endpoint_url = endpoint_url

    try:
        print(f"checking status for {config_path} source={source} ")
        config, _ = await resolve_config_path(config_path, webdav_url, webdav_username, webdav_password)
        to_process = await client.check_status(config, source)
        print(f"Files to process: {len(to_process)}")
        print(f"Total files: {len(config)}")
        if detail and len(to_process) > 0:
            for row in to_process:
                print(row)
    finally:
        # Restore original endpoint_url
        settings.endpoint_url = original_endpoint
