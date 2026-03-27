"""Async WebDAV client using aiohttp with retry and concurrency control."""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree.ElementTree import Element
from xml.etree.ElementTree import fromstring as str2xml

import aiohttp
from tenacity import AsyncRetrying
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from soliplex.agents.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ClientError(Exception):
    """Base exception for WebDAV client errors."""

    def __init__(self, msg: str) -> None:
        self.msg = msg
        super().__init__(msg)


class ResourceNotFound(ClientError):
    """Resource does not exist on the server (404)."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"The resource {path} could not be found")


class InsufficientStorage(ClientError):
    """Server has insufficient storage (507)."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__("Insufficient Storage on the server")


class RetryableHTTPError(ClientError):
    """Raised on retryable HTTP status codes to trigger tenacity retry."""

    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}")


# Retryable status codes (matching webdav4 retry.py behaviour)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 509}

# Exception types that trigger a retry
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RetryableHTTPError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    ConnectionResetError,
    TimeoutError,
)

RETRY_MAX_DELAY = 30


# ---------------------------------------------------------------------------
# URL utilities (ported from webdav4.urls)
# ---------------------------------------------------------------------------


def _strip_trailing_slash(path: str) -> str:
    return path.rstrip("/") if path and path != "/" else path


def _normalize_path(path: str) -> str:
    path = re.sub("/{2,}", "/", path)
    return _strip_trailing_slash(path)


def _join_url_path(hostname: str, path: str) -> str:
    path = path.strip("/")
    return _normalize_path(f"/{hostname}/{path}")


def _relative_url_to(base_path: str, rel: str) -> str:
    base = base_path.strip("/")
    rel = rel.strip("/")
    if base == rel or not rel:
        return "/"
    if not base and rel:
        return rel
    index = len(base) + 1
    return rel[index:]


# ---------------------------------------------------------------------------
# PROPFIND / Multistatus XML parsing (ported from webdav4.multistatus)
# ---------------------------------------------------------------------------

_MAPPING_PROPS: dict[str, str] = {
    "content_length": "getcontentlength",
    "etag": "getetag",
    "created": "creationdate",
    "modified": "getlastmodified",
    "content_language": "getcontentlanguage",
    "content_type": "getcontenttype",
    "display_name": "displayname",
}


def _prop(node: Element, name: str, relative: bool = False) -> str | None:
    namespace = "{DAV:}"
    selector = ".//" if relative else ""
    xpath = f"{selector}{namespace}{name}"
    return node.findtext(xpath)


def _parse_iso_datetime(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _parse_rfc1123(s: str) -> datetime | None:
    try:
        return parsedate_to_datetime(s)
    except Exception:  # noqa: BLE001
        return _parse_iso_datetime(s)


class DAVProperties:
    """Parsed properties from a PROPFIND <d:propstat> element."""

    def __init__(self, response_xml: Element | None = None) -> None:
        self.raw: dict[str, Any] = {}

        def extract(prop_name: str) -> str | None:
            text = _prop(response_xml, _MAPPING_PROPS[prop_name], relative=True) if response_xml is not None else None
            self.raw[prop_name] = text
            return text

        created = extract("created")
        self.created = _parse_iso_datetime(created) if created else None

        modified = extract("modified")
        self.modified = _parse_rfc1123(modified) if modified else None

        self.etag = extract("etag") or None
        self.content_type = extract("content_type")

        content_length = extract("content_length")
        self.content_length = int(content_length) if content_length else None
        self.content_language = extract("content_language")

        collection: bool | None = None
        resource_type: str | None = None
        resource_xml: Element | None = None

        if response_xml is not None:
            resource_xml = response_xml.find(".//{DAV:}resourcetype")

        if resource_xml is not None:
            collection = resource_xml.find(".//{DAV:}collection") is not None
            resource_type = "directory" if collection else "file"

        self.collection = collection
        self.resource_type = resource_type
        self.display_name = extract("display_name")

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_length": self.content_length,
            "created": self.created,
            "modified": self.modified,
            "content_language": self.content_language,
            "content_type": self.content_type,
            "etag": self.etag,
            "type": self.resource_type,
            "display_name": self.display_name,
        }


class Response:
    """Individual response from a multistatus PROPFIND response."""

    def __init__(self, response_xml: Element) -> None:
        href = _prop(response_xml, "href")
        assert href

        self.href = href
        self.path = href.split("?")[0]  # strip query string
        if "://" in self.path:
            # absolute URL — extract path portion
            from urllib.parse import urlparse

            self.path = urlparse(self.path).path

        self.path_norm = _strip_trailing_slash(self.path)
        self.properties = DAVProperties(response_xml)

    def path_relative_to(self, base_path: str) -> str:
        return _relative_url_to(base_path, self.path_norm)


class MultiStatusResponse:
    """Container for parsed multistatus (207) responses."""

    def __init__(self, content: str) -> None:
        self.content = content
        tree = str2xml(content)  # noqa: S314

        self.responses: dict[str, Response] = {}
        for resp in tree.findall(".//{DAV:}response"):
            r_obj = Response(resp)
            self.responses[r_obj.path_norm] = r_obj


# ---------------------------------------------------------------------------
# Result helper (ported from webdav4.client._prepare_result_info)
# ---------------------------------------------------------------------------


def _prepare_result_info(response: Response, base_path: str, detail: bool = True) -> str | dict[str, Any]:
    rel = response.path_relative_to(base_path)
    if not detail:
        return rel
    return {"name": rel, "href": response.href, **response.properties.as_dict()}


# ---------------------------------------------------------------------------
# Response dataclass for head()
# ---------------------------------------------------------------------------


@dataclass
class WebDAVResponse:
    """Lightweight wrapper around status + headers from an HTTP response."""

    status: int
    headers: dict[str, str]


# ---------------------------------------------------------------------------
# Async WebDAV Client
# ---------------------------------------------------------------------------


class AsyncWebDAVClient:
    """Async WebDAV client built on aiohttp with retry and concurrency control.

    Implements only the methods required by the ingester-agents webdav app:
    ls, info, head, download, and the internal propfind.
    """

    def __init__(
        self,
        base_url: str,
        auth: tuple[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
        ssl: bool | None = None,
        max_retries: int = 3,
        max_concurrent: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = aiohttp.BasicAuth(auth[0], auth[1]) if auth else None
        self._headers = headers or {}
        self._timeout = timeout or aiohttp.ClientTimeout(total=60, connect=20)
        self._ssl = ssl
        self._max_retries = max_retries
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._session: aiohttp.ClientSession | None = None
        self._connector: aiohttp.TCPConnector | None = None

    # -- lifecycle -----------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._connector = aiohttp.TCPConnector(limit=self._max_concurrent)
            self._session = aiohttp.ClientSession(
                auth=self._auth,
                headers=self._headers,
                timeout=self._timeout,
                connector=self._connector,
            )
        return self._session

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            self._connector = None

    async def __aenter__(self) -> "AsyncWebDAVClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    # -- core transport with retry + semaphore --------------------------------

    def _build_url(self, path: str) -> str:
        path = path.lstrip("/")
        return f"{self._base_url}/{path}" if path else self._base_url

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allow_redirects: bool = False,
        headers: dict[str, str] | None = None,
        data: str | None = None,
    ) -> aiohttp.ClientResponse:
        session = await self._ensure_session()
        url = self._build_url(path)

        kwargs: dict[str, Any] = {
            "allow_redirects": allow_redirects,
        }
        if self._ssl is not None:
            kwargs["ssl"] = self._ssl
        if headers:
            kwargs["headers"] = headers
        if data is not None:
            kwargs["data"] = data

        async with self._semaphore:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
                wait=wait_exponential(multiplier=1, max=RETRY_MAX_DELAY),
                stop=stop_after_attempt(self._max_retries),
                reraise=True,
            ):
                with attempt:
                    resp = await session.request(method, url, **kwargs)

                    if resp.status == 404:
                        resp.release()
                        raise ResourceNotFound(path)
                    if resp.status == 507:
                        resp.release()
                        raise InsufficientStorage(path)
                    if resp.status in RETRYABLE_STATUS_CODES:
                        body = await resp.text()
                        resp.release()
                        raise RetryableHTTPError(resp.status, body)
                    if resp.status >= 400:
                        body = await resp.text()
                        resp.release()
                        raise ClientError(f"HTTP {resp.status}: {body[:200]}")

                    return resp  # type: ignore[return-value]

        raise AssertionError("unreachable")  # pragma: no cover

    # -- WebDAV methods -------------------------------------------------------

    async def propfind(
        self,
        path: str,
        data: str | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> MultiStatusResponse:
        """Send a PROPFIND request and parse the multistatus response."""
        merged_headers = dict(headers or {})
        if "Depth" not in merged_headers:
            merged_headers["Depth"] = "1"

        resp = await self._request(
            "PROPFIND",
            path,
            allow_redirects=follow_redirects,
            headers=merged_headers,
            data=data,
        )
        text = await resp.text()
        resp.release()

        if resp.status != 207:
            raise ClientError(f"Expected 207 multistatus, got {resp.status}")

        return MultiStatusResponse(text)

    async def ls(self, path: str, detail: bool = True) -> list[str | dict[str, Any]]:
        """List items in a WebDAV directory via PROPFIND with Depth: 1."""
        result = await self.propfind(
            path,
            headers={"Depth": "1"},
            follow_redirects=True,
        )
        responses = result.responses

        # Resolve the base path for filtering
        base_path = _normalize_path(
            _join_url_path(
                self._base_url.split("://", 1)[-1].split("/", 1)[-1] if "/" in self._base_url.split("://", 1)[-1] else "",
                path,
            )
        )

        # Remove the directory entry itself
        responses.pop(base_path, None)

        return [_prepare_result_info(resp, base_path, detail) for resp in responses.values()]

    async def info(self, path: str) -> dict[str, Any]:
        """Return metadata for a single resource."""
        result = await self.propfind(path, headers={"Depth": "0"}, follow_redirects=False)
        responses = result.responses
        if not responses:
            raise ResourceNotFound(path)
        # Return the first (and should be only) response
        resp = next(iter(responses.values()))
        info = _prepare_result_info(resp, "", detail=True)
        assert isinstance(info, dict)
        return info

    async def head(self, path: str) -> WebDAVResponse:
        """Send an HTTP HEAD request. Returns status and headers."""
        resp = await self._request("HEAD", path, allow_redirects=True)
        headers = dict(resp.headers)
        status = resp.status
        resp.release()
        return WebDAVResponse(status=status, headers=headers)

    async def download(self, path: str) -> bytes:
        """Download a file via HTTP GET. Returns the file content as bytes."""
        resp = await self._request("GET", path, allow_redirects=True)
        content = await resp.read()
        resp.release()
        return content


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def create_async_webdav_client(
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> AsyncWebDAVClient:
    """Create an AsyncWebDAVClient from explicit params or settings.

    Args:
        url: WebDAV server URL (falls back to settings.webdav_url).
        username: WebDAV username (falls back to settings.webdav_username).
        password: WebDAV password (falls back to settings.webdav_password).

    Returns:
        Configured AsyncWebDAVClient instance.

    Raises:
        ValueError: If no URL is available.
    """
    webdav_url = url or settings.webdav_url
    webdav_username = username or settings.webdav_username
    webdav_password = password or (settings.webdav_password.get_secret_value() if settings.webdav_password else None)

    if not webdav_url:
        raise ValueError("WebDAV URL is required (set WEBDAV_URL environment variable)")

    auth = None
    if webdav_username and webdav_password:
        auth = (webdav_username, webdav_password)

    ssl: bool | None = None
    if not settings.ssl_verify:
        ssl = False

    return AsyncWebDAVClient(
        base_url=webdav_url,
        auth=auth,
        headers={"User-Agent": "soliplex-agent/curl"},
        timeout=aiohttp.ClientTimeout(total=60, connect=20),
        ssl=ssl,
        max_concurrent=settings.webdav_max_concurrent_requests,
    )
