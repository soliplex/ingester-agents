"""S3 helpers for reading objects from S3-compatible stores."""

import logging

logger = logging.getLogger(__name__)


def is_s3_url(path: str) -> bool:
    """Return True if *path* starts with ``s3://``."""
    return path.startswith("s3://")


def parse_s3_url(url: str) -> tuple[str, str]:
    """Extract bucket and key from an ``s3://bucket/key`` URL.

    Args:
        url: S3 URL in the form ``s3://bucket/key``.

    Returns:
        Tuple of (bucket, key).

    Raises:
        ValueError: If the URL is malformed.
    """
    stripped = url.removeprefix("s3://")
    if "/" not in stripped:
        raise ValueError(f"Invalid S3 URL '{url}': expected s3://bucket/key")
    bucket, key = stripped.split("/", 1)
    if not bucket or not key:
        raise ValueError(f"Invalid S3 URL '{url}': bucket and key must be non-empty")
    return bucket, key


async def read_text_from_s3(
    url: str,
    endpoint_url: str | None = None,
) -> str:
    """Download an S3 object and return its contents as UTF-8 text.

    Uses the standard boto3 credential chain (env vars, ~/.aws, IAM roles).

    Args:
        url: S3 URL (``s3://bucket/key``).
        endpoint_url: Optional custom endpoint for non-AWS S3 (MinIO, etc.).

    Returns:
        The object body decoded as UTF-8 text.
    """
    import aioboto3

    bucket, key = parse_s3_url(url)
    session = aioboto3.Session()
    async with session.client("s3", endpoint_url=endpoint_url) as s3_client:
        response = await s3_client.get_object(Bucket=bucket, Key=key)
        body = await response["Body"].read()
    return body.decode("utf-8")
