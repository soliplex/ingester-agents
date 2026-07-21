"""Web agent for fetching and ingesting HTML pages."""

import hashlib
import logging

import aiohttp
from tenacity import AsyncRetrying

from soliplex.agents import local_state
from soliplex.agents import local_store
from soliplex.agents.config import settings
from soliplex.agents.retry import RETRYABLE_STATUS_CODES
from soliplex.agents.retry import RetryableHTTPError
from soliplex.agents.retry import parse_retry_after
from soliplex.agents.retry import retry_policy

logger = logging.getLogger(__name__)

# Retry settings for web fetches
_WEB_RETRY_MAX_ATTEMPTS = 5
_WEB_RETRY_MAX_DELAY = 120


async def fetch_url(url: str) -> tuple[bytes, str]:
    """Fetch URL content with retry on transient errors.

    Args:
        url: The URL to fetch.

    Returns:
        Tuple of (content_bytes, content_type).

    Raises:
        aiohttp.ClientResponseError: If the response status indicates a non-retryable error.
        RetryableHTTPError: If retries are exhausted on 429/5xx.
    """
    timeout = aiohttp.ClientTimeout(total=settings.http_timeout_total)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async for attempt in AsyncRetrying(
            **retry_policy(_WEB_RETRY_MAX_ATTEMPTS, _WEB_RETRY_MAX_DELAY),
        ):
            with attempt:
                async with session.get(url) as response:
                    if response.status in RETRYABLE_STATUS_CODES:
                        ra = parse_retry_after(response.headers)
                        raise RetryableHTTPError(response.status, retry_after=ra)
                    response.raise_for_status()
                    content_bytes = await response.read()
                    content_type = response.content_type or "text/html"
                    return content_bytes, content_type


async def resolve_urls(
    url: str | None = None,
    urls: list[str] | None = None,
    urls_file: str | None = None,
    base_dir: str | None = None,
) -> list[str]:
    """Flatten WebComponent source fields to a URL list.

    Args:
        url: Single URL.
        urls: List of URLs.
        urls_file: Path or S3 URL to file with one URL per line.
        base_dir: Optional directory for resolving relative local paths.

    Returns:
        List of resolved URLs.
    """
    if url is not None:
        return [url]
    if urls is not None:
        return list(urls)
    if urls_file is not None:
        from soliplex.agents.common.urls_file import read_urls_file

        return await read_urls_file(urls_file, base_dir)
    return []


async def load_inventory(
    urls: list[str],
    source: str,
    extra_metadata: dict[str, str] | None = None,
    delete_stale: bool = False,
) -> dict:
    """Fetch URLs and write their content to the download directory.

    Args:
        urls: List of URLs to fetch and write.
        source: Source identifier (becomes the per-source download folder).
        extra_metadata: Extra metadata to attach to all documents.
        delete_stale: Remove documents no longer present in *urls*.

    Returns:
        Dictionary with inventory, to_process, ingested, errors and
        delete_stale_result.
    """
    # Fetch each URL up front so change detection can use content hashes.
    file_info = []
    fetched = {}
    for url in urls:
        try:
            content_bytes, content_type = await fetch_url(url)
            sha256 = hashlib.sha256(content_bytes, usedforsecurity=False).hexdigest()
            file_info.append(
                {
                    "path": url,
                    "sha256": sha256,
                    "metadata": {"content-type": content_type},
                }
            )
            fetched[url] = (content_bytes, content_type)
        except Exception as e:
            logger.exception(f"Error fetching {url}")
            file_info.append(
                {
                    "path": url,
                    "sha256": "",
                    "metadata": {"content-type": "text/html", "error": str(e)},
                }
            )

    to_process = local_state.compute_to_process(file_info, source)
    ingested = []
    errors = []
    ret = {"inventory": file_info, "to_process": to_process, "ingested": ingested, "errors": errors}

    for row in to_process:
        url = row["path"]
        if url not in fetched:
            errors.append({"uri": url, "error": "fetch failed"})
            continue
        content_bytes, content_type = fetched[url]
        meta = dict(row.get("metadata") or {})
        for k in ("path", "sha256", "size", "source", "batch_id", "source_uri", "content-type"):
            meta.pop(k, None)
        if extra_metadata:
            meta.update(extra_metadata)
        try:
            local_store.write_document(source, url, content_bytes, content_type, meta, ingestion_type="web", source_url=url)
            local_state.upsert_file(source, url, row.get("sha256"), size=len(content_bytes), mime_type=content_type)
            ingested.append(url)
        except Exception as e:
            logger.exception(f"Error writing {url}")
            errors.append({"uri": url, "error": str(e)})

    delete_stale_result = None
    if delete_stale and len(errors) == 0:
        delete_stale_result = local_state.prune_documents(source, {r["path"] for r in file_info})
    ret["delete_stale_result"] = delete_stale_result
    return ret
