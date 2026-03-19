"""Common configuration utilities for file validation."""

import json
import logging
import mimetypes
from pathlib import Path

import aiofiles

from soliplex.agents import ValidationError

# MIME type overrides for Office documents
MIME_OVERRIDES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",  # noqa: E501
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",  # noqa: E501
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",  # noqa: E501
    "text/plantuml": "puml",  # noqa: E501
    "text/asciidoc": "adoc",
    "text/svg+xml": "svg",  # noqa: E501
    "application/x-latex": "latex",  # noqa: E501
    "text/python": "python",  # noqa: E501
    "text/yaml": "yaml",  # noqa: E501
    "text/toml": "toml",
    "text/json": "json",
    "text/xml": "xml",
    "text/javascript": "js",
}

logger = logging.getLogger(__name__)


def check_config(config: list[dict], start: int = 0, end: int = None) -> list[dict]:
    """
    Validate file metadata in configuration.

    Args:
        config: List of file configuration dictionaries
        start: Starting index for validation
        end: Ending index for validation

    Returns:
        List of file configurations with 'valid' and optionally 'reason' fields added
    """
    for row in config:
        path = row["path"]
        ext = Path(path).suffix.lstrip(".")
        row["valid"] = True
        if "metadata" in row and "content-type" in row["metadata"]:
            content_type = row["metadata"]["content-type"]
            if content_type in [
                "application/zip",
                "application/x-zip-compressed",
                "application/octet-stream",
                "application/x-rar-compressed",
                "application/x-7z-compressed",
            ]:
                row["valid"] = False
                row["reason"] = "Unsupported content type"
        else:
            row["valid"] = False
            row["reason"] = "No content type"

        if len(ext) > 4:
            row["valid"] = False
            row["reason"] = f"Unsupported file extension {ext}"
    return config


def detect_mime_type(path: str) -> str:
    """
    Detect MIME type for a file path with Office format overrides.

    Args:
        path: File path to detect MIME type for

    Returns:
        MIME type string
    """
    mime_type = mimetypes.guess_type(str(path))[0]
    if mime_type is None:
        # Check if it matches an Office format by extension
        for mime, ext in MIME_OVERRIDES.items():
            if path.endswith(ext):
                return mime  # pragma: no cover
        if "/issues/" in path:  # pragma: no cover
            return "text/markdown"  # issues don't have mime types so use markdown
        logger.error(f"unrecognized mime type for {path}")
        mime_type = "application/octet-stream"
    return mime_type


async def read_config(config_path: str) -> list[dict]:
    """
    Read and parse a configuration file.

    Args:
        config_path: Path to the inventory JSON file

    Returns:
        List of file configuration dictionaries sorted by size

    Raises:
        ValidationError: If the config file format is invalid
    """
    async with aiofiles.open(config_path) as f:
        raw = await f.read()
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.exception("Invalid JSON in config %s", config_path)
            raise ValidationError(config_path) from e

        if isinstance(config, list):
            ret = config
        elif isinstance(config, dict) and "data" in config:
            ret = config["data"]
        else:
            raise ValidationError(config_path)

        ret = sorted(
            ret,
            key=lambda x: int(x.get("metadata", {}).get("size", 0)),
        )
        return ret
