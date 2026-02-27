"""Tests for soliplex.agents.webdav.app module."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import aiofiles
import pytest

from soliplex.agents.webdav import app as webdav_app


@pytest.fixture
def mock_webdav_client():
    """Create a mock WebDAV client."""
    client = MagicMock()
    client.ls.return_value = [
        {"name": "/documents", "type": "directory"},
        {"name": "/documents/test.md", "type": "file", "size": 100},
        {"name": "/documents/readme.pdf", "type": "file", "size": 200},
    ]

    # Mock download_fileobj to write content to buffer
    def mock_download_fileobj(path, buffer):
        buffer.write(b"test content")

    client.download_fileobj = mock_download_fileobj
    return client


class TestCreateWebDAVClient:
    """Tests for create_webdav_client function."""

    def test_create_webdav_client_with_params(self):
        """Test creating WebDAV client with explicit parameters."""
        client = webdav_app.create_webdav_client(url="https://webdav.example.com", username="user", password="pass")
        assert client is not None

    def test_create_webdav_client_no_url(self):
        """Test creating WebDAV client without URL raises error."""
        with patch("soliplex.agents.webdav.app.settings") as mock_settings:
            mock_settings.webdav_url = None
            with pytest.raises(ValueError, match="WebDAV URL is required"):
                webdav_app.create_webdav_client()


class TestBuildConfig:
    """Tests for build_config function."""

    @pytest.mark.asyncio
    async def test_build_config(self, mock_webdav_client):
        """Test building config from WebDAV directory."""
        with (
            patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client),
            patch("soliplex.agents.webdav.app.recursive_listdir_webdav", new_callable=AsyncMock) as mock_ls,
        ):
            mock_ls.return_value = [
                {"path": "/documents/test.md", "size": 100},
                {"path": "/documents/readme.pdf", "size": 200},
            ]

            config = await webdav_app.build_config("/documents")

            assert isinstance(config, list)
            assert len(config) == 2
            # Verify paths are relative
            assert config[0]["path"] == "test.md" or config[0]["path"] == "readme.pdf"
            assert all("sha256" in item for item in config)
            assert all("metadata" in item for item in config)


class TestRecursiveListdirWebdav:
    """Tests for recursive_listdir_webdav function."""

    @pytest.mark.asyncio
    async def test_recursive_listdir_webdav_flat(self, mock_webdav_client):
        """Test listing files in a flat WebDAV directory."""
        files = await webdav_app.recursive_listdir_webdav(mock_webdav_client, "/documents")

        assert isinstance(files, list)
        assert len(files) == 2
        assert all("path" in f and "size" in f for f in files)

    @pytest.mark.asyncio
    async def test_recursive_listdir_webdav_nested(self):
        """Test listing files in nested WebDAV directories."""
        mock_client = MagicMock()
        # First call returns a directory and a file
        # Second call (for subdirectory) returns files
        mock_client.ls.side_effect = [
            [
                {"name": "/documents", "type": "directory"},
                {"name": "/documents/subdir", "type": "directory"},
                {"name": "/documents/file1.md", "type": "file", "size": 100},
            ],
            [
                {"name": "/documents/subdir", "type": "directory"},
                {"name": "/documents/subdir/file2.md", "type": "file", "size": 200},
            ],
        ]

        files = await webdav_app.recursive_listdir_webdav(mock_client, "/documents")

        assert len(files) == 2  # file1.md and file2.md


class TestExportUrls:
    """Tests for export_urls_to_file function."""

    @pytest.mark.asyncio
    async def test_export_urls_to_file(self, tmp_path):
        """Test writing URLs to file with absolute path reconstruction."""
        config = [
            {"path": "report.md"},
            {"path": "sub/readme.pdf"},
        ]
        output_file = str(tmp_path / "urls.txt")

        count = await webdav_app.export_urls_to_file(config, "/documents", output_file)

        assert count == 2
        async with aiofiles.open(output_file) as f:
            content = await f.read()
        lines = [line for line in content.splitlines() if line.strip()]
        assert lines == ["/documents/report.md", "/documents/sub/readme.pdf"]

    @pytest.mark.asyncio
    async def test_export_urls_to_file_trailing_slash(self, tmp_path):
        """Test that trailing slashes on base_path are handled."""
        config = [{"path": "file.md"}]
        output_file = str(tmp_path / "urls.txt")

        await webdav_app.export_urls_to_file(config, "/documents/", output_file)

        async with aiofiles.open(output_file) as f:
            content = await f.read()
        assert "/documents/file.md" in content


class TestBuildConfigFromUrls:
    """Tests for build_config_from_urls function."""

    @pytest.mark.asyncio
    async def test_build_config_from_urls(self, tmp_path, mock_webdav_client):
        """Test building config from URL file."""
        urls_file = str(tmp_path / "urls.txt")
        async with aiofiles.open(urls_file, "w") as f:
            await f.write("/documents/test.md\n/documents/readme.pdf\n")

        with patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client):
            config = await webdav_app.build_config_from_urls(urls_file)

        assert len(config) == 2
        assert config[0]["path"] == "/documents/test.md"
        assert config[1]["path"] == "/documents/readme.pdf"
        assert all("sha256" in item for item in config)
        assert all("metadata" in item for item in config)
        assert all("size" in item["metadata"] for item in config)
        assert all("content-type" in item["metadata"] for item in config)

    @pytest.mark.asyncio
    async def test_build_config_from_urls_extension_filtering(self, tmp_path, mock_webdav_client):
        """Test that extension filtering works."""
        urls_file = str(tmp_path / "urls.txt")
        async with aiofiles.open(urls_file, "w") as f:
            await f.write("/documents/test.md\n/documents/archive.zip\n")

        with patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client):
            config = await webdav_app.build_config_from_urls(urls_file)

        # Only .md should pass (zip is not in default extensions)
        paths = [item["path"] for item in config]
        assert "/documents/test.md" in paths
        assert "/documents/archive.zip" not in paths

    @pytest.mark.asyncio
    async def test_build_config_from_urls_blank_lines(self, tmp_path, mock_webdav_client):
        """Test that blank lines are skipped."""
        urls_file = str(tmp_path / "urls.txt")
        async with aiofiles.open(urls_file, "w") as f:
            await f.write("/documents/test.md\n\n  \n/documents/readme.pdf\n")

        with patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client):
            config = await webdav_app.build_config_from_urls(urls_file)

        assert len(config) == 2


class TestValidateConfig:
    """Tests for validate_config function."""

    @pytest.mark.asyncio
    async def test_validate_config_with_webdav_path(self, mock_webdav_client, capsys):
        """Test validate_config with WebDAV path."""
        with (
            patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client),
            patch("soliplex.agents.webdav.app.build_config", new_callable=AsyncMock) as mock_build,
        ):
            mock_build.return_value = [
                {"path": "test.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}}
            ]

            await webdav_app.validate_config("/documents")

            captured = capsys.readouterr()
            assert "Total files: 1" in captured.out

    @pytest.mark.asyncio
    async def test_validate_config_with_export_urls(self, mock_webdav_client, capsys, tmp_path):
        """Test validate_config with export_urls parameter."""
        output_file = str(tmp_path / "exported.txt")
        with (
            patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client),
            patch("soliplex.agents.webdav.app.build_config", new_callable=AsyncMock) as mock_build,
        ):
            mock_build.return_value = [
                {"path": "test.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}}
            ]

            await webdav_app.validate_config("/documents", export_urls=output_file)

            captured = capsys.readouterr()
            assert "Exported 1 URLs" in captured.out
            async with aiofiles.open(output_file) as f:
                content = await f.read()
            assert "/documents/test.md" in content


class TestLoadInventory:
    """Tests for load_inventory function."""

    @pytest.mark.asyncio
    async def test_load_inventory_with_webdav_path(self, mock_webdav_client, monkeypatch):
        """Test load_inventory with WebDAV path."""
        from unittest.mock import AsyncMock

        # Mock client functions
        mock_check_status = AsyncMock(return_value=[])
        mock_find_batch = AsyncMock(return_value=None)
        mock_create_batch = AsyncMock(return_value=123)

        monkeypatch.setattr("soliplex.agents.client.check_status", mock_check_status)
        monkeypatch.setattr("soliplex.agents.client.find_batch_for_source", mock_find_batch)
        monkeypatch.setattr("soliplex.agents.client.create_batch", mock_create_batch)

        with (
            patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client),
            patch("soliplex.agents.webdav.app.build_config", new_callable=AsyncMock) as mock_build,
        ):
            mock_build.return_value = [
                {"path": "test.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}}
            ]

            result = await webdav_app.load_inventory(
                "/documents",
                "test-source",
                start_workflows=False,
            )

            assert "inventory" in result
            assert len(result["inventory"]) == 1

    @pytest.mark.asyncio
    async def test_load_inventory_with_prebuilt_config(self, mock_webdav_client, monkeypatch):
        """Test load_inventory with pre-built config skips build_config."""
        mock_check_status = AsyncMock(return_value=[])
        mock_find_batch = AsyncMock(return_value=None)
        mock_create_batch = AsyncMock(return_value=123)

        monkeypatch.setattr("soliplex.agents.client.check_status", mock_check_status)
        monkeypatch.setattr("soliplex.agents.client.find_batch_for_source", mock_find_batch)
        monkeypatch.setattr("soliplex.agents.client.create_batch", mock_create_batch)

        prebuilt_config = [
            {"path": "/docs/test.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}}
        ]

        with patch("soliplex.agents.webdav.app.build_config", new_callable=AsyncMock) as mock_build:
            result = await webdav_app.load_inventory(
                "",
                "test-source",
                start_workflows=False,
                config=prebuilt_config,
            )

            mock_build.assert_not_called()
            assert result["inventory"] == prebuilt_config


class TestLoadInventoryFromUrls:
    """Tests for load_inventory_from_urls function."""

    @pytest.mark.asyncio
    async def test_load_inventory_from_urls(self, mock_webdav_client, monkeypatch, tmp_path):
        """Test delegation to build_config_from_urls + load_inventory."""
        mock_check_status = AsyncMock(return_value=[])
        monkeypatch.setattr("soliplex.agents.client.check_status", mock_check_status)

        urls_file = str(tmp_path / "urls.txt")
        async with aiofiles.open(urls_file, "w") as f:
            await f.write("/documents/test.md\n")

        with patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client):
            result = await webdav_app.load_inventory_from_urls(
                urls_file,
                "test-source",
                start_workflows=False,
            )

        assert "inventory" in result
        assert len(result["inventory"]) == 1
        assert result["inventory"][0]["path"] == "/documents/test.md"


class TestStatusReport:
    """Tests for status_report function."""

    @pytest.mark.asyncio
    async def test_status_report_with_webdav_path(self, mock_webdav_client, monkeypatch, capsys):
        """Test status_report with WebDAV path."""
        from unittest.mock import AsyncMock

        mock_check_status = AsyncMock(return_value=[])
        monkeypatch.setattr("soliplex.agents.client.check_status", mock_check_status)

        with (
            patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client),
            patch("soliplex.agents.webdav.app.build_config", new_callable=AsyncMock) as mock_build,
        ):
            mock_build.return_value = [
                {"path": "test.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}}
            ]

            await webdav_app.status_report("/documents", "test-source")

            captured = capsys.readouterr()
            assert "Total files: 1" in captured.out
