"""Shared utility for reading URL list files from local paths, S3, or WebDAV."""

import logging
from pathlib import Path

import aiofiles

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
    from urllib.parse import urlparse

    from soliplex.agents.webdav.async_client import create_async_webdav_client

    parsed = urlparse(url)
    base_url = webdav_url or f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path

    client = create_async_webdav_client(base_url, webdav_username, webdav_password)
    async with client:
        content = await client.download(path)
    return content.decode("utf-8")


async def read_urls_file(
    urls_file: str,
    base_dir: str | None = None,
    webdav_url: str | None = None,
    webdav_username: str | None = None,
    webdav_password: str | None = None,
) -> list[str]:
    """Read a URL list file and return non-empty, stripped lines.

    Supports S3 URLs (``s3://bucket/key``), WebDAV URLs
    (``http(s)://...``), and local filesystem paths.  For local paths,
    relative paths are resolved against *base_dir* when provided (see
    :func:`resolve_local_path`).

    Args:
        urls_file: Path, S3 URL, or WebDAV URL to the URL list file.
        base_dir: Optional directory for resolving relative local paths.
        webdav_url: Optional WebDAV base URL override (for WebDAV URLs).
        webdav_username: Optional WebDAV username (for WebDAV URLs).
        webdav_password: Optional WebDAV password (for WebDAV URLs).

    Returns:
        List of non-empty, whitespace-stripped lines.
    """
    if is_s3_url(urls_file):
        content = await read_text_from_s3(urls_file, settings.s3_endpoint_url)
    elif is_webdav_url(urls_file):
        content = await read_text_from_webdav(urls_file, webdav_url, webdav_username, webdav_password)
    else:
        resolved = resolve_local_path(urls_file, base_dir)
        async with aiofiles.open(resolved) as f:
            content = await f.read()
    return [line.strip() for line in content.splitlines() if line.strip()]
