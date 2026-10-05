"""Shared utility for reading URL list files from local paths, S3, or WebDAV."""

import logging
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
import aiohttp

from soliplex.agents import UrlsFileFormatError
from soliplex.agents.common.s3 import is_s3_url
from soliplex.agents.common.s3 import read_text_from_s3
from soliplex.agents.config import settings

logger = logging.getLogger(__name__)

LineValidator = Callable[[str], bool]

# Tags that mark a document as HTML when it also starts with "<" (see
# _looks_like_html). Only the first _HTML_SNIFF_CHARS are inspected.
_HTML_MARKERS = ("<!doctype html", "<html", "<head", "<body")
_HTML_SNIFF_CHARS = 4096

# How many dropped lines the warning quotes, and how much of each.
_MAX_EXAMPLES = 5
_MAX_EXAMPLE_CHARS = 120


def is_webdav_url(path: str) -> bool:
    """Return True if *path* looks like an HTTP(S) URL."""
    return path.startswith("http://") or path.startswith("https://")


def is_webdav_path(line: str) -> bool:
    """Return True if *line* is an absolute path on a WebDAV server.

    Full URLs are not accepted: the WebDAV client joins a path onto its base
    URL, so ``https://host/path`` would become ``base/https://host/path``.
    """
    return line.startswith("/")


def is_http_url(line: str) -> bool:
    """Return True if *line* is an ``http://`` or ``https://`` URL with a host."""
    parsed = urlparse(line)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _looks_like_html(content: str) -> bool:
    """Return True if *content* is an HTML document rather than a URL list.

    The document must start with ``<`` (after a BOM and whitespace) **and**
    contain one of :data:`_HTML_MARKERS` near its start, so a list line that
    merely contains ``<html`` can't trip it.
    """
    head = content[:_HTML_SNIFF_CHARS].lstrip("\ufeff \t\r\n").lower()
    return head.startswith("<") and any(marker in head for marker in _HTML_MARKERS)


def _example(line: str) -> str:
    """Return *line* quoted and truncated for a log or error message."""
    if len(line) > _MAX_EXAMPLE_CHARS:
        line = line[:_MAX_EXAMPLE_CHARS] + "..."
    return repr(line)


def parse_urls_content(
    content: str,
    urls_file: str,
    *,
    is_valid_line: LineValidator | None = None,
) -> list[str]:
    """Turn the text of a URL list file into its list of URLs.

    - An HTML document raises :class:`UrlsFileFormatError`.
    - Lines are stripped; blank lines and ``#`` comment lines are dropped.
    - When *is_valid_line* is given, lines it rejects are dropped, with one
      WARNING per file giving the count and a few examples.
    - If lines were dropped and none remain, :class:`UrlsFileFormatError` is
      raised: an empty list from a non-empty file means the file is wrong,
      and returning it would read as "the source is empty".
    - A file with no non-blank lines at all returns ``[]``.

    Args:
        content: The decoded file contents.
        urls_file: The file's location, for messages.
        is_valid_line: Optional per-line check (e.g. :func:`is_webdav_path`).

    Returns:
        The valid lines, in file order.
    """
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if _looks_like_html(content):
        first = lines[0] if lines else ""
        raise UrlsFileFormatError(
            f"urls_file {urls_file} returned HTML, not a URL list (first line: {_example(first)}); refusing to use it"
        )

    entries = [line for line in lines if not line.startswith("#")]
    if is_valid_line is None:
        valid, invalid = entries, []
    else:
        valid = [line for line in entries if is_valid_line(line)]
        invalid = [line for line in entries if not is_valid_line(line)]

    if lines and not valid:
        if invalid:
            raise UrlsFileFormatError(
                f"urls_file {urls_file}: all {len(invalid)} lines were invalid; first: {_example(invalid[0])}"
            )
        raise UrlsFileFormatError(f"urls_file {urls_file}: contains only comment lines, no URLs")

    if invalid:
        examples = ", ".join(_example(line) for line in invalid[:_MAX_EXAMPLES])
        logger.warning(
            "urls_file %s: dropped %d invalid line(s) of %d, e.g. %s",
            urls_file,
            len(invalid),
            len(entries),
            examples,
        )
    return valid


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
    *,
    is_valid_line: LineValidator | None = None,
) -> list[str]:
    """Read a URL list file and return its URLs.

    Supports S3 URLs (``s3://bucket/key``), HTTP(S) URLs, and local filesystem
    paths.  For local paths, relative paths are resolved against *base_dir* when
    provided (see :func:`resolve_local_path`).

    HTTP(S) URLs are routed by host: a URL on the configured WebDAV host (see
    :func:`_is_on_webdav_host`) is fetched through the authenticated WebDAV
    client, while a URL on any other host is fetched **literally** via a plain
    GET (:func:`read_text_from_url`) -- its host is honored as given rather than
    rewritten to the WebDAV server.

    The text is then checked and filtered by :func:`parse_urls_content`.

    Args:
        urls_file: Path, S3 URL, or HTTP(S) URL to the URL list file.
        base_dir: Optional directory for resolving relative local paths.
        webdav_url: Optional WebDAV base URL override (identifies the WebDAV
            host and, for WebDAV-host URLs, the base).
        webdav_username: Optional WebDAV username (for WebDAV-host URLs).
        webdav_password: Optional WebDAV password (for WebDAV-host URLs).
        is_valid_line: Optional per-line check; lines it rejects are dropped.
            When *None*, comments are still stripped and HTML still rejected.

    Returns:
        List of non-empty, whitespace-stripped, non-comment lines.

    Raises:
        UrlsFileFormatError: The file isn't UTF-8 text, is an HTML document, or
            has no valid line left after filtering.
    """
    try:
        if is_s3_url(urls_file):
            content = await read_text_from_s3(urls_file, settings.s3_endpoint_url)
        elif is_webdav_url(urls_file):
            if _is_on_webdav_host(urls_file, webdav_url):
                content = await read_text_from_webdav(urls_file, webdav_url, webdav_username, webdav_password)
            else:
                content = await read_text_from_url(urls_file)
        else:
            resolved = resolve_local_path(urls_file, base_dir)
            async with aiofiles.open(resolved, encoding="utf-8") as f:
                content = await f.read()
    except UnicodeDecodeError as exc:
        raise UrlsFileFormatError(f"urls_file {urls_file} is not UTF-8 text, not a URL list: {exc}") from exc
    return parse_urls_content(content, urls_file, is_valid_line=is_valid_line)
