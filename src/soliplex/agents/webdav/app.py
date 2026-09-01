"""WebDAV agent core functionality."""

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import aiofiles
import aiohttp

from soliplex.agents import local_state
from soliplex.agents import local_store
from soliplex.agents.common.config import check_config
from soliplex.agents.common.mime import detect_mime_type
from soliplex.agents.common.mime import extension_allowed
from soliplex.agents.common.mime import passes_extension_prefilter
from soliplex.agents.config import settings
from soliplex.agents.webdav.async_client import AsyncWebDAVClient
from soliplex.agents.webdav.async_client import ResourceNotFound
from soliplex.agents.webdav.async_client import create_async_webdav_client

logger = logging.getLogger(__name__)

_STRIP_KEYS = ("path", "sha256", "size", "source", "batch_id", "source_uri", "content-type", "_etag")


@asynccontextmanager
async def _client_for(
    client: AsyncWebDAVClient | None,
    webdav_url: str | None,
    webdav_username: str | None,
    webdav_password: str | None,
):
    """Yield *client* if given, else an owned one closed on exit.

    A run threads a single client through discovery and every download so the
    connection pool is reused: building one per file costs a TCP connect and
    TLS handshake each time, which dominates an initial sync. Callers that
    pass nothing (tests, direct entry points) keep the old own-it behaviour.
    """
    if client is not None:
        yield client
        return
    owned = create_async_webdav_client(webdav_url, webdav_username, webdav_password)
    async with owned:
        yield owned


@asynccontextmanager
async def _optional_shared_client(
    webdav_url: str | None,
    webdav_username: str | None,
    webdav_password: str | None,
):
    """Yield a run-level client when one can be built, else ``None``.

    A run may have no WebDAV URL at all -- a local ``base_path``, or a
    prebuilt config with nothing left to fetch -- and warming a connection
    pool must not be what makes those fail. Callers pass the result straight
    to :func:`_client_for`, so ``None`` simply restores the old
    create-per-call behaviour and the missing-URL error still surfaces at the
    point that actually needed a connection.
    """
    try:
        owned = create_async_webdav_client(webdav_url, webdav_username, webdav_password)
    except ValueError:
        logger.debug("No WebDAV URL configured; not opening a shared client")
        yield None
        return
    async with owned:
        yield owned


def _listing_semaphore() -> asyncio.Semaphore:
    """Bound concurrent WebDAV requests to the configured limit."""
    return asyncio.Semaphore(settings.webdav_max_concurrent_requests)


def _doc_meta(row: dict, extra_metadata: dict[str, str] | None) -> dict:
    """Build the sidecar metadata for a WebDAV inventory row."""
    meta = dict(row.get("metadata") or {})
    for k in _STRIP_KEYS:
        meta.pop(k, None)
    if extra_metadata:
        meta.update(extra_metadata)
    return meta


def _version_token(etag, modified) -> tuple[str | None, str | None]:
    """Return a cache validator for a remote file and where it came from.

    Prefers the strong ETag. When the server omits ETags (some WebDAV
    servers do) it falls back to the last-modified timestamp, which is
    still good enough to detect changes. ``modified`` may be a ``datetime``
    (from a PROPFIND listing) or an HTTP-date string (from a ``Last-Modified``
    header); both normalise to a stable string.

    Returns:
        ``(token, source)`` where source is ``"etag"`` or ``"modified"``,
        or ``(None, None)`` when neither is available.
    """
    if etag:
        return etag, "etag"
    if modified is not None:
        iso = getattr(modified, "isoformat", None)
        token = iso() if callable(iso) else str(modified)
        return token, "modified"
    return None, None


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
    client: AsyncWebDAVClient | None = None,
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

    allowed_extensions = settings.extensions
    cached_state = local_state.load_file_state(source) if source else {}

    lines = await read_urls_file(
        urls_file,
        base_dir,
        webdav_url=webdav_url,
        webdav_username=webdav_username,
        webdav_password=webdav_password,
    )

    async with _client_for(client, webdav_url, webdav_username, webdav_password) as webdav_client:
        semaphore = _listing_semaphore()

        async def _probe(full_path: str) -> tuple[dict | None, dict]:
            """Resolve one URL's validator and cache state.

            Returns ``(config_row_or_None, result_row)`` so the caller can
            rebuild both lists in input order.
            """
            # Coarse pre-filter: allowed extension or none (extension-less
            # files are typed from the server header / content at download).
            if not passes_extension_prefilter(full_path, allowed_extensions):
                logger.info(f"skipping {full_path}")
                ext = Path(full_path).suffix.lstrip(".")
                return None, {
                    "url": full_path,
                    "status": "skipped",
                    "error_message": f"Extension .{ext} not allowed",
                }

            # Validator for the cache check: prefer ETag, fall back to
            # last-modified (this server omits ETags but sends modified).
            server_etag = None
            modified = None
            server_content_type = None
            async with semaphore:
                try:
                    info = await webdav_client.info(full_path)
                    server_etag = info.get("etag")
                    modified = info.get("modified")
                    server_content_type = info.get("content_type")
                except Exception:
                    logger.debug("Could not get info for %s", full_path, exc_info=True)
                if not server_etag:
                    try:
                        resp = await webdav_client.head(full_path)
                        server_etag = resp.headers.get("etag")
                        if not modified:
                            modified = resp.headers.get("last-modified")
                        if not server_content_type:
                            server_content_type = resp.headers.get("content-type")
                    except Exception:
                        logger.debug("Could not HEAD %s", full_path, exc_info=True)

            server_token, token_source = _version_token(server_etag, modified)
            if server_token:
                logger.debug("validator for %s via %s: %s", full_path, token_source, server_token)
            else:
                logger.info("no etag or last-modified for %s -- will re-download every run", full_path)

            cached_entry = cached_state.get(full_path)
            # Provisional type from the server header (falls back to the
            # extension). The authoritative type is resolved from headers
            # + content at download time in do_ingest.
            mime_type = detect_mime_type(full_path, header_type=server_content_type)

            if server_token and cached_entry and cached_entry.get("etag") == server_token:
                # Cache hit — reuse cached SHA256, no download
                logger.debug("cache HIT for %s (validator=%s via %s)", full_path, server_token, token_source)
                rec = {
                    "path": full_path,
                    "sha256": cached_entry["sha256"],
                    "metadata": {
                        "size": cached_entry.get("size", 0),
                        "content-type": mime_type,
                    },
                    "_etag": server_token,
                }
            else:
                # Cache miss — defer download to write step
                rec = {
                    "path": full_path,
                    "sha256": None,
                    "metadata": {
                        "size": 0,
                        "content-type": mime_type,
                    },
                }
                if server_token:
                    rec["_etag"] = server_token
            return rec, {"url": full_path, "status": "success", "error_message": None}

        probed = await asyncio.gather(*(_probe(line) for line in lines), return_exceptions=True)

    # Rebuilt in input order so both lists match the sequential version.
    config = []
    results = []
    for full_path, outcome in zip(lines, probed, strict=True):
        if isinstance(outcome, BaseException):
            logger.error("Error processing %s", full_path, exc_info=outcome)
            results.append({"url": full_path, "status": "error", "error_message": str(outcome)})
            continue
        rec, result = outcome
        if rec is not None:
            config.append(rec)
        results.append(result)

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

        if not passes_extension_prefilter(full_path, allowed_extensions):
            logger.info(f"skipping {full_path}")
            continue

        mime_type = detect_mime_type(full_path, header_type=file_info.get("content_type"))
        # Drop only positively-identified disallowed types. An indeterminate
        # type (octet-stream: no header, no extension) is deferred so it can
        # be sniffed from content when the file is downloaded for ingestion.
        if mime_type != "application/octet-stream" and not extension_allowed(mime_type, allowed_extensions):
            logger.info(f"skipping {full_path} (detected {mime_type})")
            continue

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
    client: AsyncWebDAVClient | None = None,
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
        client: Existing client to reuse; one is created and closed here when
            omitted.

    Returns:
        List of file configuration dictionaries
    """
    allowed_extensions = settings.extensions
    config = []
    cache_hits = 0
    cache_misses = 0
    via_etag = 0
    via_modified = 0
    via_none = 0

    cached_state = local_state.load_file_state(source) if source else {}
    logger.info(
        "build_config: scanning %s (source=%r, %d cached state entries)",
        webdav_path,
        source,
        len(cached_state),
    )

    async with _client_for(client, webdav_url, webdav_username, webdav_password) as webdav_client:
        # Recursively list all files
        files = await recursive_listdir_webdav(webdav_client, webdav_path)

        for file_info in files:
            full_path = file_info["path"]  # This is the absolute WebDAV path

            if not passes_extension_prefilter(full_path, allowed_extensions):
                logger.info(f"skipping {full_path}")
                continue

            server_etag = file_info.get("etag")
            modified = file_info.get("modified")
            server_content_type = file_info.get("content_type")
            etag_source = "listing"
            if not server_etag:
                etag_source = "HEAD"
                try:
                    resp = await webdav_client.head(full_path)
                    server_etag = resp.headers.get("etag")
                    if not modified:
                        modified = resp.headers.get("last-modified")
                    if not server_content_type:
                        server_content_type = resp.headers.get("content-type")
                except Exception:
                    logger.debug("Could not HEAD %s", full_path, exc_info=True)

            # Provisional type from the server header (else extension).
            # Indeterminate types (octet-stream) are deferred to do_ingest,
            # which sniffs the downloaded content; positively-identified
            # disallowed types are dropped here without downloading.
            mime_type = detect_mime_type(full_path, header_type=server_content_type)
            if mime_type != "application/octet-stream" and not extension_allowed(mime_type, allowed_extensions):
                logger.info(f"skipping {full_path} (detected {mime_type})")
                continue

            # Validator: strong ETag if present, else last-modified timestamp.
            server_token, token_source = _version_token(server_etag, modified)
            if token_source == "etag":
                via_etag += 1
            elif token_source == "modified":
                via_modified += 1
            else:
                via_none += 1

            if server_token:
                logger.debug(
                    "validator for %s via %s (%s lookup): %s",
                    full_path,
                    token_source,
                    etag_source,
                    server_token,
                )
            else:
                logger.info(
                    "no etag or last-modified for %s (checked %s) -- will re-download every run",
                    full_path,
                    etag_source,
                )

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

            if server_token and cached_entry and cached_entry.get("etag") == server_token:
                sha256_hash = cached_entry["sha256"]
                cache_hits += 1
                logger.debug("cache HIT for %s (validator=%s via %s)", relative_path, server_token, token_source)
            else:
                # Cache miss — defer download to write step
                sha256_hash = None
                etag_for_rec = server_token
                cache_misses += 1
                if not server_token:
                    miss_reason = "no etag or last-modified from server"
                elif not cached_entry:
                    miss_reason = "not in local state (first sight)"
                else:
                    miss_reason = f"validator changed (cached={cached_entry.get('etag')!r}, server={server_token!r})"
                logger.debug("cache MISS for %s: %s", relative_path, miss_reason)

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

    logger.info(
        "build_config: %d files; cache hits=%d misses=%d; validators: %d via etag, %d via last-modified, %d none",
        len(config),
        cache_hits,
        cache_misses,
        via_etag,
        via_modified,
        via_none,
    )
    return config


# Failures that mean the server or the network is unusable, rather than one
# directory being unreadable. These propagate; anything else degrades to
# partial results for that subtree.
_LISTING_FATAL = (TimeoutError, ConnectionError, aiohttp.ClientError, ResourceNotFound)


async def recursive_listdir_webdav(
    webdav_client: AsyncWebDAVClient,
    path: str,
    semaphore: asyncio.Semaphore | None = None,
) -> list[dict]:
    """
    Recursively list files in a WebDAV directory.

    Sibling directories are listed concurrently, bounded by
    ``webdav_max_concurrent_requests``. The upstream server only honours
    ``Depth: 1``, so the number of PROPFINDs is fixed at one per directory;
    overlapping them is what removes the round-trip-per-directory wait.

    Args:
        webdav_client: Async WebDAV client instance
        path: Directory path to list
        semaphore: Shared request limiter; created on the first call and
            passed down so the whole walk shares one budget.

    Returns:
        List of file info dictionaries with 'path' and 'size'
    """
    semaphore = semaphore or _listing_semaphore()
    file_list: list[dict] = []
    subdirs: list[str] = []

    logger.debug(f"Listing WebDAV directory: {path}")

    try:
        # Held for this PROPFIND only. Keeping it across the recursive gather
        # below would deadlock as soon as the tree is deeper than the limit.
        async with semaphore:
            resources = await webdav_client.ls(path, detail=True)
        for resource in resources:
            rel_name = resource["name"]
            logger.debug(f"Found resource: {rel_name}, type: {resource.get('type', 'unknown')}")

            basename = rel_name.rstrip("/").split("/")[-1]
            if not basename or basename == "_data":
                continue

            full_resource_path = f"{path.rstrip('/')}/{rel_name.lstrip('/')}"

            if resource["type"] == "directory":
                subdirs.append(full_resource_path)
            else:
                rec = {"path": full_resource_path, "size": resource.get("content_length", 0)}
                if "etag" in resource:
                    rec["etag"] = resource["etag"]
                for key in [x for x in resource.keys() if x not in ["href", "etag", "type", "name"]]:
                    rec[key] = resource.get(key)
                file_list.append(rec)
    except _LISTING_FATAL:
        logger.exception(f"Connection error listing {path}")
        raise
    except Exception:
        logger.error(
            "Error listing WebDAV directory %s, returning partial results",
            path,
            exc_info=True,
        )
        return file_list

    if not subdirs:
        return file_list

    results = await asyncio.gather(
        *(recursive_listdir_webdav(webdav_client, sub, semaphore) for sub in subdirs),
        return_exceptions=True,
    )
    # Preserve the two-tier contract: a connection-class failure anywhere in
    # the tree aborts the walk, while any other error leaves that subtree out
    # and keeps the rest. Results stay in directory order regardless.
    for sub, result in zip(subdirs, results, strict=True):
        if isinstance(result, _LISTING_FATAL):
            raise result
        if isinstance(result, BaseException):
            logger.error(
                "Error listing WebDAV subtree %s, returning partial results",
                sub,
                exc_info=result,
            )
            continue
        file_list.extend(result)

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
    async with _optional_shared_client(webdav_url, webdav_username, webdav_password) as webdav_client:
        return await _load_inventory(
            path=path,
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
            client=webdav_client,
        )


async def _load_inventory(
    *,
    path: str,
    source: str,
    start: int,
    end: int | None,
    skip_invalid: bool,
    webdav_url: str | None,
    webdav_username: str | None,
    webdav_password: str | None,
    config: list[dict] | None,
    extra_metadata: dict[str, str] | None,
    delete_stale: bool,
    client: AsyncWebDAVClient | None,
):
    """Body of :func:`load_inventory`, with the run's client already open.

    *client* is ``None`` when no WebDAV URL is configured; callees then fall
    back to creating their own, exactly as before.
    """
    if config is None:
        config = await build_config(path, webdav_url, webdav_username, webdav_password, source=source, client=client)
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
    not_found = []
    ret = {
        "inventory": config,
        "to_process": to_process,
        "ingested": ingested,
        "errors": errors,
        "not_found": not_found,
    }
    semaphore = _listing_semaphore()

    async def _fetch_one(idx: int, row: dict):
        """Download and write one row, bounded by the shared request budget."""
        async with semaphore:
            uri = row["path"]
            meta = _doc_meta(row, extra_metadata)
            logger.info(f"writing {uri} {idx + 1}/{len(to_process)}")
            # Provisional type from discovery; do_ingest resolves the final
            # type from the GET Content-Type header and content sniffing.
            mime_type = (row.get("metadata") or {}).get("content-type")
            return await do_ingest(
                base_path,
                uri,
                meta,
                source,
                mime_type,
                webdav_url,
                webdav_username,
                webdav_password,
                etag=row.get("_etag"),
                client=client,
            )

    outcomes = await asyncio.gather(
        *(_fetch_one(idx, row) for idx, row in enumerate(to_process)),
        return_exceptions=True,
    )

    # Folded back in inventory order, not completion order, so the result
    # lists are identical to the sequential version for the same inputs --
    # and so every failure reaches `errors`, which gates delete_stale below.
    for row, res in zip(to_process, outcomes, strict=True):
        uri = row["path"]
        if isinstance(res, BaseException):
            logger.error("Failed to write %s", uri, exc_info=res)
            errors.append({"uri": uri, "error": str(res)})
        elif "error" in res:
            logger.error(f"Error writing {uri}: {res['error']}")
            errors.append({"uri": uri, "error": res["error"]})
        elif res.get("not_found"):
            # Definitive removal, not a blocking error: excluded from the
            # reconcile's "should exist" set below so its local copy is
            # deleted (when delete_stale is on).
            not_found.append(uri)
        elif res.get("skipped"):
            logger.info("skipping %s: %s", uri, res["skipped"])
        else:
            ingested.append(uri)

    delete_stale_result = None
    if delete_stale and len(errors) == 0:
        current = {r["path"] for r in config} - set(not_found)
        delete_stale_result = await local_state.reconcile_documents(source, current)
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
    client: AsyncWebDAVClient | None = None,
):
    """
    Read a file from WebDAV (or local filesystem) and write it locally.

    Args:
        base_path: Base directory or WebDAV path
        uri: Relative file path
        meta: File metadata for the sidecar
        source: Source identifier
        mime_type: Provisional MIME type from discovery (may be ``None``);
            the final type is resolved from the GET ``Content-Type`` header
            and content sniffing.
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password
        etag: Server ETag to record in local state, if known
        client: Existing client to reuse; one is created and closed here when
            omitted. Reusing the caller's client keeps the connection pool
            warm across a run instead of paying a TLS handshake per file.

    Returns:
        Result dictionary with success/error information (or a ``skipped``
        reason when the resolved content type is not allowed).
    """
    logger.info(f"base_path={base_path}, uri={uri}")

    header_type = None
    source_url = None
    # Check if base_path is a local directory
    if base_path and Path(base_path).exists():
        load_path = Path(base_path) / uri
        logger.debug(f"Loading from local path: {load_path}")
        async with aiofiles.open(load_path, "rb") as f:
            doc_body = await f.read()
    else:
        full_path = f"{base_path.rstrip('/')}/{uri.lstrip('/')}" if base_path else uri
        if webdav_url:
            source_url = f"{webdav_url.rstrip('/')}/{full_path.lstrip('/')}"
        try:
            async with _client_for(client, webdav_url, webdav_username, webdav_password) as webdav_client:
                logger.info(f"Downloading from WebDAV: {full_path}")
                doc_body, header_type = await webdav_client.download(full_path)

                # Capture a validator (ETag, else Last-Modified) via HEAD if
                # the caller didn't already supply one from the listing step.
                # Same client as the GET above, so this costs a round trip,
                # not a connection.
                if not etag and webdav_url:
                    try:
                        head_path = full_path if base_path else uri
                        resp = await webdav_client.head(head_path)
                        etag, token_source = _version_token(
                            resp.headers.get("etag"),
                            resp.headers.get("last-modified"),
                        )
                        logger.debug("do_ingest HEAD validator for %s via %s: %s", uri, token_source, etag)
                    except Exception:
                        logger.debug("Could not get validator via HEAD for %s", uri, exc_info=True)
        except ResourceNotFound:
            # 404 is a definitive "gone" signal (not a transient failure), so
            # report it separately -- the caller treats it as a removal when
            # delete_stale is enabled rather than a blocking error.
            logger.info("source file gone (404): %s", uri)
            return {"not_found": True, "uri": uri}
        except Exception as e:
            logger.exception(f"Error downloading {uri} from WebDAV")
            return {"error": str(e)}

    # Resolve the final type: server GET header wins, then content sniffing,
    # then the filename extension. WebDAV relies on the server's mime type,
    # so no plain-text (.txt) fallback is applied. The provisional type from
    # discovery (e.g. a PROPFIND getcontenttype) is used only when nothing
    # else identifies the content.
    resolved = detect_mime_type(uri, data=doc_body, header_type=header_type)
    if resolved == "application/octet-stream" and mime_type:
        resolved = mime_type
    mime_type = resolved
    if not extension_allowed(mime_type, settings.extensions):
        reason = f"content type {mime_type} not allowed"
        logger.info("skipping %s: %s", uri, reason)
        return {"skipped": reason, "uri": uri}

    sha256_hash = hashlib.sha256(doc_body, usedforsecurity=False).hexdigest()
    await local_store.write_document(source, uri, doc_body, mime_type, meta, ingestion_type="webdav", source_url=source_url)
    if etag:
        logger.debug("recording %s in local state (validator=%s)", uri, etag)
    else:
        logger.info("recording %s WITHOUT a validator -- it will re-download next run", uri)
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
    async with _optional_shared_client(webdav_url, webdav_username, webdav_password) as webdav_client:
        config, url_results = await build_config_from_urls(
            urls_file,
            webdav_url,
            webdav_username,
            webdav_password,
            base_dir=base_dir,
            source=source,
            client=webdav_client,
        )

        result = await _load_inventory(
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
            client=webdav_client,
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
