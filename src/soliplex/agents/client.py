import datetime
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
from tenacity import AsyncRetrying

from .config import settings
from .retry import RETRYABLE_STATUS_CODES
from .retry import RetryableHTTPError
from .retry import parse_retry_after
from .retry import retry_policy
from .scm import UnexpectedResponseError

"""
Client for interacting with Soliplex Ingester API.
"""
logger = logging.getLogger(__name__)

# Constants
STATUS_NEW = "new"
STATUS_MISMATCH = "mismatch"
PROCESSABLE_STATUSES = {STATUS_NEW, STATUS_MISMATCH}

# Retry settings for transient errors (429, timeouts, 5xx)
RETRY_MAX_ATTEMPTS = 5
RETRY_MAX_DELAY = 120


class RateLimitError(RetryableHTTPError):
    """Raised when the server returns 429 Too Many Requests."""

    def __init__(self, msg: str = "", retry_after: float | None = None) -> None:
        super().__init__(status=429, retry_after=retry_after, body=msg)


def validate_parameters(start_workflows: bool, workflow_definition_id: str | None, param_set_id: str | None) -> None:
    if start_workflows and (workflow_definition_id is None or param_set_id is None):
        raise ValueError("start_workflows requires both workflow_definition_id and param_set_id")


class _RetrySession:
    """Wraps an aiohttp.ClientSession to retry on 429 Too Many Requests with exponential backoff."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    def get(self, url, **kwargs):
        return _RetryRequestContext(self._session.get, url, **kwargs)

    def post(self, url, **kwargs):
        return _RetryRequestContext(self._session.post, url, **kwargs)

    def put(self, url, **kwargs):
        return _RetryRequestContext(self._session.put, url, **kwargs)

    def delete(self, url, **kwargs):
        return _RetryRequestContext(self._session.delete, url, **kwargs)


class _RetryRequestContext:
    """Async context manager that retries the request on 429 responses using tenacity."""

    def __init__(self, method, url, **kwargs) -> None:
        self._method = method
        self._url = url
        self._kwargs = kwargs
        self._response: aiohttp.ClientResponse | None = None

    async def __aenter__(self) -> aiohttp.ClientResponse:
        async for attempt in AsyncRetrying(
            **retry_policy(RETRY_MAX_ATTEMPTS, RETRY_MAX_DELAY),
        ):
            with attempt:
                self._response = await self._method(self._url, **self._kwargs)
                status = self._response.status
                if status == 429:
                    ra = parse_retry_after(self._response.headers)
                    logger.warning(
                        "Rate limited (429) on %s (attempt %d/%d)",
                        self._url,
                        attempt.retry_state.attempt_number,
                        RETRY_MAX_ATTEMPTS,
                    )
                    self._response.release()
                    raise RateLimitError(
                        f"429 Too Many Requests on {self._url}",
                        retry_after=ra,
                    )
                if status in RETRYABLE_STATUS_CODES and status != 429:
                    ra = parse_retry_after(self._response.headers)
                    logger.warning(
                        "Retryable %d on %s (attempt %d/%d)",
                        status,
                        self._url,
                        attempt.retry_state.attempt_number,
                        RETRY_MAX_ATTEMPTS,
                    )
                    self._response.release()
                    raise RetryableHTTPError(status, retry_after=ra)
        return self._response  # type: ignore[return-value]

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._response is not None:
            self._response.release()


@asynccontextmanager
async def get_session():
    headers = {"User-Agent": "soliplex-agent"}
    if settings.ingester_api_key:
        headers["Authorization"] = f"Bearer {settings.ingester_api_key}"
    async with aiohttp.ClientSession(headers=headers) as session:
        yield _RetrySession(session)


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


async def find_param_set(param_id: str) -> str | None:
    """
    Find the parameter set with the given ID.

    Args:
        param_id (str): ID of the parameter set to find.

    Returns:
        str | None: The parameter set YAML string if found, None otherwise.
    """
    url = _build_url(f"/workflow/param-sets/{param_id}")
    async with get_session() as session:
        async with session.get(url) as response:
            if response.status == 404:
                return None
            else:
                response.raise_for_status()
                return await response.json()


async def find_workflow(workflow_id: str) -> str | None:
    """
    Find the workflow definition with the given ID.

    Args:
        workflow_id (str): ID of the workflow to find.

    Returns:
        str | None: The workflow YAML string if found, None otherwise.
    """
    url = _build_url(f"/workflow/definitions/{workflow_id}")
    async with get_session() as session:
        async with session.get(url) as response:
            if response.status == 404:
                return None
            else:
                response.raise_for_status()
                return await response.json()


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


async def find_or_create_batch(source: str) -> int:
    """Find an existing batch for *source*, or create one.

    Args:
        source: Source identifier.

    Returns:
        Batch ID (existing or newly created).
    """
    batch_id = await find_batch_for_source(source)
    if batch_id is not None:
        return batch_id
    return await create_batch(source, source)


async def start_workflows_for_batch(
    batch_id: int,
    workflow_definition_id: str,
    param_id: str,
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
    validate_parameters(True, workflow_definition_id, param_id)
    form = aiohttp.FormData()
    form.add_field("batch_id", str(batch_id))
    form.add_field("priority", str(priority))
    form.add_field("only_unparsed", "true")

    form.add_field("param_id", param_id)
    form.add_field("workflow_definition_id", workflow_definition_id)
    logger.info(f"Starting workflows for batch {batch_id} with priority {priority} and param {param_id}")
    res = await _post_request("/batch/start-workflows", form)

    if "workflows" in res:
        logger.info(f"Started {res['workflows']} workflows for batch {batch_id}")
    elif "error" in res:
        logger.error(f"Failed to start workflows for batch {batch_id}: {res['error']}")
    return res


async def check_status(file_info: list[dict[str, Any]], source: str, delete_stale: bool = False) -> list[dict[str, Any]]:
    """
    Check which files need processing based on their status.

    When delete_stale is True, the Ingester will also delete documents
    belonging to the source whose URI is not in the submitted file_info.

    Args:
        file_info: List of file information dictionaries with 'uri' and 'sha256' keys
        source: Source identifier
        delete_stale: If True, server will delete documents not in file_info

    Returns:
        List of files that need processing (status is 'new' or 'mismatch')
    """
    # fill in uri if path provided
    for x in file_info:
        x["uri"] = x.get("path", x.get("uri"))

    status_dict = {
        x["uri"]: {
            "sha256": x.get("sha256") or "",
            "etag": x.get("_etag") or "",
        }
        for x in file_info
    }
    uri_to_file = {x["uri"]: x for x in file_info}

    url = _build_url("/source-status")
    logger.debug("url = %s", url)

    form = aiohttp.FormData()
    form.add_field("source", source)
    form.add_field("hashes", json.dumps(status_dict))
    if delete_stale:
        form.add_field("delete_stale", "true")

    to_process = []
    async with get_session() as session:
        async with session.post(url, data=form) as response:
            response.raise_for_status()
            resp = await response.json()

            # When delete_stale=True, server wraps response in {"status": ..., "deleted_count": ...}
            status_data = resp.get("status", resp) if delete_stale else resp

            for uri, row in status_data.items():
                status = row["status"]
                if status in PROCESSABLE_STATUSES:
                    logger.debug(f"need to process {uri} with status {status}")
                    to_process.append(uri_to_file[uri])
                else:
                    logger.debug(f"no need to process {uri} with status {status}")

            if delete_stale:
                deleted_count = resp.get("deleted_count", 0)
                logger.info("delete_stale removed %d documents for source %s", deleted_count, source)

    return to_process


async def delete_source_uri(uri: str, source: str) -> dict[str, Any]:
    """
    Delete a document by URI and source from the ingester.

    Args:
        uri: The document URI to delete
        source: Source identifier

    Returns:
        Deletion statistics or error dict
    """
    url = _build_url("/document/by-uri")

    form = aiohttp.FormData()
    form.add_field("uri", uri)
    form.add_field("source", source)

    try:
        async with get_session() as session:
            async with session.delete(url, data=form) as response:
                if response.status == 404:
                    logger.warning(f"Document not found for deletion: {uri} ({source})")
                    return {"status": "not_found", "uri": uri}

                response.raise_for_status()
                return await response.json()

    except Exception as e:
        logger.exception(f"Error deleting document {uri}")
        return {"error": str(e)}


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
                if resp["last_sync_date"] is not None:
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
