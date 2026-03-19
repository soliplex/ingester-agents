"""Shared utility for reading URL list files from local paths or S3."""

import logging
from pathlib import Path

import aiofiles

from soliplex.agents.common.s3 import is_s3_url
from soliplex.agents.common.s3 import read_text_from_s3
from soliplex.agents.config import settings

logger = logging.getLogger(__name__)


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


async def read_urls_file(
    urls_file: str,
    base_dir: str | None = None,
) -> list[str]:
    """Read a URL list file and return non-empty, stripped lines.

    Supports S3 URLs (``s3://bucket/key``) and local filesystem paths.
    For local paths, relative paths are resolved against *base_dir*
    when provided (see :func:`resolve_local_path`).

    Args:
        urls_file: Path or S3 URL to the URL list file.
        base_dir: Optional directory for resolving relative local paths.

    Returns:
        List of non-empty, whitespace-stripped lines.
    """
    if is_s3_url(urls_file):
        content = await read_text_from_s3(urls_file, settings.s3_endpoint_url)
    else:
        resolved = resolve_local_path(urls_file, base_dir)
        async with aiofiles.open(resolved) as f:
            content = await f.read()
    return [line.strip() for line in content.splitlines() if line.strip()]
