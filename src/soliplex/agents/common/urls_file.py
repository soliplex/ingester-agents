"""Shared utility for reading URL list files from local paths, S3, or WebDAV."""

import logging
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
import aiohttp

from soliplex.agents.common.s3 import is_s3_url
from soliplex.agents.common.s3 import read_text_from_s3
from soliplex.agents.config import settings

logger = logging.getLogger(__name__)


def is_webdav_url(path: str) -> bool:
    """Return True if *path* looks like an HTTP(S) URL."""
    return path.startswith("http://") or path.startswith("https://")


def resolve_local_path(
    urls_file: str,
    base_dir: str | None = None,
) -> str:
    """Resolve a local urls_file path.

    Resolution order:
    1. If *urls_file* is absolute, return it as-is.
    2. If *base_dir* is provided and ``base_dir / urls_file`` exists,
       return that resolved path.
    3. Otherwise return *urls_file* unchanged (relative to CWD).

    Args:
        urls_file: The path from the manifest or CLI.
        base_dir: Optional directory to resolve relative paths against
            (typically the manifest file's parent directory).

    Returns:
        Resolved path string.
    """
    p = Path(urls_file)
    if p.is_absolute():
        return urls_file
    if base_dir is not None:
        candidate = Path(base_dir) / urls_file
        if candidate.exists():
            return str(candidate)
    return urls_file


async def read_text_from_webdav(
    url: str,
    webdav_url: str | None = None,
    webdav_username: str | None = None,
    webdav_password: str | None = None,
) -> str:
    """Download a text file from a WebDAV server.

    The full file URL is split into a base URL (scheme + host) and a
    path component.  Authentication credentials fall back to the
    global settings when not provided explicitly.  Client creation is
    delegated to :func:`~soliplex.agents.webdav.async_client.create_async_webdav_client`
    so that timeout, header, and TLS settings stay consistent.

    Args:
        url: Full HTTP(S) URL to the file on the WebDAV server.
        webdav_url: Optional override for the WebDAV base URL.
            When *None* the base URL is derived from *url*.
        webdav_username: Optional WebDAV username.
        webdav_password: Optional WebDAV password.

    Returns:
        The file contents decoded as UTF-8 text.
    """
    from soliplex.agents.webdav.async_client import create_async_webdav_client

    parsed = urlparse(url)
    base_url = webdav_url or f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path

    client = create_async_webdav_client(base_url, webdav_username, webdav_password)
    async with client:
        content, _content_type = await client.download(path)
    return content.decode("utf-8")


def _is_on_webdav_host(url: str, webdav_url: str | None) -> bool:
    """Return True when *url* targets the configured WebDAV host.

    The configured host is *webdav_url* when given, else
    ``settings.webdav_url``. A urls_file URL on that host is fetched through the
    authenticated WebDAV client; a URL on any other host (e.g. an internal
    manifest server) is fetched literally instead of having its host rewritten
    to -- and its credentials sent to -- the WebDAV server (see
    :func:`read_urls_file`).
    """
    configured = webdav_url or settings.webdav_url
    if not configured:
        return False
    return urlparse(url).netloc == urlparse(configured).netloc


async def read_text_from_url(url: str) -> str:
    """Download a text file at its literal URL via a plain HTTP GET.

    Unlike :func:`read_text_from_webdav`, the URL is used exactly as given (its
    own scheme and host) and no WebDAV credentials are attached. Used for
    urls_file URLs pointing at a host other than the configured WebDAV server,
    so the host is honored literally rather than rewritten to the WebDAV server.

    Args:
        url: Full HTTP(S) URL to the file.

    Returns:
        The file contents decoded as UTF-8 text.
    """
    ssl = None if settings.ssl_verify else False
    timeout = aiohttp.ClientTimeout(total=300, connect=20)
    headers = {"User-Agent": "soliplex-agent/curl"}
    async with (
        aiohttp.ClientSession(timeout=timeout, headers=headers) as session,
        session.get(url, ssl=ssl, allow_redirects=True) as resp,
    ):
        resp.raise_for_status()
        content = await resp.read()
    return content.decode("utf-8")


async def read_urls_file(
    urls_file: str,
    base_dir: str | None = None,
    webdav_url: str | None = None,
    webdav_username: str | None = None,
    webdav_password: str | None = None,
) -> list[str]:
    """Read a URL list file and return non-empty, stripped lines.

    Supports S3 URLs (``s3://bucket/key``), HTTP(S) URLs, and local filesystem
    paths.  For local paths, relative paths are resolved against *base_dir* when
    provided (see :func:`resolve_local_path`).

    HTTP(S) URLs are routed by host: a URL on the configured WebDAV host (see
    :func:`_is_on_webdav_host`) is fetched through the authenticated WebDAV
    client, while a URL on any other host is fetched **literally** via a plain
    GET (:func:`read_text_from_url`) -- its host is honored as given rather than
    rewritten to the WebDAV server.

    Args:
        urls_file: Path, S3 URL, or HTTP(S) URL to the URL list file.
        base_dir: Optional directory for resolving relative local paths.
        webdav_url: Optional WebDAV base URL override (identifies the WebDAV
            host and, for WebDAV-host URLs, the base).
        webdav_username: Optional WebDAV username (for WebDAV-host URLs).
        webdav_password: Optional WebDAV password (for WebDAV-host URLs).

    Returns:
        List of non-empty, whitespace-stripped lines.
    """
    if is_s3_url(urls_file):
        content = await read_text_from_s3(urls_file, settings.s3_endpoint_url)
    elif is_webdav_url(urls_file):
        if _is_on_webdav_host(urls_file, webdav_url):
            content = await read_text_from_webdav(urls_file, webdav_url, webdav_username, webdav_password)
        else:
            content = await read_text_from_url(urls_file)
    else:
        resolved = resolve_local_path(urls_file, base_dir)
        async with aiofiles.open(resolved) as f:
            content = await f.read()
    return [line.strip() for line in content.splitlines() if line.strip()]
