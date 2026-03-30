"""Tests for soliplex.agents.client module."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import aiohttp
import pytest

from soliplex.agents import client
from soliplex.agents.scm import UnexpectedResponseError


@pytest.mark.asyncio
async def test_get_session():
    """Test get_session creates session with correct headers."""
    async with client.get_session() as session:
        assert isinstance(session, client._RetrySession)
        assert session._session.headers["User-Agent"] == "soliplex-agent"


@pytest.mark.asyncio
async def test_get_session_with_api_key():
    """Test get_session includes Bearer token when INGESTER_API_KEY is set."""
    with patch("soliplex.agents.client.settings") as mock_settings:
        mock_settings.ingester_api_key = "your-api-key"

        async with client.get_session() as session:
            assert isinstance(session, client._RetrySession)
            assert session._session.headers["User-Agent"] == "soliplex-agent"
            assert session._session.headers["Authorization"] == "Bearer your-api-key"


@pytest.mark.asyncio
async def test_get_session_without_api_key():
    """Test get_session does not include Authorization header when INGESTER_API_KEY is not set."""
    with patch("soliplex.agents.client.settings") as mock_settings:
        mock_settings.ingester_api_key = None

        async with client.get_session() as session:
            assert isinstance(session, client._RetrySession)
            assert session._session.headers["User-Agent"] == "soliplex-agent"
            assert "Authorization" not in session._session.headers


def test_build_url():
    """Test _build_url constructs correct URL."""
    url = client._build_url("/test/path")
    assert "/api/v1/test/path" in url


@pytest.mark.asyncio
async def test_post_request_success(mock_response):
    """Test _post_request with successful response."""
    from tests.unit.conftest import create_async_context_manager

    response_data = {"batch_id": 123}
    mock_resp = mock_response(201, response_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        form_data = aiohttp.FormData()
        form_data.add_field("test", "value")

        result = await client._post_request("/test", form_data, expected_status=201)

        assert result == response_data
        mock_session.post.assert_called_once()


@pytest.mark.asyncio
async def test_post_request_error_in_response(mock_response):
    """Test _post_request raises ValueError when response contains error."""
    from tests.unit.conftest import create_async_context_manager

    response_data = {"error": "Something went wrong"}
    mock_resp = mock_response(400, response_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        form_data = aiohttp.FormData()

        with pytest.raises(ValueError, match="Something went wrong"):
            await client._post_request("/test", form_data)


@pytest.mark.asyncio
async def test_post_request_unexpected_status(mock_response):
    """Test _post_request raises UnexpectedResponseError for wrong status."""
    from tests.unit.conftest import create_async_context_manager

    response_data = {"message": "unexpected"}
    mock_resp = mock_response(500, response_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        form_data = aiohttp.FormData()

        with pytest.raises(UnexpectedResponseError):
            await client._post_request("/test", form_data, expected_status=201)


@pytest.mark.asyncio
async def test_create_batch():
    """Test create_batch returns batch ID."""
    with patch("soliplex.agents.client._post_request") as mock_post:
        mock_post.return_value = {"batch_id": 456}

        batch_id = await client.create_batch("test_source", "test_batch")

        assert batch_id == 456
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "/batch/"


@pytest.mark.asyncio
async def test_start_workflows_for_batch_minimal():
    """Test start_workflows_for_batch with minimal parameters. Should raise ValueError."""
    with pytest.raises(ValueError, match="start_workflows requires both workflow_definition_id and param_set_id"):
        await client.start_workflows_for_batch(batch_id=123, workflow_definition_id=None, param_id=None, priority=5)


@pytest.mark.asyncio
async def test_start_workflows_for_batch_with_optional_params():
    """Test start_workflows_for_batch with all parameters."""
    with patch("soliplex.agents.client._post_request") as mock_post:
        mock_post.return_value = {"status": "started"}

        result = await client.start_workflows_for_batch(
            batch_id=123, workflow_definition_id="wf_123", param_id="param_456", priority=10
        )

        assert result == {"status": "started"}


@pytest.mark.asyncio
async def test_start_workflows_for_batch_success_with_workflows(caplog):
    """Test start_workflows_for_batch logs success when workflows key present."""
    import logging

    caplog.set_level(logging.INFO)

    with patch("soliplex.agents.client._post_request") as mock_post:
        mock_post.return_value = {"workflows": 5}

        result = await client.start_workflows_for_batch(
            batch_id=123, workflow_definition_id="wf_123", param_id="param_456", priority=10
        )

        assert result == {"workflows": 5}
        assert "Started 5 workflows for batch 123" in caplog.text


@pytest.mark.asyncio
async def test_start_workflows_for_batch_error_response(caplog):
    """Test start_workflows_for_batch logs error when error key present."""
    import logging

    caplog.set_level(logging.ERROR)

    with patch("soliplex.agents.client._post_request") as mock_post:
        mock_post.return_value = {"error": "No documents in batch"}

        result = await client.start_workflows_for_batch(
            batch_id=456, workflow_definition_id="wf_789", param_id="param_012", priority=5
        )

        assert result == {"error": "No documents in batch"}
        assert "Failed to start workflows for batch 456: No documents in batch" in caplog.text


@pytest.mark.asyncio
async def test_check_status(mock_response):
    """Test check_status returns files needing processing."""
    from tests.unit.conftest import create_async_context_manager

    file_info = [
        {"uri": "file1.md", "sha256": "hash1"},
        {"uri": "file2.md", "sha256": "hash2"},
        {"uri": "file3.md", "sha256": "hash3"},
    ]

    response_data = {
        "file1.md": {"status": "new"},
        "file2.md": {"status": "mismatch"},
        "file3.md": {"status": "processed"},
    }

    mock_resp = mock_response(200, response_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.check_status(file_info, "test_source")

        # Should only return files with 'new' or 'mismatch' status
        assert len(result) == 2
        assert result[0]["uri"] == "file1.md"
        assert result[1]["uri"] == "file2.md"

        # Verify payload includes sha256 and etag in dict format
        post_call = mock_session.post.call_args
        import json

        sent_hashes = json.loads(post_call[1]["data"]._fields[1][2])
        assert sent_hashes["file1.md"] == {"sha256": "hash1", "etag": ""}


@pytest.mark.asyncio
async def test_check_status_sends_etag(mock_response):
    """Test check_status includes ETag in payload when available."""
    from tests.unit.conftest import create_async_context_manager

    file_info = [
        {"uri": "file1.md", "sha256": None, "_etag": '"etag1"'},
    ]
    response_data = {"file1.md": {"status": "matched"}}
    mock_resp = mock_response(200, response_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.check_status(file_info, "test_source")

        assert len(result) == 0

        import json

        post_call = mock_session.post.call_args
        sent_hashes = json.loads(post_call[1]["data"]._fields[1][2])
        assert sent_hashes["file1.md"] == {"sha256": "", "etag": '"etag1"'}


@pytest.mark.asyncio
async def test_check_status_empty(mock_response):
    """Test check_status with no files needing processing."""
    from tests.unit.conftest import create_async_context_manager

    file_info = [{"uri": "file1.md", "sha256": "hash1"}]
    response_data = {"file1.md": {"status": "processed"}}

    mock_resp = mock_response(200, response_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.check_status(file_info, "test_source")

        assert len(result) == 0


@pytest.mark.asyncio
async def test_check_status_delete_stale(mock_response, caplog):
    """Test check_status with delete_stale=True unwraps nested status."""
    import logging

    from tests.unit.conftest import create_async_context_manager

    caplog.set_level(logging.INFO)

    file_info = [
        {"uri": "file1.md", "sha256": "hash1"},
        {"uri": "file2.md", "sha256": "hash2"},
    ]

    response_data = {
        "status": {
            "file1.md": {"status": "new"},
            "file2.md": {"status": "matched"},
        },
        "deleted_count": 3,
    }

    mock_resp = mock_response(200, response_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.check_status(file_info, "test_source", delete_stale=True)

        assert len(result) == 1
        assert result[0]["uri"] == "file1.md"
        assert "delete_stale removed 3 documents" in caplog.text


@pytest.mark.asyncio
async def test_check_status_delete_stale_no_deleted(mock_response, caplog):
    """Test check_status with delete_stale=True when deleted_count is missing."""
    import logging

    from tests.unit.conftest import create_async_context_manager

    caplog.set_level(logging.INFO)

    file_info = [{"uri": "file1.md", "sha256": "hash1"}]
    response_data = {
        "status": {"file1.md": {"status": "matched"}},
    }

    mock_resp = mock_response(200, response_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.check_status(file_info, "test_source", delete_stale=True)

        assert len(result) == 0
        assert "delete_stale removed 0 documents" in caplog.text


@pytest.mark.asyncio
async def test_do_ingest_success_with_bytes(mock_response):
    """Test do_ingest with successful ingestion using bytes."""
    from tests.unit.conftest import create_async_context_manager

    mock_resp = mock_response(201, {"document_id": 789})

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.do_ingest(
            doc_body=b"test content",
            uri="/docs/test.md",
            meta={"author": "test"},
            source="test_source",
            batch_id=123,
            mime_type="text/markdown",
        )

        assert result == {"result": "success"}


@pytest.mark.asyncio
async def test_do_ingest_success_with_string(mock_response):
    """Test do_ingest with successful ingestion using string."""
    from tests.unit.conftest import create_async_context_manager

    mock_resp = mock_response(201, {"document_id": 789})

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.do_ingest(
            doc_body="test content",
            uri="/docs/test.md",
            meta={"author": "test"},
            source="test_source",
            batch_id=123,
            mime_type="text/markdown",
        )

        assert result == {"result": "success"}


@pytest.mark.asyncio
async def test_do_ingest_failure(mock_response):
    """Test do_ingest with failed ingestion."""
    from tests.unit.conftest import create_async_context_manager

    mock_resp = mock_response(400, {"error": "Invalid document"})

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.do_ingest(
            doc_body=b"test content",
            uri="/docs/test.md",
            meta={},
            source="test_source",
            batch_id=123,
            mime_type="text/markdown",
        )

        assert result == {"error": "Invalid document"}


@pytest.mark.asyncio
async def test_do_ingest_exception():
    """Test do_ingest handles exceptions gracefully."""
    from tests.unit.conftest import create_async_context_manager

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.side_effect = Exception("Network error")
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.do_ingest(
            doc_body=b"test content",
            uri="/docs/test.md",
            meta={},
            source="test_source",
            batch_id=123,
            mime_type="text/markdown",
        )

        assert "error" in result
        assert "Network error" in result["error"]


@pytest.mark.asyncio
async def test_find_batch_for_source_found(mock_response):
    """Test find_batch_for_source returns batch ID when source is found."""
    from tests.unit.conftest import create_async_context_manager

    batches = [
        {"id": 1, "source": "github", "name": "batch1"},
        {"id": 2, "source": "gitlab", "name": "batch2"},
        {"id": 3, "source": "bitbucket", "name": "batch3"},
    ]
    mock_resp = mock_response(200, batches)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.find_batch_for_source("gitlab")

        assert result == 2
        mock_session.get.assert_called_once()


@pytest.mark.asyncio
async def test_find_batch_for_source_not_found(mock_response):
    """Test find_batch_for_source returns None when source is not found."""
    from tests.unit.conftest import create_async_context_manager

    batches = [
        {"id": 1, "source": "github", "name": "batch1"},
        {"id": 2, "source": "gitlab", "name": "batch2"},
    ]
    mock_resp = mock_response(200, batches)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.find_batch_for_source("nonexistent")

        assert result is None


@pytest.mark.asyncio
async def test_find_batch_for_source_empty_list(mock_response):
    """Test find_batch_for_source returns None when no batches exist."""
    from tests.unit.conftest import create_async_context_manager

    batches = []
    mock_resp = mock_response(200, batches)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.find_batch_for_source("github")

        assert result is None


@pytest.mark.asyncio
async def test_find_batch_for_source_multiple_found(mock_response, caplog):
    """Test find_batch_for_source returns first batch ID when multiple matches found."""
    import logging

    from tests.unit.conftest import create_async_context_manager

    caplog.set_level(logging.WARNING)

    batches = [
        {"id": 10, "source": "github", "name": "batch1"},
        {"id": 20, "source": "github", "name": "batch2"},
        {"id": 30, "source": "github", "name": "batch3"},
    ]
    mock_resp = mock_response(200, batches)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.find_batch_for_source("github")

        assert result == 10
        assert "Multiple batches found 3 for source github using first one" in caplog.text


@pytest.mark.asyncio
async def test_find_batch_for_source_http_error(mock_response):
    """Test find_batch_for_source handles HTTP errors."""
    from tests.unit.conftest import create_async_context_manager

    mock_resp = mock_response(500, {"error": "Internal server error"})
    mock_resp.raise_for_status.side_effect = aiohttp.ClientResponseError(
        request_info=MagicMock(), history=(), status=500, message="Internal Server Error"
    )

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(aiohttp.ClientResponseError):
            await client.find_batch_for_source("github")


@pytest.mark.asyncio
async def test_find_batch_for_source_url_construction(mock_response):
    """Test find_batch_for_source uses correct endpoint."""
    from tests.unit.conftest import create_async_context_manager

    batches = [{"id": 1, "source": "github", "name": "batch1"}]
    mock_resp = mock_response(200, batches)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        with patch("soliplex.agents.client._build_url") as mock_build_url:
            mock_build_url.return_value = "http://127.0.0.1:8000/api/v1/batch/"

            mock_session = MagicMock()
            mock_session.get.return_value = create_async_context_manager(mock_resp)
            mock_get_session.return_value = create_async_context_manager(mock_session)

            await client.find_batch_for_source("github")

            mock_build_url.assert_called_once_with("/batch/")
            mock_session.get.assert_called_once_with("http://127.0.0.1:8000/api/v1/batch/")


def test_constants():
    """Test module constants are defined correctly."""
    assert client.STATUS_NEW == "new"
    assert client.STATUS_MISMATCH == "mismatch"
    assert client.PROCESSABLE_STATUSES == {"new", "mismatch"}


# --- Retry / 429 tests ---


@pytest.mark.asyncio
async def test_retry_session_delegates_methods():
    """Test _RetrySession wraps get/post/put/delete as _RetryRequestContext."""
    mock_session = MagicMock()
    retry_session = client._RetrySession(mock_session)

    for method_name in ("get", "post", "put", "delete"):
        ctx = getattr(retry_session, method_name)("http://example.com")
        assert isinstance(ctx, client._RetryRequestContext)


@pytest.mark.asyncio
async def test_retry_request_context_no_retry_on_success():
    """Test _RetryRequestContext returns immediately on non-429 status."""
    mock_method = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.release = MagicMock()
    mock_method.return_value = mock_resp

    ctx = client._RetryRequestContext(mock_method, "http://example.com")
    async with ctx as response:
        assert response.status == 200

    mock_method.assert_called_once()


@pytest.mark.asyncio
async def test_retry_request_context_retries_on_429(monkeypatch):
    """Test _RetryRequestContext retries on 429 and succeeds."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    mock_method = AsyncMock()

    def make_429():
        r = MagicMock()
        r.status = 429
        r.headers = {}
        r.release = MagicMock()
        return r

    resp_200 = MagicMock()
    resp_200.status = 200
    resp_200.release = MagicMock()

    mock_method.side_effect = [make_429(), make_429(), resp_200]

    ctx = client._RetryRequestContext(mock_method, "http://example.com")
    async with ctx as response:
        assert response.status == 200

    assert mock_method.call_count == 3


@pytest.mark.asyncio
async def test_retry_request_context_exhausts_retries(monkeypatch):
    """Test _RetryRequestContext raises RateLimitError after max retries exhausted."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    mock_method = AsyncMock()

    resp_429 = MagicMock()
    resp_429.status = 429
    resp_429.headers = {}
    resp_429.release = MagicMock()

    mock_method.return_value = resp_429

    ctx = client._RetryRequestContext(mock_method, "http://example.com")
    with pytest.raises(client.RateLimitError):
        async with ctx as _response:
            pass

    assert mock_method.call_count == client.RETRY_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_retry_request_context_aexit_with_no_response():
    """Test __aexit__ handles the case where _response is None (e.g., __aenter__ never completed)."""
    mock_method = AsyncMock()
    ctx = client._RetryRequestContext(mock_method, "http://example.com")
    # Call __aexit__ directly without ever calling __aenter__
    await ctx.__aexit__(None, None, None)
    assert ctx._response is None


@pytest.mark.asyncio
async def test_retry_request_context_retries_on_timeout(monkeypatch):
    """Test _RetryRequestContext retries on asyncio.TimeoutError."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    mock_method = AsyncMock()
    resp_200 = MagicMock()
    resp_200.status = 200
    resp_200.release = MagicMock()

    mock_method.side_effect = [
        TimeoutError(),
        resp_200,
    ]

    ctx = client._RetryRequestContext(mock_method, "http://example.com")
    async with ctx as response:
        assert response.status == 200

    assert mock_method.call_count == 2


@pytest.mark.asyncio
async def test_retry_request_context_retries_on_server_disconnect(
    monkeypatch,
):
    """Test _RetryRequestContext retries on ServerDisconnectedError."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    mock_method = AsyncMock()
    resp_200 = MagicMock()
    resp_200.status = 200
    resp_200.release = MagicMock()

    mock_method.side_effect = [
        aiohttp.ServerDisconnectedError(),
        resp_200,
    ]

    ctx = client._RetryRequestContext(mock_method, "http://example.com")
    async with ctx as response:
        assert response.status == 200

    assert mock_method.call_count == 2


@pytest.mark.asyncio
async def test_retry_request_context_timeout_exhausts_retries(
    monkeypatch,
):
    """Test _RetryRequestContext raises TimeoutError after max retries."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    mock_method = AsyncMock()
    mock_method.side_effect = TimeoutError()

    ctx = client._RetryRequestContext(mock_method, "http://example.com")
    with pytest.raises(TimeoutError):
        async with ctx as _response:
            pass

    assert mock_method.call_count == client.RETRY_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_retry_request_context_retries_on_502(monkeypatch):
    """Test _RetryRequestContext retries on 502 Bad Gateway."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    mock_method = AsyncMock()

    resp_502 = MagicMock()
    resp_502.status = 502
    resp_502.headers = {}
    resp_502.release = MagicMock()

    resp_200 = MagicMock()
    resp_200.status = 200
    resp_200.release = MagicMock()

    mock_method.side_effect = [resp_502, resp_200]

    ctx = client._RetryRequestContext(mock_method, "http://example.com")
    async with ctx as response:
        assert response.status == 200

    assert mock_method.call_count == 2


@pytest.mark.asyncio
async def test_retry_request_context_retries_on_503(monkeypatch):
    """Test _RetryRequestContext retries on 503 Service Unavailable."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    mock_method = AsyncMock()

    resp_503 = MagicMock()
    resp_503.status = 503
    resp_503.headers = {}
    resp_503.release = MagicMock()

    resp_200 = MagicMock()
    resp_200.status = 200
    resp_200.release = MagicMock()

    mock_method.side_effect = [resp_503, resp_200]

    ctx = client._RetryRequestContext(mock_method, "http://example.com")
    async with ctx as response:
        assert response.status == 200

    assert mock_method.call_count == 2


@pytest.mark.asyncio
async def test_retry_request_context_429_with_retry_after_header(monkeypatch):
    """Test _RetryRequestContext reads Retry-After header on 429."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    mock_method = AsyncMock()

    resp_429 = MagicMock()
    resp_429.status = 429
    resp_429.headers = {"Retry-After": "30"}
    resp_429.release = MagicMock()

    resp_200 = MagicMock()
    resp_200.status = 200
    resp_200.release = MagicMock()

    mock_method.side_effect = [resp_429, resp_200]

    ctx = client._RetryRequestContext(mock_method, "http://example.com")
    async with ctx as response:
        assert response.status == 200

    assert mock_method.call_count == 2


@pytest.mark.asyncio
async def test_retry_request_context_5xx_exhausts_retries(monkeypatch):
    """Test _RetryRequestContext raises RetryableHTTPError after max 5xx retries."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    mock_method = AsyncMock()

    resp_500 = MagicMock()
    resp_500.status = 500
    resp_500.headers = {}
    resp_500.release = MagicMock()

    mock_method.return_value = resp_500

    ctx = client._RetryRequestContext(mock_method, "http://example.com")
    with pytest.raises(client.RetryableHTTPError):
        async with ctx as _response:
            pass

    assert mock_method.call_count == client.RETRY_MAX_ATTEMPTS


def test_retryable_exceptions_tuple():
    """Test RETRYABLE_EXCEPTIONS contains expected types."""
    from soliplex.agents.retry import RETRYABLE_EXCEPTIONS

    # RateLimitError is a subclass of RetryableHTTPError, which is in
    # the tuple -- so retry_if_exception_type will match it.
    assert issubclass(client.RateLimitError, client.RetryableHTTPError)
    assert client.RetryableHTTPError in RETRYABLE_EXCEPTIONS
    assert TimeoutError in RETRYABLE_EXCEPTIONS
    assert aiohttp.ServerDisconnectedError in RETRYABLE_EXCEPTIONS
    assert aiohttp.ClientConnectorError in RETRYABLE_EXCEPTIONS
    assert aiohttp.ClientOSError in RETRYABLE_EXCEPTIONS
    assert ConnectionResetError in RETRYABLE_EXCEPTIONS


# --- delete_source_uri tests ---


@pytest.mark.asyncio
async def test_delete_source_uri_success(mock_response):
    """Test delete_source_uri with successful deletion."""
    from tests.unit.conftest import create_async_context_manager

    response_data = {"deleted_document_uris": 1, "total_deleted": 5}
    mock_resp = mock_response(200, response_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.delete.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.delete_source_uri("/docs/test.md", "test_source")

        assert result == response_data
        mock_session.delete.assert_called_once()


@pytest.mark.asyncio
async def test_delete_source_uri_not_found(mock_response):
    """Test delete_source_uri when document is not found."""
    from tests.unit.conftest import create_async_context_manager

    mock_resp = mock_response(404, None)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.delete.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.delete_source_uri("/docs/missing.md", "test_source")

        assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_delete_source_uri_exception():
    """Test delete_source_uri handles exceptions gracefully."""
    from tests.unit.conftest import create_async_context_manager

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.delete.side_effect = Exception("Network error")
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.delete_source_uri("/docs/test.md", "test_source")

        assert "error" in result
        assert "Network error" in result["error"]


# --- check_status with delete_stale tests ---


@pytest.mark.asyncio
async def test_check_status_with_delete_stale(mock_response):
    """Test check_status passes delete_stale and handles wrapped response."""
    from tests.unit.conftest import create_async_context_manager

    file_info = [
        {"uri": "file1.md", "sha256": "hash1"},
        {"uri": "file2.md", "sha256": "hash2"},
    ]

    response_data = {
        "status": {
            "file1.md": {"status": "new"},
            "file2.md": {"status": "matched"},
        },
        "deleted_count": 3,
    }

    mock_resp = mock_response(200, response_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.check_status(file_info, "test_source", delete_stale=True)

        assert len(result) == 1
        assert result[0]["uri"] == "file1.md"


# --- find_or_create_batch ---


@pytest.mark.asyncio
async def test_find_or_create_batch_existing():
    """Test find_or_create_batch returns existing batch."""
    with patch.object(client, "find_batch_for_source", new_callable=AsyncMock, return_value=42):
        result = await client.find_or_create_batch("src")
    assert result == 42


@pytest.mark.asyncio
async def test_find_or_create_batch_creates_new():
    """Test find_or_create_batch creates batch when none exists."""
    with (
        patch.object(client, "find_batch_for_source", new_callable=AsyncMock, return_value=None),
        patch.object(client, "create_batch", new_callable=AsyncMock, return_value=99),
    ):
        result = await client.find_or_create_batch("src")
    assert result == 99


# --- find_workflow ---


@pytest.mark.asyncio
async def test_find_workflow_found(mock_response):
    """Test find_workflow returns workflow data when found."""
    from tests.unit.conftest import create_async_context_manager

    workflow_data = {"id": "wf-1", "name": "My Workflow"}
    mock_resp = mock_response(200, workflow_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.find_workflow("wf-1")

        assert result == workflow_data


@pytest.mark.asyncio
async def test_find_workflow_not_found(mock_response):
    """Test find_workflow returns None on 404."""
    from tests.unit.conftest import create_async_context_manager

    mock_resp = mock_response(404)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.find_workflow("missing-wf")

        assert result is None


# --- find_param_set ---


@pytest.mark.asyncio
async def test_find_param_set_found(mock_response):
    """Test find_param_set returns param set data when found."""
    from tests.unit.conftest import create_async_context_manager

    param_data = {"id": "ps-1", "params": {}}
    mock_resp = mock_response(200, param_data)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.find_param_set("ps-1")

        assert result == param_data


@pytest.mark.asyncio
async def test_find_param_set_not_found(mock_response):
    """Test find_param_set returns None on 404."""
    from tests.unit.conftest import create_async_context_manager

    mock_resp = mock_response(404)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await client.find_param_set("missing-ps")

        assert result is None
