"""Common configuration utilities for file validation."""

import mimetypes

# MIME type overrides for Office documents
MIME_OVERRIDES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",  # noqa: E501
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",  # noqa: E501
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",  # noqa: E501
}


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
        ext = path.split(".")[-1]
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
        mime_type = "application/octet-stream"
    return mime_type
