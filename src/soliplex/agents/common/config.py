"""Common configuration utilities for file validation."""

from pathlib import Path


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
