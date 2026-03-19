"""Tests for S3 helper utilities."""

import sys
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from soliplex.agents.common.s3 import is_s3_url
from soliplex.agents.common.s3 import parse_s3_url
from soliplex.agents.common.s3 import read_text_from_s3


class TestIsS3Url:
    def test_s3_url(self):
        assert is_s3_url("s3://bucket/key.txt") is True

    def test_s3_url_nested(self):
        assert is_s3_url("s3://bucket/path/to/key.txt") is True

    def test_local_path(self):
        assert is_s3_url("/local/path.txt") is False

    def test_https_url(self):
        assert is_s3_url("https://example.com/file.txt") is False

    def test_empty_string(self):
        assert is_s3_url("") is False

    def test_relative_path(self):
        assert is_s3_url("relative/path.txt") is False


class TestParseS3Url:
    def test_simple(self):
        bucket, key = parse_s3_url("s3://my-bucket/my-key.txt")
        assert bucket == "my-bucket"
        assert key == "my-key.txt"

    def test_nested_key(self):
        bucket, key = parse_s3_url("s3://bucket/path/to/file.txt")
        assert bucket == "bucket"
        assert key == "path/to/file.txt"

    def test_no_slash_raises(self):
        with pytest.raises(ValueError, match="expected s3://bucket/key"):
            parse_s3_url("s3://bucketonly")

    def test_empty_bucket_raises(self):
        with pytest.raises(ValueError, match="bucket and key must be non-empty"):
            parse_s3_url("s3:///key.txt")

    def test_empty_key_raises(self):
        with pytest.raises(ValueError, match="bucket and key must be non-empty"):
            parse_s3_url("s3://bucket/")


def _make_mock_aioboto3(body_bytes: bytes, session_mock: MagicMock | None = None):
    """Create a mock aioboto3 module with a Session that returns a mock S3 client."""
    mock_body = AsyncMock()
    mock_body.read.return_value = body_bytes

    mock_client = AsyncMock()
    mock_client.get_object.return_value = {"Body": mock_body}
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_session = session_mock or MagicMock()
    mock_session.client.return_value = mock_client

    mock_module = MagicMock()
    mock_module.Session.return_value = mock_session
    return mock_module, mock_session, mock_client


class TestReadTextFromS3:
    @pytest.mark.asyncio
    async def test_reads_object(self):
        mock_module, _session, mock_client = _make_mock_aioboto3(b"line1\nline2\n")
        with patch.dict(sys.modules, {"aioboto3": mock_module}):
            result = await read_text_from_s3("s3://my-bucket/my-key.txt")

        assert result == "line1\nline2\n"
        mock_client.get_object.assert_called_once_with(Bucket="my-bucket", Key="my-key.txt")

    @pytest.mark.asyncio
    async def test_passes_endpoint_url(self):
        mock_module, mock_session, _client = _make_mock_aioboto3(b"content")
        with patch.dict(sys.modules, {"aioboto3": mock_module}):
            await read_text_from_s3(
                "s3://bucket/key.txt",
                endpoint_url="https://minio:9000",
            )

        mock_session.client.assert_called_once_with("s3", endpoint_url="https://minio:9000")

    @pytest.mark.asyncio
    async def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="expected s3://bucket/key"):
            await read_text_from_s3("s3://bucketonly")
