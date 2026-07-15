"""Common configuration utilities for file validation."""

import json
import logging
from pathlib import Path

import aiofiles

from soliplex.agents import ValidationError

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
