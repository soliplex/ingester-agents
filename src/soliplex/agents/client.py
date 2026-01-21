import datetime
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

from .config import settings
from .scm import UnexpectedResponseError

"""
Client for interacting with Soliplex Ingester API.
"""
logger = logging.getLogger(__name__)

# Constants
STATUS_NEW = "new"
STATUS_MISMATCH = "mismatch"
PROCESSABLE_STATUSES = {STATUS_NEW, STATUS_MISMATCH}


@asynccontextmanager
async def get_session():
    headers = {"User-Agent": "soliplex-agent"}
    if settings.ingester_api_key:
        headers["Authorization"] = f"Bearer {settings.ingester_api_key}"
    async with aiohttp.ClientSession(headers=headers) as session:
        yield session


def _build_url(path: str) -> str:
    """Build full URL from endpoint and path."""
    return f"{settings.endpoint_url}{path}"


async def find_batch_for_source(source: str) -> int | None:
    url = _build_url("/batch/")
    async with get_session() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            batches = await response.json()
            found = [b for b in batches if b["source"] == source]
            if len(found) == 0:
                return None
            if len(found) > 1:
                logger.warning(f"Multiple batches found {len(found)} for source {source} using first one")
            return found[0]["id"]


async def _post_request(path: str, form_data: aiohttp.FormData, expected_status: int = 201) -> dict[str, Any]:
    """
    Send POST request and handle common error patterns.

    Args:
        path: API endpoint path
        form_data: Form data to send
        expected_status: Expected HTTP status code

    Returns:
        JSON response as dictionary

    Raises:
        ValueError: If response contains error or unexpected status
    """
    url = _build_url(path)
    async with get_session() as session:
        async with session.post(url, data=form_data) as response:
            logger.debug(f"{path} response: {response.status}")
            res = await response.json()

            if "error" in res:
                raise ValueError(res["error"])
            if response.status != expected_status:
                logger.error(f"Unexpected status {response.status}: {res}")
                raise UnexpectedResponseError

            return res


async def create_batch(source: str, name: str) -> int:
    """
    Create a new batch for document ingestion.

    Args:
        source: Source identifier
        name: Batch name

    Returns:
        Created batch ID
    """
    form = aiohttp.FormData()
    form.add_field("source", source)
    form.add_field("name", name)

    res = await _post_request("/batch/", form)
    return res["batch_id"]


async def do_start_workflows(
    batch_id: int,
    workflow_definition_id: str | None,
    param_id: str | None,
    priority: int,
) -> dict[str, Any]:
    """
    Start workflows for a batch.

    Args:
        batch_id: Batch identifier
        workflow_definition_id: Optional workflow definition identifier
        param_id: Optional parameter identifier
        priority: Workflow priority level

    Returns:
        Response dictionary from the API
    """
    form = aiohttp.FormData()
    form.add_field("batch_id", str(batch_id))
    form.add_field("priority", str(priority))

    if param_id:
        form.add_field("param_id", param_id)
    if workflow_definition_id:
        form.add_field("workflow_definition_id", workflow_definition_id)

    return await _post_request("/batch/start-workflows", form)


async def check_status(file_info: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    """
    Check which files need processing based on their status.

    Args:
        file_info: List of file information dictionaries with 'uri' and 'sha256' keys
        source: Source identifier

    Returns:
        List of files that need processing (status is 'new' or 'mismatch')
    """
    # fill in uri if path provided
    for x in file_info:
        x["uri"] = x.get("path", x.get("uri"))

    status_dict = {x["uri"]: x["sha256"] for x in file_info}
    uri_to_file = {x["uri"]: x for x in file_info}

    url = _build_url("/source-status")
    logger.debug("url = %s", url)

    form = aiohttp.FormData()
    form.add_field("source", source)
    form.add_field("hashes", json.dumps(status_dict))

    to_process = []
    async with get_session() as session:
        async with session.post(url, data=form) as response:
            response.raise_for_status()
            resp = await response.json()

            for uri, row in resp.items():
                status = row["status"]
                if status in PROCESSABLE_STATUSES:
                    logger.debug(f"need to process {uri} with status {status}")
                    to_process.append(uri_to_file[uri])
                else:
                    logger.debug(f"no need to process {uri} with status {status}")

    return to_process


async def do_ingest(
    doc_body: bytes | str,
    uri: str,
    meta: dict[str, str],
    source: str,
    batch_id: int,
    mime_type: str,
) -> dict[str, Any]:
    """
    Ingest a document into the system.

    Args:
        doc_body: Document content as bytes or string
        uri: Source URI of the document
        meta: Metadata dictionary
        source: Source identifier
        batch_id: Batch identifier
        mime_type: MIME type of the document

    Returns:
        Result dictionary with success or error information
    """
    url = _build_url("/document/ingest-document")

    # Normalize URI and document body
    normalized_uri = uri.lstrip("/")
    doc_bytes = doc_body.encode("utf-8") if isinstance(doc_body, str) else doc_body

    form = aiohttp.FormData()
    form.add_field("source", source)
    form.add_field("source_uri", uri)
    form.add_field("batch_id", str(batch_id))
    form.add_field("doc_meta", json.dumps(meta))
    form.add_field("mime_type", mime_type)
    form.add_field(
        "file",
        doc_bytes,
        filename=normalized_uri.split("/")[-1],
        content_type="binary/octet-stream",
    )

    try:
        async with get_session() as session:
            async with session.post(url, data=form) as response:
                res = await response.json()
                logger.debug(f"do_ingest response: {response.status} {res}")

                if response.status != 201:
                    logger.error(f"ingest res for {uri}={res}")
                    return res

                return {"result": "success"}

    except Exception as e:
        logger.exception(f"Error ingesting {uri}")
        return {"error": f"Error ingesting {uri}: {e}"}


async def get_sync_state(source: str) -> dict[str, Any]:
    """
    Get last sync state for a source.

    Args:
        source: Source identifier (e.g., "gitea:admin:myrepo")

    Returns:
        Sync state dict with last_commit_sha, last_sync_date, etc.
    """
    url = _build_url(f"/sync-state/{source}")
    try:
        async with get_session() as session:
            async with session.get(url) as response:
                if response.status == 404:
                    # No sync state exists yet
                    return {
                        "source_id": source,
                        "last_commit_sha": None,
                        "last_sync_date": None,
                        "branch": "main",
                    }

                response.raise_for_status()
                resp = await response.json()
                resp["last_sync_date"] = datetime.datetime.fromisoformat(resp["last_sync_date"])
                return resp

    except Exception as e:
        logger.exception(f"Error getting sync state for {source}")
        return {"error": str(e)}


async def update_sync_state(
    source: str, commit_sha: str, branch: str = "main", metadata: dict | None = None
) -> dict[str, Any]:
    """
    Update sync state after successful sync.

    Args:
        source: Source identifier
        commit_sha: Latest processed commit SHA
        branch: Branch name
        metadata: Optional sync metadata

    Returns:
        Updated sync state or error dict
    """
    url = _build_url(f"/sync-state/{source}")

    form = aiohttp.FormData()
    form.add_field("commit_sha", commit_sha)
    form.add_field("branch", branch)
    if metadata:
        form.add_field("metadata", json.dumps(metadata))

    try:
        async with get_session() as session:
            async with session.put(url, data=form) as response:
                response.raise_for_status()
                return await response.json()

    except Exception as e:
        logger.exception(f"Error updating sync state for {source}")
        return {"error": str(e)}


async def reset_sync_state(source: str) -> dict[str, Any]:
    """
    Reset sync state (forces full sync).

    Args:
        source: Source identifier

    Returns:
        Confirmation message or error dict
    """
    url = _build_url(f"/sync-state/{source}")

    try:
        async with get_session() as session:
            async with session.delete(url) as response:
                if response.status == 404:
                    return {"message": f"No sync state found for {source}"}

                response.raise_for_status()
                return await response.json()

    except Exception as e:
        logger.exception(f"Error resetting sync state for {source}")
        return {"error": str(e)}
