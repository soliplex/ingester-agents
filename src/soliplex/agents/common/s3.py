"""S3 helpers for reading objects from S3-compatible stores."""

import logging

logger = logging.getLogger(__name__)


def is_s3_url(path: str) -> bool:
    """Return True if *path* starts with ``s3://``."""
    return path.startswith("s3://")


def split_bucket(configured: str) -> tuple[str, str]:
    """Split a configured bucket into its name and an optional base prefix.

    Accepts the same spelling as the ``S3_BUCKET`` variable the haiku-rag
    configs interpolate -- a full ``s3://bucket/prefix`` URI -- as well as a
    bare bucket name, so one value can be shared between the reader and the
    writer without either having to know which form the other wants.

    ``s3://bucket/ingester`` puts every download under ``ingester/``, with
    ``download_dir`` nested inside it. That is what makes the writer's keys
    line up with a reader pointed at ``${S3_BUCKET}/ingester/...``.

    Lives here rather than beside the store it configures: the settings object
    validates a configured bucket while it is being built, and the store module
    imports those settings, so the parser has to sit somewhere neither of them
    depends on.

    Args:
        configured: Bucket name, ``s3://bucket``, or ``s3://bucket/prefix``.

    Returns:
        ``(bucket, base_prefix)``; the prefix is ``""`` when none was given.

    Raises:
        ValueError: if a non-``s3`` scheme is given, or no bucket is named.
            Both are silent misroutes otherwise.
    """
    value = configured.strip()
    if "://" in value:
        scheme, _, value = value.partition("://")
        if scheme.lower() != "s3":
            raise ValueError(f"download bucket must be an s3:// URI or a bare bucket name, got '{configured}'")
    bucket, _, prefix = value.strip("/").partition("/")
    if not bucket:
        raise ValueError(f"download bucket names no bucket: '{configured}'")
    return bucket, prefix.strip("/")


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
