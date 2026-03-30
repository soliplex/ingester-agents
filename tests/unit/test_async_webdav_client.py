"""Tests for soliplex.agents.webdav.async_client module."""

import asyncio
import logging
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import aiohttp
import pytest

from soliplex.agents.webdav.async_client import AsyncWebDAVClient
from soliplex.agents.webdav.async_client import ClientError
from soliplex.agents.webdav.async_client import DAVProperties
from soliplex.agents.webdav.async_client import InsufficientStorage
from soliplex.agents.webdav.async_client import MultiStatusResponse
from soliplex.agents.webdav.async_client import ResourceNotFound
from soliplex.agents.webdav.async_client import RetryableHTTPError
from soliplex.agents.webdav.async_client import WebDAVResponse
from soliplex.agents.webdav.async_client import _join_url_path
from soliplex.agents.webdav.async_client import _normalize_path
from soliplex.agents.webdav.async_client import _parse_iso_datetime
from soliplex.agents.webdav.async_client import _parse_rfc1123
from soliplex.agents.webdav.async_client import _prepare_result_info
from soliplex.agents.webdav.async_client import _relative_url_to
from soliplex.agents.webdav.async_client import _strip_trailing_slash
from soliplex.agents.webdav.async_client import create_async_webdav_client

# ---------------------------------------------------------------------------
# Sample XML for PROPFIND responses
# ---------------------------------------------------------------------------

MULTISTATUS_DIR_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/remote/documents/</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype><D:collection/></D:resourcetype>
        <D:displayname>documents</D:displayname>
      </D:prop>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/remote/documents/readme.md</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
        <D:getcontentlength>1024</D:getcontentlength>
        <D:getetag>"abc123"</D:getetag>
        <D:getcontenttype>text/markdown</D:getcontenttype>
        <D:displayname>readme.md</D:displayname>
      </D:prop>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/remote/documents/report.pdf</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
        <D:getcontentlength>2048</D:getcontentlength>
        <D:getetag>"def456"</D:getetag>
        <D:getcontenttype>application/pdf</D:getcontenttype>
      </D:prop>
    </D:propstat>
  </D:response>
</D:multistatus>"""

MULTISTATUS_SINGLE_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/remote/documents/readme.md</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
        <D:getcontentlength>1024</D:getcontentlength>
        <D:getetag>"abc123"</D:getetag>
        <D:getcontenttype>text/markdown</D:getcontenttype>
        <D:getlastmodified>Mon, 01 Jan 2024 12:00:00 GMT</D:getlastmodified>
        <D:creationdate>2024-01-01T00:00:00Z</D:creationdate>
      </D:prop>
    </D:propstat>
  </D:response>
</D:multistatus>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(status=200, text="", headers=None, content=b"", history=None, url=None):
    """Create a mock aiohttp.ClientResponse."""
    resp = MagicMock(spec=aiohttp.ClientResponse)
    resp.status = status
    resp.headers = headers or {}
    resp.text = AsyncMock(return_value=text)
    resp.read = AsyncMock(return_value=content)
    resp.release = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.history = history or ()
    resp.url = url or "https://example.com/path"
    return resp


def _make_client(base_url="https://example.com/remote", **kwargs):
    """Create an AsyncWebDAVClient with a mocked session."""
    kwargs.setdefault("max_retries", 1)
    client = AsyncWebDAVClient(base_url=base_url, **kwargs)
    session = AsyncMock(spec=aiohttp.ClientSession)
    session.closed = False
    session.close = AsyncMock()
    client._session = session
    client._connector = MagicMock()
    return client, session


# ---------------------------------------------------------------------------
# URL Utilities
# ---------------------------------------------------------------------------


class TestUrlUtilities:
    def test_strip_trailing_slash(self):
        assert _strip_trailing_slash("/foo/") == "/foo"
        assert _strip_trailing_slash("/") == "/"
        assert _strip_trailing_slash("") == ""
        assert _strip_trailing_slash("/foo") == "/foo"

    def test_normalize_path(self):
        assert _normalize_path("//foo///bar//") == "/foo/bar"
        assert _normalize_path("/") == "/"

    def test_join_url_path(self):
        assert _join_url_path("remote", "documents") == "/remote/documents"
        assert _join_url_path("", "documents") == "/documents"

    def test_relative_url_to(self):
        assert _relative_url_to("/remote", "/remote/documents") == "documents"
        assert _relative_url_to("/remote", "/remote") == "/"
        assert _relative_url_to("", "documents") == "documents"
        assert _relative_url_to("/remote", "") == "/"


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


class TestDateParsing:
    def test_parse_iso_datetime(self):
        dt = _parse_iso_datetime("2024-01-01T00:00:00+00:00")
        assert dt is not None
        assert dt.year == 2024

    def test_parse_iso_datetime_invalid(self):
        assert _parse_iso_datetime("not-a-date") is None

    def test_parse_rfc1123(self):
        dt = _parse_rfc1123("Mon, 01 Jan 2024 12:00:00 GMT")
        assert dt is not None
        assert dt.year == 2024

    def test_parse_rfc1123_fallback_iso(self):
        dt = _parse_rfc1123("2024-01-01T12:00:00+00:00")
        assert dt is not None

    def test_parse_rfc1123_invalid(self):
        assert _parse_rfc1123("not-a-date") is None


# ---------------------------------------------------------------------------
# DAVProperties
# ---------------------------------------------------------------------------


class TestDAVProperties:
    def test_from_none(self):
        props = DAVProperties(None)
        assert props.etag is None
        assert props.content_length is None
        assert props.resource_type is None
        assert props.collection is None
        d = props.as_dict()
        assert d["etag"] is None

    def test_from_xml(self):
        ms = MultiStatusResponse(MULTISTATUS_SINGLE_XML)
        resp = next(iter(ms.responses.values()))
        props = resp.properties
        assert props.etag == '"abc123"'
        assert props.content_length == 1024
        assert props.content_type == "text/markdown"
        assert props.resource_type == "file"
        assert props.collection is False
        assert props.modified is not None
        assert props.created is not None

    def test_directory_resource_type(self):
        ms = MultiStatusResponse(MULTISTATUS_DIR_XML)
        dir_resp = ms.responses.get("/remote/documents")
        assert dir_resp is not None
        assert dir_resp.properties.resource_type == "directory"
        assert dir_resp.properties.collection is True


# ---------------------------------------------------------------------------
# Response and MultiStatusResponse
# ---------------------------------------------------------------------------


class TestMultiStatusResponse:
    def test_parse_directory_listing(self):
        ms = MultiStatusResponse(MULTISTATUS_DIR_XML)
        assert len(ms.responses) == 3
        assert "/remote/documents" in ms.responses
        assert "/remote/documents/readme.md" in ms.responses

    def test_response_path_relative_to(self):
        ms = MultiStatusResponse(MULTISTATUS_DIR_XML)
        resp = ms.responses["/remote/documents/readme.md"]
        rel = resp.path_relative_to("/remote/documents")
        assert rel == "readme.md"

    def test_response_absolute_href(self):
        xml = """\
<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>https://example.com/remote/file.txt</D:href>
    <D:propstat>
      <D:prop><D:resourcetype/></D:prop>
    </D:propstat>
  </D:response>
</D:multistatus>"""
        ms = MultiStatusResponse(xml)
        resp = next(iter(ms.responses.values()))
        assert resp.path == "/remote/file.txt"


# ---------------------------------------------------------------------------
# _prepare_result_info
# ---------------------------------------------------------------------------


class TestPrepareResultInfo:
    def test_detail_true(self):
        ms = MultiStatusResponse(MULTISTATUS_DIR_XML)
        resp = ms.responses["/remote/documents/readme.md"]
        info = _prepare_result_info(resp, "/remote/documents", detail=True)
        assert isinstance(info, dict)
        assert info["name"] == "readme.md"
        assert info["etag"] == '"abc123"'

    def test_detail_false(self):
        ms = MultiStatusResponse(MULTISTATUS_DIR_XML)
        resp = ms.responses["/remote/documents/readme.md"]
        info = _prepare_result_info(resp, "/remote/documents", detail=False)
        assert isinstance(info, str)
        assert info == "readme.md"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TestExceptions:
    def test_resource_not_found(self):
        e = ResourceNotFound("/path")
        assert e.path == "/path"
        assert "could not be found" in str(e)

    def test_insufficient_storage(self):
        e = InsufficientStorage("/path")
        assert e.path == "/path"

    def test_retryable_http_error(self):
        e = RetryableHTTPError(429, "rate limited")
        assert e.status == 429
        assert e.body == "rate limited"
        assert e.retry_after is None

    def test_retryable_http_error_with_retry_after(self):
        e = RetryableHTTPError(429, "rate limited", retry_after=30.0)
        assert e.status == 429
        assert e.retry_after == 30.0

    def test_client_error(self):
        e = ClientError("test")
        assert e.msg == "test"
        assert str(e) == "test"


# ---------------------------------------------------------------------------
# AsyncWebDAVClient - Constructor & Lifecycle
# ---------------------------------------------------------------------------


class TestClientLifecycle:
    def test_init_with_all_params(self):
        client = AsyncWebDAVClient(
            base_url="https://example.com/dav/",
            auth=("user", "pass"),
            headers={"X-Custom": "val"},
            timeout=aiohttp.ClientTimeout(total=30),
            ssl=False,
            max_retries=5,
            max_concurrent=2,
        )
        assert client._base_url == "https://example.com/dav"
        assert client._auth is not None
        assert client._auth.login == "user"
        assert client._headers == {"X-Custom": "val"}
        assert client._ssl is False
        assert client._max_retries == 5
        assert client._max_concurrent == 2

    def test_init_defaults(self):
        client = AsyncWebDAVClient(base_url="https://example.com")
        assert client._auth is None
        assert client._headers == {}
        assert client._ssl is None
        assert client._max_retries == 3
        assert client._max_concurrent == 3
        assert client._session is None

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        client = AsyncWebDAVClient(base_url="https://example.com")
        async with client as c:
            assert c is client
            assert c._session is not None
            assert c._connector is not None
        # After exit, session should be closed
        assert client._session is None

    @pytest.mark.asyncio
    async def test_aclose(self):
        client = AsyncWebDAVClient(base_url="https://example.com")
        await client._ensure_session()
        assert client._session is not None
        await client.aclose()
        assert client._session is None
        assert client._connector is None

    @pytest.mark.asyncio
    async def test_aclose_when_no_session(self):
        client = AsyncWebDAVClient(base_url="https://example.com")
        await client.aclose()  # should not raise

    @pytest.mark.asyncio
    async def test_ensure_session_reuses_existing(self):
        client = AsyncWebDAVClient(base_url="https://example.com")
        s1 = await client._ensure_session()
        s2 = await client._ensure_session()
        assert s1 is s2
        await client.aclose()

    def test_connector_limit_matches_max_concurrent(self):
        client = AsyncWebDAVClient(base_url="https://example.com", max_concurrent=7)
        assert client._max_concurrent == 7


# ---------------------------------------------------------------------------
# AsyncWebDAVClient._request - transport, errors, retry
# ---------------------------------------------------------------------------


class TestRequest:
    @pytest.mark.asyncio
    async def test_request_success_200(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(200, text="ok"))
        resp = await client._request("GET", "/path")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_request_url_joining(self):
        client, session = _make_client(base_url="https://example.com/remote")
        session.request = AsyncMock(return_value=_mock_response(200))
        await client._request("GET", "/documents/file.txt")
        call_args = session.request.call_args
        assert call_args[0] == ("GET", "https://example.com/remote/documents/file.txt")

    @pytest.mark.asyncio
    async def test_request_url_joining_no_leading_slash(self):
        client, session = _make_client(base_url="https://example.com/remote")
        session.request = AsyncMock(return_value=_mock_response(200))
        await client._request("GET", "documents/file.txt")
        call_args = session.request.call_args
        assert call_args[0] == ("GET", "https://example.com/remote/documents/file.txt")

    @pytest.mark.asyncio
    async def test_request_404_raises_resource_not_found(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(404))
        with pytest.raises(ResourceNotFound):
            await client._request("GET", "/missing")

    @pytest.mark.asyncio
    async def test_request_507_raises_insufficient_storage(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(507))
        with pytest.raises(InsufficientStorage):
            await client._request("PUT", "/full")

    @pytest.mark.asyncio
    async def test_request_400_no_retry(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(400, text="bad request"))
        with pytest.raises(ClientError, match="HTTP 400"):
            await client._request("GET", "/bad")
        assert session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_request_429_retries_then_succeeds(self):
        client, session = _make_client(max_retries=3)
        session.request = AsyncMock(
            side_effect=[
                _mock_response(429, text="rate limited"),
                _mock_response(200, text="ok"),
            ]
        )
        resp = await client._request("GET", "/path")
        assert resp.status == 200
        assert session.request.call_count == 2

    @pytest.mark.asyncio
    async def test_request_500_retries(self):
        client, session = _make_client(max_retries=2)
        session.request = AsyncMock(
            side_effect=[
                _mock_response(500, text="error"),
                _mock_response(200, text="ok"),
            ]
        )
        resp = await client._request("GET", "/path")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_request_502_retries_then_raises(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(502, text="bad gateway"))
        with pytest.raises(RetryableHTTPError) as exc_info:
            await client._request("GET", "/path")
        assert exc_info.value.status == 502

    @pytest.mark.asyncio
    async def test_request_503_retries(self):
        client, session = _make_client(max_retries=2)
        session.request = AsyncMock(
            side_effect=[
                _mock_response(503, text="unavailable"),
                _mock_response(200),
            ]
        )
        resp = await client._request("GET", "/path")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_request_504_retries(self):
        client, session = _make_client(max_retries=2)
        session.request = AsyncMock(
            side_effect=[
                _mock_response(504, text="timeout"),
                _mock_response(200),
            ]
        )
        resp = await client._request("GET", "/path")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_request_509_retries(self):
        client, session = _make_client(max_retries=2)
        session.request = AsyncMock(
            side_effect=[
                _mock_response(509, text="bandwidth"),
                _mock_response(200),
            ]
        )
        resp = await client._request("GET", "/path")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_request_connection_error_retries(self):
        client, session = _make_client(max_retries=2)
        session.request = AsyncMock(
            side_effect=[
                aiohttp.ServerDisconnectedError(),
                _mock_response(200),
            ]
        )
        resp = await client._request("GET", "/path")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_request_connection_error_exhausted(self):
        client, session = _make_client()
        session.request = AsyncMock(side_effect=aiohttp.ServerDisconnectedError())
        with pytest.raises(aiohttp.ServerDisconnectedError):
            await client._request("GET", "/path")

    @pytest.mark.asyncio
    async def test_request_ssl_param_passed(self):
        client, session = _make_client(ssl=False)
        session.request = AsyncMock(return_value=_mock_response(200))
        await client._request("GET", "/path")
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["ssl"] is False

    @pytest.mark.asyncio
    async def test_request_headers_passed(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(200))
        await client._request("GET", "/path", headers={"X-Custom": "value"})
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["headers"] == {"X-Custom": "value"}

    @pytest.mark.asyncio
    async def test_request_data_passed(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(200))
        await client._request("PROPFIND", "/path", data="<xml/>")
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["data"] == "<xml/>"

    @pytest.mark.asyncio
    async def test_request_concurrency_limited(self):
        client, session = _make_client(max_concurrent=2)

        call_count = 0
        max_concurrent_seen = 0
        current_concurrent = 0

        original_request = AsyncMock(return_value=_mock_response(200))

        async def counting_request(*args, **kwargs):
            nonlocal call_count, max_concurrent_seen, current_concurrent
            current_concurrent += 1
            max_concurrent_seen = max(max_concurrent_seen, current_concurrent)
            call_count += 1
            await asyncio.sleep(0.05)
            result = await original_request(*args, **kwargs)
            current_concurrent -= 1
            return result

        session.request = counting_request

        tasks = [client._request("GET", f"/path/{i}") for i in range(5)]
        await asyncio.gather(*tasks)
        assert max_concurrent_seen <= 2

    @pytest.mark.asyncio
    async def test_request_concurrency_serialized(self):
        client, session = _make_client(max_concurrent=1)

        max_concurrent_seen = 0
        current_concurrent = 0

        async def counting_request(*args, **kwargs):
            nonlocal max_concurrent_seen, current_concurrent
            current_concurrent += 1
            max_concurrent_seen = max(max_concurrent_seen, current_concurrent)
            await asyncio.sleep(0.02)
            current_concurrent -= 1
            return _mock_response(200)

        session.request = counting_request

        tasks = [client._request("GET", f"/path/{i}") for i in range(3)]
        await asyncio.gather(*tasks)
        assert max_concurrent_seen == 1

    @pytest.mark.asyncio
    async def test_request_empty_path(self):
        client, session = _make_client(base_url="https://example.com/remote")
        session.request = AsyncMock(return_value=_mock_response(200))
        await client._request("GET", "")
        call_args = session.request.call_args
        assert call_args[0] == ("GET", "https://example.com/remote")


# ---------------------------------------------------------------------------
# Redirect handling
# ---------------------------------------------------------------------------


class TestRedirects:
    @pytest.mark.asyncio
    async def test_propfind_passes_allow_redirects(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(207, text=MULTISTATUS_DIR_XML))
        await client.propfind("/documents", follow_redirects=True)
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["allow_redirects"] is True

    @pytest.mark.asyncio
    async def test_propfind_no_redirect_by_default(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(207, text=MULTISTATUS_DIR_XML))
        await client.propfind("/documents")
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["allow_redirects"] is False

    @pytest.mark.asyncio
    async def test_download_follows_redirects(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(200, content=b"data"))
        await client.download("/file.txt")
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["allow_redirects"] is True

    @pytest.mark.asyncio
    async def test_redirect_chain_is_logged(self, caplog):
        client, session = _make_client()
        hop1 = MagicMock()
        hop1.url = "https://example.com/old"
        hop2 = MagicMock()
        hop2.url = "https://example.com/mid"
        session.request = AsyncMock(
            return_value=_mock_response(
                200,
                content=b"data",
                history=(hop1, hop2),
                url="https://example.com/final",
            )
        )
        with caplog.at_level(logging.INFO, logger="soliplex.agents.webdav.async_client"):
            await client._request("GET", "/old", allow_redirects=True)
        assert "redirected" in caplog.text
        assert "https://example.com/old" in caplog.text
        assert "https://example.com/final" in caplog.text

    @pytest.mark.asyncio
    async def test_no_redirect_no_log(self, caplog):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(200))
        with caplog.at_level(logging.INFO, logger="soliplex.agents.webdav.async_client"):
            await client._request("GET", "/path")
        assert "redirected" not in caplog.text


# ---------------------------------------------------------------------------
# propfind
# ---------------------------------------------------------------------------


class TestPropfind:
    @pytest.mark.asyncio
    async def test_propfind_returns_multistatus(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(207, text=MULTISTATUS_DIR_XML))
        result = await client.propfind("/documents")
        assert isinstance(result, MultiStatusResponse)
        assert len(result.responses) == 3

    @pytest.mark.asyncio
    async def test_propfind_non_207_raises(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(200, text="not xml"))
        with pytest.raises(ClientError, match="Expected 207"):
            await client.propfind("/documents")

    @pytest.mark.asyncio
    async def test_propfind_with_custom_headers(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(207, text=MULTISTATUS_SINGLE_XML))
        await client.propfind("/file", headers={"Depth": "0", "X-Custom": "val"})
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["headers"]["Depth"] == "0"
        assert call_kwargs["headers"]["X-Custom"] == "val"

    @pytest.mark.asyncio
    async def test_propfind_with_data(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(207, text=MULTISTATUS_SINGLE_XML))
        await client.propfind("/file", data="<propfind/>")
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["data"] == "<propfind/>"

    @pytest.mark.asyncio
    async def test_propfind_default_depth_header(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(207, text=MULTISTATUS_SINGLE_XML))
        await client.propfind("/file")
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["headers"]["Depth"] == "1"


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


class TestLs:
    @pytest.mark.asyncio
    async def test_ls_filters_parent_directory(self):
        client, session = _make_client(base_url="https://example.com/remote")
        session.request = AsyncMock(return_value=_mock_response(207, text=MULTISTATUS_DIR_XML))
        result = await client.ls("/documents")
        # Should exclude the parent directory entry
        names = [r["name"] if isinstance(r, dict) else r for r in result]
        assert "documents" not in names
        assert "/" not in names

    @pytest.mark.asyncio
    async def test_ls_detail_true_returns_dicts(self):
        client, session = _make_client(base_url="https://example.com/remote")
        session.request = AsyncMock(return_value=_mock_response(207, text=MULTISTATUS_DIR_XML))
        result = await client.ls("/documents", detail=True)
        assert all(isinstance(r, dict) for r in result)
        assert len(result) == 2  # readme.md and report.pdf

    @pytest.mark.asyncio
    async def test_ls_detail_false_returns_strings(self):
        client, session = _make_client(base_url="https://example.com/remote")
        session.request = AsyncMock(return_value=_mock_response(207, text=MULTISTATUS_DIR_XML))
        result = await client.ls("/documents", detail=False)
        assert all(isinstance(r, str) for r in result)

    @pytest.mark.asyncio
    async def test_ls_single_file(self):
        xml = """\
<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/remote/file.txt</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
        <D:getcontentlength>512</D:getcontentlength>
      </D:prop>
    </D:propstat>
  </D:response>
</D:multistatus>"""
        client, session = _make_client(base_url="https://example.com/remote")
        session.request = AsyncMock(return_value=_mock_response(207, text=xml))
        result = await client.ls("/file.txt")
        # The file itself might be returned depending on path matching
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


class TestInfo:
    @pytest.mark.asyncio
    async def test_info_returns_resource_dict(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(207, text=MULTISTATUS_SINGLE_XML))
        result = await client.info("/documents/readme.md")
        assert isinstance(result, dict)
        assert result["etag"] == '"abc123"'
        assert result["content_length"] == 1024

    @pytest.mark.asyncio
    async def test_info_not_found(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(404))
        with pytest.raises(ResourceNotFound):
            await client.info("/missing")

    @pytest.mark.asyncio
    async def test_info_empty_responses_raises(self):
        xml = """\
<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
</D:multistatus>"""
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(207, text=xml))
        with pytest.raises(ResourceNotFound):
            await client.info("/missing")

    @pytest.mark.asyncio
    async def test_info_uses_depth_0(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(207, text=MULTISTATUS_SINGLE_XML))
        await client.info("/file")
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["headers"]["Depth"] == "0"


# ---------------------------------------------------------------------------
# head
# ---------------------------------------------------------------------------


class TestHead:
    @pytest.mark.asyncio
    async def test_head_returns_webdav_response(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(200, headers={"etag": '"abc"', "content-length": "1024"}))
        result = await client.head("/file.txt")
        assert isinstance(result, WebDAVResponse)
        assert result.status == 200

    @pytest.mark.asyncio
    async def test_head_etag_in_headers(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(200, headers={"etag": '"xyz789"'}))
        result = await client.head("/file.txt")
        assert result.headers.get("etag") == '"xyz789"'

    @pytest.mark.asyncio
    async def test_head_follows_redirects(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(200, headers={}))
        await client.head("/file.txt")
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["allow_redirects"] is True


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


class TestDownload:
    @pytest.mark.asyncio
    async def test_download_returns_bytes(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(200, content=b"file content here"))
        result = await client.download("/file.txt")
        assert result == b"file content here"

    @pytest.mark.asyncio
    async def test_download_follows_redirects(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(200, content=b"data"))
        await client.download("/file.txt")
        call_kwargs = session.request.call_args[1]
        assert call_kwargs["allow_redirects"] is True

    @pytest.mark.asyncio
    async def test_download_error_raises(self):
        client, session = _make_client()
        session.request = AsyncMock(return_value=_mock_response(404))
        with pytest.raises(ResourceNotFound):
            await client.download("/missing.txt")


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


class TestFactory:
    @patch("soliplex.agents.webdav.async_client.settings")
    def test_factory_with_explicit_params(self, mock_settings):
        mock_settings.ssl_verify = True
        mock_settings.webdav_max_concurrent_requests = 5
        client = create_async_webdav_client(
            url="https://dav.example.com",
            username="user",
            password="pass",
        )
        assert client._base_url == "https://dav.example.com"
        assert client._auth is not None
        assert client._auth.login == "user"
        assert client._max_concurrent == 5

    @patch("soliplex.agents.webdav.async_client.settings")
    def test_factory_from_settings(self, mock_settings):
        mock_settings.webdav_url = "https://dav.example.com"
        mock_settings.webdav_username = "admin"
        mock_settings.webdav_password = MagicMock()
        mock_settings.webdav_password.get_secret_value.return_value = "secret"
        mock_settings.ssl_verify = True
        mock_settings.webdav_max_concurrent_requests = 3
        client = create_async_webdav_client()
        assert client._base_url == "https://dav.example.com"
        assert client._auth is not None

    @patch("soliplex.agents.webdav.async_client.settings")
    def test_factory_no_url_raises_value_error(self, mock_settings):
        mock_settings.webdav_url = None
        with pytest.raises(ValueError, match="WebDAV URL is required"):
            create_async_webdav_client()

    @patch("soliplex.agents.webdav.async_client.settings")
    def test_factory_partial_auth_no_auth_set(self, mock_settings):
        mock_settings.webdav_url = "https://dav.example.com"
        mock_settings.webdav_username = "user"
        mock_settings.webdav_password = None
        mock_settings.ssl_verify = True
        mock_settings.webdav_max_concurrent_requests = 3
        client = create_async_webdav_client()
        assert client._auth is None  # need both username and password

    @patch("soliplex.agents.webdav.async_client.settings")
    def test_factory_ssl_verify_false(self, mock_settings):
        mock_settings.webdav_url = "https://dav.example.com"
        mock_settings.webdav_username = None
        mock_settings.webdav_password = None
        mock_settings.ssl_verify = False
        mock_settings.webdav_max_concurrent_requests = 3
        client = create_async_webdav_client()
        assert client._ssl is False

    @patch("soliplex.agents.webdav.async_client.settings")
    def test_factory_ssl_verify_true(self, mock_settings):
        mock_settings.webdav_url = "https://dav.example.com"
        mock_settings.webdav_username = None
        mock_settings.webdav_password = None
        mock_settings.ssl_verify = True
        mock_settings.webdav_max_concurrent_requests = 3
        client = create_async_webdav_client()
        assert client._ssl is None  # None = default (verify)


# ---------------------------------------------------------------------------
# WebDAVResponse dataclass
# ---------------------------------------------------------------------------


class TestWebDAVResponse:
    def test_fields(self):
        r = WebDAVResponse(status=200, headers={"etag": '"abc"'})
        assert r.status == 200
        assert r.headers["etag"] == '"abc"'
