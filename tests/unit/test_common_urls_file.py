"""Tests for urls_file shared utility."""

from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from soliplex.agents.common.urls_file import read_urls_file
from soliplex.agents.common.urls_file import resolve_local_path


class TestResolveLocalPath:
    def test_absolute_path(self):
        assert resolve_local_path("/abs/path.txt") == "/abs/path.txt"

    def test_absolute_path_ignores_base_dir(self):
        assert resolve_local_path("/abs/path.txt", base_dir="/other") == "/abs/path.txt"

    def test_relative_with_base_dir_exists(self, tmp_path):
        (tmp_path / "urls.txt").write_text("url1\n")
        result = resolve_local_path("urls.txt", base_dir=str(tmp_path))
        assert result == str(tmp_path / "urls.txt")

    def test_relative_with_base_dir_not_exists(self, tmp_path):
        result = resolve_local_path("missing.txt", base_dir=str(tmp_path))
        assert result == "missing.txt"

    def test_relative_no_base_dir(self):
        result = resolve_local_path("relative.txt")
        assert result == "relative.txt"

    def test_relative_base_dir_none(self):
        result = resolve_local_path("relative.txt", base_dir=None)
        assert result == "relative.txt"


class TestReadUrlsFile:
    @pytest.mark.asyncio
    async def test_local_file(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text("http://a.com\nhttp://b.com\n")
        result = await read_urls_file(str(f))
        assert result == ["http://a.com", "http://b.com"]

    @pytest.mark.asyncio
    async def test_local_file_strips_whitespace(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text("  http://a.com  \n  http://b.com  \n")
        result = await read_urls_file(str(f))
        assert result == ["http://a.com", "http://b.com"]

    @pytest.mark.asyncio
    async def test_local_file_filters_blank_lines(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text("http://a.com\n\n  \nhttp://b.com\n\n")
        result = await read_urls_file(str(f))
        assert result == ["http://a.com", "http://b.com"]

    @pytest.mark.asyncio
    async def test_relative_with_base_dir(self, tmp_path):
        f = tmp_path / "urls.txt"
        f.write_text("http://a.com\n")
        result = await read_urls_file("urls.txt", base_dir=str(tmp_path))
        assert result == ["http://a.com"]

    @pytest.mark.asyncio
    async def test_s3_url(self):
        with patch(
            "soliplex.agents.common.urls_file.read_text_from_s3",
            new_callable=AsyncMock,
            return_value="http://a.com\nhttp://b.com\n",
        ) as mock_s3:
            result = await read_urls_file("s3://bucket/urls.txt")

        assert result == ["http://a.com", "http://b.com"]
        mock_s3.assert_called_once()

    @pytest.mark.asyncio
    async def test_s3_url_passes_endpoint(self):
        with (
            patch(
                "soliplex.agents.common.urls_file.read_text_from_s3",
                new_callable=AsyncMock,
                return_value="url1\n",
            ) as mock_s3,
            patch("soliplex.agents.common.urls_file.settings") as mock_settings,
        ):
            mock_settings.s3_endpoint_url = "https://minio:9000"
            await read_urls_file("s3://bucket/key.txt")

        mock_s3.assert_called_once_with("s3://bucket/key.txt", "https://minio:9000")

    @pytest.mark.asyncio
    async def test_s3_url_no_endpoint(self):
        with (
            patch(
                "soliplex.agents.common.urls_file.read_text_from_s3",
                new_callable=AsyncMock,
                return_value="url1\n",
            ) as mock_s3,
            patch("soliplex.agents.common.urls_file.settings") as mock_settings,
        ):
            mock_settings.s3_endpoint_url = None
            await read_urls_file("s3://bucket/key.txt")

        mock_s3.assert_called_once_with("s3://bucket/key.txt", None)
