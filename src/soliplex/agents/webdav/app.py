"""WebDAV agent core functionality."""

import hashlib
import logging
from pathlib import Path

import aiofiles
import aiohttp

from soliplex.agents import local_state
from soliplex.agents import local_store
from soliplex.agents.common.config import check_config
from soliplex.agents.common.config import detect_mime_type
from soliplex.agents.config import settings
from soliplex.agents.webdav.async_client import AsyncWebDAVClient
from soliplex.agents.webdav.async_client import ResourceNotFound
from soliplex.agents.webdav.async_client import create_async_webdav_client

logger = logging.getLogger(__name__)

_STRIP_KEYS = ("path", "sha256", "size", "source", "batch_id", "source_uri", "content-type", "_etag")


def _doc_meta(row: dict, extra_metadata: dict[str, str] | None) -> dict:
    """Build the sidecar metadata for a WebDAV inventory row."""
    meta = dict(row.get("metadata") or {})
    for k in _STRIP_KEYS:
        meta.pop(k, None)
    if extra_metadata:
        meta.update(extra_metadata)
    return meta


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
    urls_file: str,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    base_dir: str | None = None,
    source: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Build config from a file containing one absolute WebDAV path per line.

    Uses ETag-based caching (against the per-source local state) to avoid
    re-downloading unchanged files. Each URL is processed independently;
    errors are captured per-URL so one failure does not stop the list.

    Args:
        urls_file: Path or S3 URL to file containing WebDAV URLs (one per line)
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password
        base_dir: Optional directory for resolving relative local paths
        source: Source identifier used for the ETag cache lookup

    Returns:
        Tuple of (config list, results list). The config list contains
        successfully processed files. The results list contains one entry
        per URL with status and optional error_message.
    """
    from soliplex.agents.common.urls_file import read_urls_file

    webdav_client = create_async_webdav_client(webdav_url, webdav_username, webdav_password)
    allowed_extensions = settings.extensions
    config = []
    results = []

    cached_state = local_state.load_file_state(source) if source else {}

    lines = await read_urls_file(
        urls_file,
        base_dir,
        webdav_url=webdav_url,
        webdav_username=webdav_username,
        webdav_password=webdav_password,
    )

    async with webdav_client:
        for full_path in lines:
            ext = Path(full_path).suffix.lstrip(".")
            if ext not in allowed_extensions:
                logger.info(f"skipping {full_path}")
                results.append({"url": full_path, "status": "skipped", "error_message": f"Extension .{ext} not allowed"})
                continue

            try:
                # Try to get ETag via info() or HEAD for cache check
                server_etag = None
                try:
                    info = await webdav_client.info(full_path)
                    server_etag = info.get("etag")
                    if not server_etag:  # pragma: no cover
                        logger.info(f"Could not get ETag for {full_path} {info}")
                    else:  # pragma: no cover
                        logger.info(f"Got ETag {server_etag} for {full_path}")
                except Exception:
                    logger.debug(f"Could not get info for {full_path}")
                if not server_etag:
                    try:
                        resp = await webdav_client.head(full_path)
                        server_etag = resp.headers.get("etag")
                    except Exception:
                        logger.debug(f"Could not HEAD {full_path}")

                cached_entry = cached_state.get(full_path)
                mime_type = detect_mime_type(full_path)

                if server_etag and cached_entry and cached_entry.get("etag") == server_etag:
                    # ETag cache hit — reuse cached SHA256, no download
                    rec = {
                        "path": full_path,
                        "sha256": cached_entry["sha256"],
                        "metadata": {
                            "size": cached_entry.get("size", 0),
                            "content-type": mime_type,
                        },
                        "_etag": server_etag,
                    }
                else:
                    # ETag cache miss — defer download to write step
                    rec = {
                        "path": full_path,
                        "sha256": None,
                        "metadata": {
                            "size": 0,
                            "content-type": mime_type,
                        },
                    }
                    if server_etag:
                        rec["_etag"] = server_etag
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
    webdav_client = create_async_webdav_client(webdav_url, webdav_username, webdav_password)
    allowed_extensions = settings.extensions
    config = []

    async with webdav_client:
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
    webdav_path: str,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    source: str | None = None,
) -> list[dict]:
    """
    Scan a WebDAV directory and create inventory configuration.

    Uses ETag-based caching (against the per-source local state) to avoid
    re-downloading unchanged files.

    Args:
        webdav_path: Path within WebDAV server (e.g., "/documents")
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password
        source: Source identifier used for the ETag cache lookup

    Returns:
        List of file configuration dictionaries
    """
    webdav_client = create_async_webdav_client(webdav_url, webdav_username, webdav_password)
    allowed_extensions = settings.extensions
    config = []
    cache_hits = 0

    cached_state = local_state.load_file_state(source) if source else {}

    async with webdav_client:
        # Recursively list all files
        files = await recursive_listdir_webdav(webdav_client, webdav_path)

        for file_info in files:
            full_path = file_info["path"]  # This is the absolute WebDAV path
            ext = Path(full_path).suffix.lstrip(".")

            if ext not in allowed_extensions:
                logger.info(f"skipping {full_path}")
                continue

            server_etag = file_info.get("etag")
            if not server_etag:
                try:
                    resp = await webdav_client.head(full_path)
                    server_etag = resp.headers.get("etag")
                except Exception:
                    logger.debug(f"Could not HEAD {full_path}")

            # Make path relative to webdav_path
            normalized_base = webdav_path.strip("/")
            normalized_full = full_path.strip("/")

            if normalized_full.startswith(normalized_base + "/"):
                relative_path = normalized_full[len(normalized_base) + 1 :]
            elif normalized_full == normalized_base:
                relative_path = ""
            else:
                relative_path = normalized_full

            cached_entry = cached_state.get(relative_path)
            etag_for_rec = None

            if server_etag and cached_entry and cached_entry.get("etag") == server_etag:
                sha256_hash = cached_entry["sha256"]
                cache_hits += 1
                logger.debug(f"ETag cache hit for {full_path}")
            else:
                # ETag cache miss — defer download to write step
                sha256_hash = None
                etag_for_rec = server_etag

            mime_type = detect_mime_type(full_path)

            rec = {
                "path": relative_path,
                "sha256": sha256_hash,
                "metadata": {
                    "size": file_info["size"],
                    "content-type": mime_type,
                },
            }
            if sha256_hash is None and etag_for_rec:
                rec["_etag"] = etag_for_rec
            config.append(rec)

    logger.info(f"Built config: {len(config)} files, {cache_hits} cache hits")
    return config


async def recursive_listdir_webdav(webdav_client: AsyncWebDAVClient, path: str) -> list[dict]:
    """
    Recursively list files in a WebDAV directory.

    Args:
        webdav_client: Async WebDAV client instance
        path: Directory path to list

    Returns:
        List of file info dictionaries with 'path' and 'size'
    """
    file_list = []

    logger.debug(f"Listing WebDAV directory: {path}")

    try:
        resources = await webdav_client.ls(path, detail=True)
        for resource in resources:
            rel_name = resource["name"]
            logger.debug(f"Found resource: {rel_name}, type: {resource.get('type', 'unknown')}")

            basename = rel_name.rstrip("/").split("/")[-1]
            if not basename or basename == "_data":
                continue

            full_resource_path = f"{path.rstrip('/')}/{rel_name.lstrip('/')}"

            if resource["type"] == "directory":
                subdir_files = await recursive_listdir_webdav(webdav_client, full_resource_path)
                file_list.extend(subdir_files)
            else:
                rec = {"path": full_resource_path, "size": resource.get("content_length", 0)}
                if "etag" in resource:
                    rec["etag"] = resource["etag"]
                for key in [x for x in resource.keys() if x not in ["href", "etag", "type", "name"]]:
                    rec[key] = resource.get(key)
                file_list.append(rec)
    except (TimeoutError, ConnectionError, aiohttp.ClientError, ResourceNotFound):
        logger.exception(f"Connection error listing {path}")
        raise
    except Exception:
        logger.error(
            "Error listing WebDAV directory %s, returning partial results",
            path,
            exc_info=True,
        )

    return file_list


async def load_inventory(
    path: str,
    source: str,
    start: int = 0,
    end: int = None,
    skip_invalid: bool = False,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    config: list[dict] | None = None,
    extra_metadata: dict[str, str] | None = None,
    delete_stale: bool = False,
):
    """
    Load an inventory and write changed files to the download directory.

    Builds config from WebDAV directory contents and writes files locally.

    Args:
        path: WebDAV directory path to process (e.g., /documents)
        source: Source identifier (becomes the per-source download folder)
        start: Starting index for processing (default: 0)
        end: Ending index for processing (default: None, processes all)
        skip_invalid: Skip files that fail validation (default: False)
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password
        config: Pre-built config (skips discovery when provided)
        extra_metadata: Extra metadata attached to every document
        delete_stale: Remove documents not in inventory (default: False)

    Returns:
        Dictionary with inventory, to_process, ingested, errors, and
        delete_stale_result
    """
    if config is None:
        config = await build_config(path, webdav_url, webdav_username, webdav_password, source=source)
    base_path = path
    if skip_invalid:
        filtered = check_config(config)
        config = [x for x in filtered if x["valid"]]

    logger.info(f"found {len(config)} files in {path}")

    to_process = local_state.compute_to_process(config, source)
    if end is None:
        end = len(config)
    to_process = to_process[start:end]
    logger.info(f"found {len(to_process)} out of {len(config)} to process in {base_path}")

    ingested = []
    errors = []
    ret = {"inventory": config, "to_process": to_process, "ingested": ingested, "errors": errors}
    for idx, row in enumerate(to_process):
        uri = row["path"]
        try:
            meta = _doc_meta(row, extra_metadata)
            etag = row.get("_etag")
            logger.info(f"writing {uri} {idx + 1}/{len(to_process)}")
            mime_type = (row.get("metadata") or {}).get("content-type") or detect_mime_type(uri)
            res = await do_ingest(
                base_path,
                uri,
                meta,
                source,
                mime_type,
                webdav_url,
                webdav_username,
                webdav_password,
                etag=etag,
            )
            if "error" in res:
                logger.error(f"Error writing {uri}: {res['error']}")
                errors.append({"uri": uri, "error": res["error"]})
            else:
                ingested.append(uri)
        except Exception as e:
            logger.exception("Failed to write %s", uri)
            errors.append({"uri": uri, "error": str(e)})

    delete_stale_result = None
    if delete_stale and len(errors) == 0:
        delete_stale_result = local_state.prune_documents(source, {r["path"] for r in config})
    ret["delete_stale_result"] = delete_stale_result
    return ret


async def do_ingest(
    base_path: str,
    uri: str,
    meta: dict[str, str],
    source: str,
    mime_type: str,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    etag: str | None = None,
):
    """
    Read a file from WebDAV (or local filesystem) and write it locally.

    Args:
        base_path: Base directory or WebDAV path
        uri: Relative file path
        meta: File metadata for the sidecar
        source: Source identifier
        mime_type: MIME type of the file
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password
        etag: Server ETag to record in local state, if known

    Returns:
        Result dictionary with success/error information
    """
    logger.info(f"base_path={base_path}, uri={uri}")

    # Check if base_path is a local directory
    if base_path and Path(base_path).exists():
        load_path = Path(base_path) / uri
        logger.debug(f"Loading from local path: {load_path}")
        async with aiofiles.open(load_path, "rb") as f:
            doc_body = await f.read()
    else:
        try:
            webdav_client = create_async_webdav_client(webdav_url, webdav_username, webdav_password)
            full_path = f"{base_path.rstrip('/')}/{uri.lstrip('/')}" if base_path else uri
            logger.info(f"Downloading from WebDAV: {full_path}")
            async with webdav_client:
                doc_body = await webdav_client.download(full_path)
        except Exception as e:
            logger.exception(f"Error downloading {uri} from WebDAV")
            return {"error": str(e)}

        # Capture ETag via HTTP HEAD if not already known
        if not etag and webdav_url:
            try:
                wc = create_async_webdav_client(webdav_url, webdav_username, webdav_password)
                head_path = full_path if base_path else uri
                async with wc:
                    resp = await wc.head(head_path)
                etag = resp.headers.get("etag")
            except Exception:
                logger.debug(f"Could not get ETag via HEAD for {uri}")

    sha256_hash = hashlib.sha256(doc_body, usedforsecurity=False).hexdigest()
    local_store.write_document(source, uri, doc_body, mime_type, meta)
    local_state.upsert_file(source, uri, sha256_hash, etag=etag, size=len(doc_body), mime_type=mime_type)
    return {"result": "success", "uri": uri, "_sha256": sha256_hash, "_size": len(doc_body)}


async def load_inventory_from_urls(
    urls_file: str,
    source: str,
    start: int = 0,
    end: int = None,
    skip_invalid: bool = False,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    extra_metadata: dict[str, str] | None = None,
    delete_stale: bool = False,
    base_dir: str | None = None,
):
    """
    Load an inventory from a URL list file and write files locally.

    Reads URLs from file, builds config with ETag caching, then delegates
    to load_inventory.

    Args:
        urls_file: Path or S3 URL to file containing WebDAV URLs (one per line)
        source: Source identifier (becomes the per-source download folder)
        start: Starting index for processing (default: 0)
        end: Ending index for processing (default: None, processes all)
        skip_invalid: Skip files that fail validation (default: False)
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password
        extra_metadata: Extra metadata attached to every document
        delete_stale: Remove documents not in inventory (default: False)
        base_dir: Optional directory for resolving relative local paths

    Returns:
        Dictionary with inventory, to_process, ingested, errors, and
        url_results
    """
    config, url_results = await build_config_from_urls(
        urls_file, webdav_url, webdav_username, webdav_password, base_dir=base_dir, source=source
    )

    result = await load_inventory(
        path="",
        source=source,
        start=start,
        end=end,
        skip_invalid=skip_invalid,
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
    config = await build_config(config_path, webdav_url, webdav_username, webdav_password, source=source)
    to_process = local_state.compute_to_process(config, source)
    print(f"Files to process: {len(to_process)}")
    print(f"Total files: {len(config)}")
    if detail and len(to_process) > 0:
        for row in to_process:
            print(row)
