"""WebDAV agent core functionality."""

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import unquote

import aiofiles
import aiohttp

from soliplex.agents import WebDAVListingError
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


async def validate_config(
    path: str,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    exclude_paths: list[str] | None = None,
):
    """
    Validate a configuration and print out validation results.

    Builds config from WebDAV directory contents and validates files.

    Args:
        path: WebDAV directory path to validate (e.g., /documents)
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password
        exclude_paths: Globs of paths under *path* to skip (see walk_webdav)

    Returns:
        None
    """
    config, listing_errors = await build_config(
        path, webdav_url, webdav_username, webdav_password, exclude_paths=exclude_paths
    )
    raise_if_incomplete(path, listing_errors)
    validated = check_config(config)
    invalid = [row for row in validated if "valid" in row and not row["valid"]]
    print(f"Validation for {path}")
    print(f"Total files: {len(config)}")
    if invalid:
        print(f"Found {len(invalid)} Invalid files:")
        for row in invalid:
            print(row["path"], row["reason"], row["metadata"]["content-type"])


async def export_urls(
    path: str,
    output_path: str,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    exclude_paths: list[str] | None = None,
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
        exclude_paths: Globs of paths under *path* to skip (see walk_webdav)

    Returns:
        None
    """
    config = await list_config(path, webdav_url, webdav_username, webdav_password, exclude_paths=exclude_paths)
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
    from soliplex.agents.common.urls_file import is_webdav_path
    from soliplex.agents.common.urls_file import read_urls_file

    allowed_extensions = settings.extensions
    cached_state = local_state.load_file_state(source) if source else {}

    lines = await read_urls_file(
        urls_file,
        base_dir,
        webdav_url=webdav_url,
        webdav_username=webdav_username,
        webdav_password=webdav_password,
        is_valid_line=is_webdav_path,
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
    webdav_path: str,
    webdav_url: str = None,
    webdav_username: str = None,
    webdav_password: str = None,
    exclude_paths: list[str] | None = None,
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
        exclude_paths: Globs of paths under *webdav_path* to skip (see walk_webdav)

    Returns:
        List of file configuration dictionaries (without sha256)
    """
    webdav_client = create_async_webdav_client(webdav_url, webdav_username, webdav_password)
    allowed_extensions = settings.extensions
    config = []

    async with webdav_client:
        files = await recursive_listdir_webdav(webdav_client, webdav_path, exclude_paths=exclude_paths)

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
    exclude_paths: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Scan a WebDAV directory and create inventory configuration.

    Uses ETag-based caching (against the per-source local state) to avoid
    re-downloading unchanged files. A subdirectory that cannot be listed is
    left out of the config and reported in the listing errors, so the caller
    can refuse to delete anything on the strength of an incomplete listing;
    a failure at the root raises (see :func:`walk_webdav`).

    Args:
        webdav_path: Path within WebDAV server (e.g., "/documents")
        webdav_url: Optional WebDAV server URL
        webdav_username: Optional WebDAV username
        webdav_password: Optional WebDAV password
        source: Source identifier used for the ETag cache lookup
        client: Existing client to reuse; one is created and closed here when
            omitted.
        exclude_paths: Globs of paths under *webdav_path* to skip without
            listing them (see :func:`walk_webdav`)

    Returns:
        Tuple of (config list, listing errors). Each listing error is a
        ``{"path", "error"}`` dict naming a subtree that could not be listed.
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
        listing = await walk_webdav(webdav_client, webdav_path, exclude_paths=exclude_paths)

        for file_info in listing.files:
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
    return config, listing.errors


# Failures that mean the server or the network is unusable. These abort the
# whole walk wherever they occur. Any failure at the walk's root aborts it
# too, whatever its type: an empty listing would otherwise reach the clean-up
# and delete every document of the source. Everything else -- the client's
# own ClientError family (401/403/404/5xx after retries, 507, a non-207
# reply) and a malformed multistatus body -- is recorded per subtree.
_LISTING_CONNECTION = (TimeoutError, ConnectionError, aiohttp.ClientError)


@dataclass
class ListingResult:
    """Outcome of :func:`walk_webdav`.

    ``files`` are the file rows listed; ``errors`` holds one
    ``{"path", "error"}`` entry per subtree that could not be listed, even
    after retrying. A non-empty ``errors`` means ``files`` is incomplete, so
    nothing may be deleted on the strength of it. ``excluded`` names the
    paths skipped on purpose by ``exclude_paths``; those are not errors.
    """

    files: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    # The exception behind each entry in ``errors``, for the final log record.
    exceptions: dict[str, BaseException] = field(default_factory=dict, repr=False, compare=False)

    def merge(self, other: "ListingResult") -> None:
        """Fold a subtree's result into this one."""
        self.files.extend(other.files)
        self.errors.extend(other.errors)
        self.excluded.extend(other.excluded)
        self.exceptions.update(other.exceptions)

    def record(self, path: str, exc: BaseException) -> None:
        """Record that the subtree at *path* could not be listed."""
        self.errors.append({"path": path, "error": f"{type(exc).__name__}: {exc}"})
        self.exceptions[path] = exc


def normalize_exclude_paths(patterns: list[str] | None) -> tuple[str, ...]:
    """Strip the slashes around each pattern; reject an empty one.

    Patterns are relative to the walk's root, so ``/HR/`` and ``HR`` mean the
    same folder.
    """
    normalized = []
    for pattern in patterns or ():
        stripped = pattern.strip().strip("/")
        if not stripped:
            raise ValueError(f"exclude_paths: empty pattern {pattern!r}")
        normalized.append(stripped)
    return tuple(normalized)


@dataclass
class _Walk:
    """State shared by every PROPFIND of one walk."""

    root: str
    semaphore: asyncio.Semaphore
    exclude_paths: tuple[str, ...] = ()

    def is_excluded(self, full_path: str) -> bool:
        """Whether *full_path* matches an ``exclude_paths`` glob.

        Matched against the percent-decoded path relative to the root, with
        :meth:`PurePosixPath.full_match` semantics: ``*`` stays within one
        path segment and ``**`` spans any number of them, so ``HR`` matches
        only the top-level folder and ``**/Private`` a folder of that name at
        any depth.
        """
        if not self.exclude_paths:
            return False
        root = self.root.strip("/")
        rel = full_path.strip("/")
        if root and rel.startswith(root + "/"):
            rel = rel[len(root) + 1 :]
        candidate = PurePosixPath(unquote(rel))
        return any(candidate.full_match(pattern) for pattern in self.exclude_paths)


async def _listdir_once(webdav_client: AsyncWebDAVClient, path: str, walk: _Walk) -> tuple[ListingResult, list[str]]:
    """List one directory: ``(its files and exclusions, subdirectory paths)``."""
    result = ListingResult()
    subdirs: list[str] = []

    logger.debug(f"Listing WebDAV directory: {path}")
    # Held for this PROPFIND only. Keeping it across the recursive gather in
    # the caller would deadlock as soon as the tree is deeper than the limit.
    async with walk.semaphore:
        resources = await webdav_client.ls(path, detail=True)
    for resource in resources:
        rel_name = resource["name"]
        logger.debug(f"Found resource: {rel_name}, type: {resource.get('type', 'unknown')}")

        basename = rel_name.rstrip("/").split("/")[-1]
        if not basename or basename == "_data":
            continue

        full_resource_path = f"{path.rstrip('/')}/{rel_name.lstrip('/')}"
        is_dir = resource["type"] == "directory"

        if walk.is_excluded(full_resource_path):
            # Skipped on purpose: a folder is never PROPFINDed, so one the
            # account cannot read raises nothing, and its documents are
            # legitimately absent from the inventory.
            logger.log(logging.INFO if is_dir else logging.DEBUG, "Excluding WebDAV path %s", full_resource_path)
            result.excluded.append(full_resource_path)
        elif is_dir:
            subdirs.append(full_resource_path)
        else:
            rec = {"path": full_resource_path, "size": resource.get("content_length", 0)}
            if "etag" in resource:
                rec["etag"] = resource["etag"]
            for key in [x for x in resource.keys() if x not in ["href", "etag", "type", "name"]]:
                rec[key] = resource.get(key)
            result.files.append(rec)
    return result, subdirs


async def _walk_subtree(webdav_client: AsyncWebDAVClient, path: str, walk: _Walk) -> ListingResult:
    """List *path* and everything below it.

    A failure listing *path* itself propagates; the caller decides whether it
    is fatal (the root) or recorded (a subtree). Failures further down are
    recorded in the result, except connection-class ones, which abort.
    """
    result, subdirs = await _listdir_once(webdav_client, path, walk)
    if not subdirs:
        return result

    outcomes = await asyncio.gather(
        *(_walk_subtree(webdav_client, sub, walk) for sub in subdirs),
        return_exceptions=True,
    )
    # Results stay in directory order regardless of completion order.
    for sub, outcome in zip(subdirs, outcomes, strict=True):
        if isinstance(outcome, _LISTING_CONNECTION):
            raise outcome
        if isinstance(outcome, BaseException):
            # A subtree 404 lands here too: a directory that vanished between
            # its parent's listing and its own is rare, and recording it costs
            # one skipped clean-up rather than risking a wrong deletion.
            result.record(sub, outcome)
            continue
        result.merge(outcome)
    return result


async def _retry_failed_subtrees(webdav_client: AsyncWebDAVClient, result: ListingResult, walk: _Walk) -> ListingResult:
    """Re-walk each failed subtree, up to ``webdav_listing_retries`` passes.

    The client already retries a 5xx per request; this second tier catches
    an outage that outlasts those retries, after a pause (doubling each
    pass). Every recorded failure is retried, 401 and 403 included: those
    are fixed upstream (a renewed credential, a restored ACL), so one may
    clear between passes. A folder that is forbidden for good belongs in
    ``exclude_paths`` instead. Subtrees that list are folded in; ones that fail again, or whose
    own descendants fail, stay in ``errors`` for the next pass. A
    connection-class failure during a retry aborts the walk, as in the first
    pass.
    """
    for attempt in range(settings.webdav_listing_retries):
        if not result.errors:
            break
        failed = [e["path"] for e in result.errors]
        delay = settings.webdav_listing_retry_delay * 2**attempt
        logger.warning(
            "Retrying %d failed WebDAV subtree(s) under %s in %.1fs (pass %d of %d)",
            len(failed),
            walk.root,
            delay,
            attempt + 1,
            settings.webdav_listing_retries,
        )
        await asyncio.sleep(delay)
        outcomes = await asyncio.gather(
            *(_walk_subtree(webdav_client, sub, walk) for sub in failed),
            return_exceptions=True,
        )
        retried = ListingResult(files=result.files, excluded=result.excluded)
        for sub, outcome in zip(failed, outcomes, strict=True):
            if isinstance(outcome, _LISTING_CONNECTION):
                raise outcome
            if isinstance(outcome, BaseException):
                retried.record(sub, outcome)
                continue
            logger.info("WebDAV subtree %s listed on retry", sub)
            retried.merge(outcome)
        result = retried
    return result


async def walk_webdav(
    webdav_client: AsyncWebDAVClient,
    path: str,
    semaphore: asyncio.Semaphore | None = None,
    *,
    exclude_paths: list[str] | None = None,
) -> ListingResult:
    """
    Recursively list files in a WebDAV directory, recording subtree failures.

    Sibling directories are listed concurrently, bounded by
    ``webdav_max_concurrent_requests``. The upstream server only honours
    ``Depth: 1``, so the number of PROPFINDs is fixed at one per directory;
    overlapping them is what removes the round-trip-per-directory wait.

    Failure handling:

    * the root cannot be listed, for any reason: raises;
    * a connection-class failure (timeout, refused, aiohttp error) anywhere:
      raises, since the server or the network is unusable;
    * any other failure listing a subdirectory (401/403/404/5xx, a malformed
      body, ...): the rest of the tree is still listed, then that subtree is
      retried (``webdav_listing_retries`` passes); one that still fails is
      left out and recorded in ``.errors``.

    Args:
        webdav_client: Async WebDAV client instance
        path: Directory path to list
        semaphore: Shared request limiter; created here when omitted, and
            shared by the whole walk.
        exclude_paths: Globs, relative to *path*, of folders (or files) to
            skip without listing them -- e.g. a folder the account may never
            read. See :meth:`_Walk.is_excluded` for the matching rules.

    Returns:
        A :class:`ListingResult`; file rows carry 'path' and 'size'.
    """
    walk = _Walk(path, semaphore or _listing_semaphore(), normalize_exclude_paths(exclude_paths))
    try:
        result = await _walk_subtree(webdav_client, path, walk)
        result = await _retry_failed_subtrees(webdav_client, result, walk)
    except Exception:
        logger.exception("Error listing WebDAV directory %s", path)
        raise
    if result.excluded:
        logger.info("WebDAV listing of %s: %d path(s) excluded by exclude_paths", path, len(result.excluded))
    if result.errors:
        for err in result.errors:
            logger.error("Error listing WebDAV subtree %s", err["path"], exc_info=result.exceptions[err["path"]])
        logger.error(
            "WebDAV listing of %s incomplete: %d subtree(s) failed; stale removal will be skipped",
            path,
            len(result.errors),
        )
    return result


async def recursive_listdir_webdav(
    webdav_client: AsyncWebDAVClient,
    path: str,
    semaphore: asyncio.Semaphore | None = None,
    *,
    exclude_paths: list[str] | None = None,
) -> list[dict]:
    """
    Recursively list files in a WebDAV directory, strictly.

    As :func:`walk_webdav`, but any subtree failure raises
    :class:`~soliplex.agents.WebDAVListingError` instead of returning partial
    results, for callers whose output must be complete (exports, validation).

    Returns:
        List of file info dictionaries with 'path' and 'size'
    """
    result = await walk_webdav(webdav_client, path, semaphore, exclude_paths=exclude_paths)
    raise_if_incomplete(path, result.errors)
    return result.files


def raise_if_incomplete(path: str, listing_errors: list[dict]) -> None:
    """Raise :class:`~soliplex.agents.WebDAVListingError` if any subtree failed to list."""
    if listing_errors:
        raise WebDAVListingError(path, listing_errors)


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
    exclude_paths: list[str] | None = None,
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
        exclude_paths: Globs of paths under *path* to skip during discovery
            (see :func:`walk_webdav`); their documents count as removed

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
            exclude_paths=exclude_paths,
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
    exclude_paths: list[str] | None = None,
    discovery_errors: list[dict] | None = None,
):
    """Body of :func:`load_inventory`, with the run's client already open.

    *client* is ``None`` when no WebDAV URL is configured; callees then fall
    back to creating their own, exactly as before. *discovery_errors* are
    error rows (``uri`` / ``error`` / ``stage``) found by a caller that built
    *config* itself -- URIs it meant to include but could not -- and are
    reported in ``errors`` like a failed download.
    """
    listing_errors: list[dict] = []
    if config is None:
        config, listing_errors = await build_config(
            path,
            webdav_url,
            webdav_username,
            webdav_password,
            source=source,
            client=client,
            exclude_paths=exclude_paths,
        )
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
    # A subtree that could not be listed is an error like a failed download:
    # its documents are absent from `config`, so the clean-up below (and the
    # runner's) must not treat them as removed. The listed files are still
    # fetched.
    errors = [{"uri": e["path"], "error": e["error"], "stage": "listing"} for e in listing_errors]
    errors.extend(discovery_errors or [])
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
            logger.error("Error writing %s: %s", uri, res["error"])
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
    Download a file from WebDAV and write it locally.

    Args:
        base_path: Base WebDAV path that *uri* is relative to (may be empty
            when *uri* is already an absolute WebDAV path)
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

    source_url = None
    full_path = f"{base_path.rstrip('/')}/{uri.lstrip('/')}" if base_path else uri
    if webdav_url:
        source_url = f"{webdav_url.rstrip('/')}/{full_path.lstrip('/')}"
    try:
        async with _client_for(client, webdav_url, webdav_username, webdav_password) as webdav_client:
            logger.info(f"Downloading from WebDAV: {full_path}")
            doc_body, header_type = await webdav_client.download(full_path)

            # Capture a validator (ETag, else Last-Modified) via HEAD if the
            # caller didn't already supply one from the listing step. Same
            # client as the GET above, so this costs a round trip, not a
            # connection.
            if not etag and webdav_url:
                try:
                    resp = await webdav_client.head(full_path)
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
        logger.exception("Error downloading %s from WebDAV", uri)
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

    A URL whose probe failed (see :func:`build_config_from_urls`) is absent
    from the inventory, so it is reported in ``errors`` with ``stage:
    "probe"``: otherwise the stale clean-up -- the runner's or this
    function's -- would read its absence as a removal and delete its
    document. The URLs that probed successfully are still fetched.

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
        probe_errors = [
            {"uri": r["url"], "error": r["error_message"], "stage": "probe"} for r in url_results if r["status"] == "error"
        ]

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
            discovery_errors=probe_errors,
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
    exclude_paths: list[str] | None = None,
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
        exclude_paths: Globs of paths under *config_path* to skip (see walk_webdav)
    """

    print(f"checking status for {config_path} source={source} ")
    config, listing_errors = await build_config(
        config_path, webdav_url, webdav_username, webdav_password, source=source, exclude_paths=exclude_paths
    )
    raise_if_incomplete(config_path, listing_errors)
    to_process = local_state.compute_to_process(config, source)
    print(f"Files to process: {len(to_process)}")
    print(f"Total files: {len(config)}")
    if detail and len(to_process) > 0:
        for row in to_process:
            print(row)
