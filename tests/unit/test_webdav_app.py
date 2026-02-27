"""Tests for soliplex.agents.webdav.app module."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

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
