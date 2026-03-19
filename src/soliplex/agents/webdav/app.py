"""WebDAV agent core functionality."""

import hashlib
import logging
from io import BytesIO
from pathlib import Path

import aiofiles
import httpx
from webdav4.client import Client as WebDAVClient

from soliplex.agents import client
from soliplex.agents.common.config import check_config
from soliplex.agents.common.config import detect_mime_type
from soliplex.agents.config import settings
from soliplex.agents.webdav import state as webdav_state

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
    webdav_password = password or (settings.webdav_password.get_secret_value() if settings.webdav_password else None)

    if not webdav_url:
        raise ValueError("WebDAV URL is required (set WEBDAV_URL environment variable)")

    auth = None
    if webdav_username and webdav_password:
        auth = (webdav_username, webdav_password)
    headers = {"User-Agent": "soliplex-agent/curl"}
    timeout = httpx.Timeout(60.0, connect=20.0)
    return WebDAVClient(webdav_url, auth=auth, verify=settings.ssl_verify, headers=headers, timeout=timeout)


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

    Uses ETag-based caching to avoid re-downloading unchanged files.
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

    resolved_url = webdav_url or settings.webdav_url or ""
    cached_state = webdav_state.load_state(resolved_url)
    new_state = {}
    current_paths = set()

    async with aiofiles.open(urls_file) as f:
        content = await f.read()

    lines = [line.strip() for line in content.splitlines() if line.strip()]

    for full_path in lines:
        current_paths.add(full_path)
        ext = Path(full_path).suffix.lstrip(".")
        if ext not in allowed_extensions:
            logger.info(f"skipping {full_path}")
            results.append({"url": full_path, "status": "skipped", "error_message": f"Extension .{ext} not allowed"})
            continue

        try:
            # Try to get ETag via info() for cache check
            server_etag = None
            try:
                info = webdav_client.info(full_path)
                server_etag = info.get("etag")
            except Exception:
                logger.debug(f"Could not get info for {full_path}, will download")

            cached_entry = cached_state.get(full_path)

            if server_etag and cached_entry and cached_entry.get("etag") == server_etag:
                # ETag cache hit
                sha256_hash = cached_entry["sha256"]
                mime_type = detect_mime_type(full_path)
                rec = {
                    "path": full_path,
                    "sha256": sha256_hash,
                    "metadata": {
                        "size": cached_entry.get("size", 0),
                        "content-type": mime_type,
                    },
                }
                if server_etag:
                    new_state[full_path] = {"etag": server_etag, "sha256": sha256_hash, "size": cached_entry.get("size", 0)}
                config.append(rec)
                results.append({"url": full_path, "status": "success", "error_message": None})
                continue

            # Download file
            buffer = BytesIO()
            webdav_client.download_fileobj(full_path, buffer)
            content_bytes = buffer.getvalue()
            sha256_hash = hashlib.sha256(content_bytes, usedforsecurity=False).hexdigest()
            mime_type = detect_mime_type(full_path)

            if server_etag:
                new_state[full_path] = {"etag": server_etag, "sha256": sha256_hash, "size": len(content_bytes)}

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

    # Prune deleted files
    _, removed = webdav_state.prune_state(cached_state, current_paths)
    for removed_path in removed:
        logger.warning(f"File removed from URL list: {removed_path}")

    webdav_state.save_state(resolved_url, new_state)
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

    Uses ETag-based caching to avoid re-downloading unchanged files.

    Args:
        webdav_path: Path within WebDAV server (e.g., "/documents")
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password

    Returns:
        List of file configuration dictionaries
    """
    webdav_client = create_webdav_client(webdav_url, webdav_username, webdav_password)
    allowed_extensions = settings.extensions
    config = []
    failed = 0
    cache_hits = 0

    resolved_url = webdav_url or settings.webdav_url or ""
    cached_state = webdav_state.load_state(resolved_url)
    new_state = {}

    # Recursively list all files
    files = await recursive_listdir_webdav(webdav_client, webdav_path)
    current_paths = set()

    for file_info in files:
        full_path = file_info["path"]  # This is the absolute WebDAV path
        current_paths.add(full_path)
        ext = Path(full_path).suffix.lstrip(".")

        if ext not in allowed_extensions:
            logger.info(f"skipping {full_path}")
            continue

        server_etag = file_info.get("etag")
        cached_entry = cached_state.get(full_path)

        # Check ETag cache
        if server_etag and cached_entry and cached_entry.get("etag") == server_etag:
            sha256_hash = cached_entry["sha256"]
            cache_hits += 1
            logger.debug(f"ETag cache hit for {full_path}")
        else:
            # Download and compute hash
            try:
                buffer = BytesIO()
                webdav_client.download_fileobj(full_path, buffer)
                content = buffer.getvalue()
            except Exception:
                logger.exception(f"Error downloading {full_path}, skipping")
                failed += 1
                continue

            sha256_hash = hashlib.sha256(content, usedforsecurity=False).hexdigest()

        # Update state entry
        if server_etag:
            new_state[full_path] = {"etag": server_etag, "sha256": sha256_hash}

        # Detect MIME type
        mime_type = detect_mime_type(full_path)

        # Make path relative to webdav_path
        normalized_base = webdav_path.strip("/")
        normalized_full = full_path.strip("/")

        logger.debug(f"Comparing - Full: '{normalized_full}', Base: '{normalized_base}'")

        if normalized_full.startswith(normalized_base + "/"):
            relative_path = normalized_full[len(normalized_base) + 1 :]
        elif normalized_full == normalized_base:
            relative_path = ""
        else:
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

    # Prune deleted files and log warnings
    _, removed = webdav_state.prune_state(cached_state, current_paths)
    for removed_path in removed:
        logger.warning(f"File removed from server: {removed_path}")

    webdav_state.save_state(resolved_url, new_state)
    logger.info(f"Built config: {len(config)} files succeeded, {failed} failed, {cache_hits} cache hits")
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
                if "etag" in resource:
                    rec["etag"] = resource["etag"]
                for key in [x for x in resource.keys() if x not in ["href", "etag", "type", "name"]]:
                    rec[key] = resource.get(key)
                file_list.append(rec)
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.TimeoutException):
        logger.exception(f"Connection error listing {path}")
        raise
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
    extra_metadata: dict[str, str] | None = None,
    delete_stale: bool = False,
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
        delete_stale: Remove documents not in inventory (default: False)

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
        logger.info("nothing to process")
        if start_workflows:
            batch_id = await client.find_or_create_batch(source)
            ret["workflow_result"] = await client.start_workflows_for_batch(
                batch_id, workflow_definition_id, param_set_id, priority
            )
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
    for idx, row in enumerate(to_process):
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
        if extra_metadata:
            meta.update(extra_metadata)
        logger.info(f"starting ingest for {row['path']} {idx}/{len(to_process)} ")
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
    delete_stale_result = None
    if delete_stale and len(errors) == 0:
        delete_stale_result = await client.check_status(
            config,
            source,
            delete_stale=True,
        )
    ret["ingested"] = ingested
    ret["errors"] = errors
    ret["workflow_result"] = wf_res
    ret["delete_stale_result"] = delete_stale_result
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
        try:
            webdav_client = create_webdav_client(webdav_url, webdav_username, webdav_password)
            full_path = f"{base_path.rstrip('/')}/{uri.lstrip('/')}"
            logger.info(f"Downloading from WebDAV: {full_path}")
            buffer = BytesIO()
            webdav_client.download_fileobj(full_path, buffer)
            doc_body = buffer.getvalue()
        except Exception as e:
            logger.exception(f"Error downloading {uri} from WebDAV")
            return {"error": str(e)}

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
    extra_metadata: dict[str, str] | None = None,
    delete_stale: bool = False,
):
    """
    Load and process an inventory from a URL list file.

    Reads URLs from file, builds config with ETag caching, then delegates to load_inventory.

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
        delete_stale: Remove documents not in inventory (default: False)

    Returns:
        Dictionary with inventory, to_process, batch_id, ingested, errors,
        and url_results
    """
    config, url_results = await build_config_from_urls(urls_file, webdav_url, webdav_username, webdav_password)

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
        extra_metadata=extra_metadata,
        delete_stale=delete_stale,
    )
    result["url_results"] = url_results
    return result


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
