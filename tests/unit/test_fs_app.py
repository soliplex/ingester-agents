"""Tests for soliplex.agents.fs.app module."""

import json
import tempfile
from pathlib import Path

import pytest

from soliplex.agents.fs import app as fs_app


@pytest.fixture
def temp_inventory_file():
    """Create a temporary inventory file for testing."""
    inventory = [
        {
            "path": "doc1.md",
            "sha256": "abc123",
            "metadata": {"size": 100, "content-type": "text/markdown"},
        },
        {
            "path": "doc2.pdf",
            "sha256": "def456",
            "metadata": {"size": 200, "content-type": "application/pdf"},
        },
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(inventory, f)
        temp_path = f.name
    yield temp_path
    Path(temp_path).unlink()


@pytest.fixture
def temp_document_dir():
    """Create a temporary directory with test documents."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        test_file = Path(tmpdir) / "test.md"
        test_file.write_text("# Test Document\n\nThis is a test.")

        readme_file = Path(tmpdir) / "readme.md"
        readme_file.write_text("# README\n\nProject description.")

        yield tmpdir


class TestResolveConfigPath:
    """Tests for resolve_config_path function."""

    @pytest.mark.asyncio
    async def test_resolve_config_path_with_file(self, temp_inventory_file):
        """Test resolve_config_path with a file path."""
        config, data_path = await fs_app.resolve_config_path(temp_inventory_file)

        assert isinstance(config, list)
        assert len(config) == 2
        assert config[0]["path"] == "doc1.md"
        assert config[1]["path"] == "doc2.pdf"
        assert data_path == Path(temp_inventory_file).parent

    @pytest.mark.asyncio
    async def test_resolve_config_path_with_directory(self, temp_document_dir):
        """Test resolve_config_path with a directory path."""
        config, data_path = await fs_app.resolve_config_path(temp_document_dir)

        assert isinstance(config, list)
        assert len(config) == 2
        # Config should be built from directory contents
        paths = [item["path"] for item in config]
        assert "test.md" in paths
        assert "readme.md" in paths
        assert data_path == Path(temp_document_dir)

    @pytest.mark.asyncio
    async def test_resolve_config_path_nonexistent(self):
        """Test resolve_config_path with non-existent path."""
        with pytest.raises(FileNotFoundError):
            await fs_app.resolve_config_path("/nonexistent/path")

    @pytest.mark.asyncio
    async def test_resolve_config_path_preserves_metadata(self, temp_document_dir):
        """Test that resolve_config_path preserves file metadata."""
        config, data_path = await fs_app.resolve_config_path(temp_document_dir)

        for item in config:
            assert "sha256" in item
            assert "metadata" in item
            assert "size" in item["metadata"]
            assert "content-type" in item["metadata"]


class TestValidateConfig:
    """Tests for validate_config function."""

    @pytest.mark.asyncio
    async def test_validate_config_with_file(self, temp_inventory_file, capsys):
        """Test validate_config with inventory file."""
        await fs_app.validate_config(temp_inventory_file)

        captured = capsys.readouterr()
        assert "Total files: 2" in captured.out
        assert temp_inventory_file in captured.out

    @pytest.mark.asyncio
    async def test_validate_config_with_directory(self, temp_document_dir, capsys):
        """Test validate_config with directory path."""
        await fs_app.validate_config(temp_document_dir)

        captured = capsys.readouterr()
        assert "Total files: 2" in captured.out
        assert temp_document_dir in captured.out


class TestLoadInventory:
    """Tests for load_inventory function."""

    @pytest.mark.asyncio
    async def test_load_inventory_with_file(self, temp_inventory_file, monkeypatch):
        """Test load_inventory with inventory file."""
        from unittest.mock import AsyncMock

        # Mock client functions
        mock_check_status = AsyncMock(return_value=[])
        mock_find_batch = AsyncMock(return_value=None)
        mock_create_batch = AsyncMock(return_value=123)

        monkeypatch.setattr("soliplex.agents.client.check_status", mock_check_status)
        monkeypatch.setattr("soliplex.agents.client.find_batch_for_source", mock_find_batch)
        monkeypatch.setattr("soliplex.agents.client.create_batch", mock_create_batch)

        result = await fs_app.load_inventory(
            temp_inventory_file,
            "test-source",
            start_workflows=False,
        )

        assert "inventory" in result
        assert len(result["inventory"]) == 2
        assert result["to_process"] == []

    @pytest.mark.asyncio
    async def test_load_inventory_with_directory(self, temp_document_dir, monkeypatch):
        """Test load_inventory with directory path."""
        from unittest.mock import AsyncMock

        # Mock client functions
        mock_check_status = AsyncMock(return_value=[])
        mock_find_batch = AsyncMock(return_value=None)
        mock_create_batch = AsyncMock(return_value=123)

        monkeypatch.setattr("soliplex.agents.client.check_status", mock_check_status)
        monkeypatch.setattr("soliplex.agents.client.find_batch_for_source", mock_find_batch)
        monkeypatch.setattr("soliplex.agents.client.create_batch", mock_create_batch)

        result = await fs_app.load_inventory(
            temp_document_dir,
            "test-source",
            start_workflows=False,
        )

        assert "inventory" in result
        assert len(result["inventory"]) == 2
        # Config should be built from directory
        paths = [item["path"] for item in result["inventory"]]
        assert "test.md" in paths
        assert "readme.md" in paths


class TestStatusReport:
    """Tests for status_report function."""

    @pytest.mark.asyncio
    async def test_status_report_with_file(self, temp_inventory_file, monkeypatch, capsys):
        """Test status_report with inventory file."""
        from unittest.mock import AsyncMock

        mock_check_status = AsyncMock(return_value=[])
        monkeypatch.setattr("soliplex.agents.client.check_status", mock_check_status)

        await fs_app.status_report(temp_inventory_file, "test-source")

        captured = capsys.readouterr()
        assert "Total files: 2" in captured.out
        assert "Files to process: 0" in captured.out

    @pytest.mark.asyncio
    async def test_status_report_with_directory(self, temp_document_dir, monkeypatch, capsys):
        """Test status_report with directory path."""
        from unittest.mock import AsyncMock

        mock_check_status = AsyncMock(return_value=[])
        monkeypatch.setattr("soliplex.agents.client.check_status", mock_check_status)

        await fs_app.status_report(temp_document_dir, "test-source")

        captured = capsys.readouterr()
        assert "Total files: 2" in captured.out
        assert "Files to process: 0" in captured.out
