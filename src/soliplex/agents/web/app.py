"""Web agent for fetching and ingesting HTML pages."""

import hashlib
import logging

import aiohttp

from soliplex.agents import client
from soliplex.agents.config import settings

logger = logging.getLogger(__name__)


async def fetch_url(url: str) -> tuple[bytes, str]:
    """Fetch URL content.

    Args:
        url: The URL to fetch.

    Returns:
        Tuple of (content_bytes, content_type).

    Raises:
        aiohttp.ClientResponseError: If the response status indicates an error.
    """
    timeout = aiohttp.ClientTimeout(total=settings.http_timeout_total)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            content_bytes = await response.read()
            content_type = response.content_type or "text/html"
            return content_bytes, content_type


async def resolve_urls(
    url: str | None = None,
    urls: list[str] | None = None,
    urls_file: str | None = None,
) -> list[str]:
    """Flatten WebComponent source fields to a URL list.

    Args:
        url: Single URL.
        urls: List of URLs.
        urls_file: Path to file with one URL per line.

    Returns:
        List of resolved URLs.
    """
    if url is not None:
        return [url]
    if urls is not None:
        return list(urls)
    if urls_file is not None:
        import aiofiles

        async with aiofiles.open(urls_file) as f:
            content = await f.read()
        return [line.strip() for line in content.splitlines() if line.strip()]
    return []


async def load_inventory(
    urls: list[str],
    source: str,
    start_workflows: bool = False,
    workflow_definition_id: str | None = None,
    param_set_id: str | None = None,
    priority: int = 0,
    extra_metadata: dict[str, str] | None = None,
) -> dict:
    """Fetch URLs and ingest their content.

    Args:
        urls: List of URLs to fetch and ingest.
        source: Source identifier for the batch.
        start_workflows: Whether to start workflows after ingestion.
        workflow_definition_id: Optional workflow definition ID.
        param_set_id: Optional parameter set ID.
        priority: Workflow priority.
        extra_metadata: Extra metadata to attach to all documents.

    Returns:
        Dictionary with inventory, to_process, batch_id, ingested, errors, workflow_result.
    """
    client.validate_parameters(start_workflows, workflow_definition_id, param_set_id)

    # Build file info for status check
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

    to_process = await client.check_status(file_info, source)
    ret = {"inventory": file_info, "to_process": to_process}

    if len(to_process) == 0:
        logger.info("nothing to process. exiting")
        return ret

    found_batch_id = await client.find_batch_for_source(source)
    if found_batch_id:
        batch_id = found_batch_id
    else:
        batch_id = await client.create_batch(source, source)
    ret["batch_id"] = batch_id

    ingested = []
    errors = []
    for row in to_process:
        url = row["path"]
        if url not in fetched:
            errors.append({"uri": url, "error": "fetch failed"})
            continue
        content_bytes, content_type = fetched[url]
        meta = row.get("metadata", {}).copy()
        for k in ["path", "sha256", "size", "source", "batch_id", "source_uri"]:
            meta.pop(k, None)
        if extra_metadata:
            meta.update(extra_metadata)
        res = await client.do_ingest(
            content_bytes,
            url,
            meta,
            source,
            batch_id,
            content_type,
        )
        if "error" in res:
            logger.error(f"Error ingesting {url}: {res['error']}")
            errors.append({"uri": url, "error": res["error"]})
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
