"""Tests for soliplex.agents.fs.app module."""

import json
import tempfile
from pathlib import Path

import pytest

from soliplex.agents import local_state
from soliplex.agents import local_store
from soliplex.agents import store as agent_store
from soliplex.agents.fs import app as fs_app


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """Point download_dir and state_dir at temp directories."""
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path / "dl"))
    monkeypatch.setattr(local_state.settings, "state_dir", str(tmp_path / "state"))
    return tmp_path


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
    async def test_resolve_config_path_with_file(self, tmp_path):
        """A file path is rejected; only directories are scanned."""
        target = tmp_path / "doc1.md"
        target.write_text("hello", encoding="utf-8")

        with pytest.raises(NotADirectoryError):
            await fs_app.resolve_config_path(str(target))

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
    async def test_validate_config_with_directory(self, temp_document_dir, capsys):
        """Test validate_config with directory path."""
        await fs_app.validate_config(temp_document_dir)

        captured = capsys.readouterr()
        assert "Total files: 2" in captured.out
        assert temp_document_dir in captured.out


class TestLoadInventory:
    """Tests for load_inventory function."""

    @pytest.mark.asyncio
    async def test_load_inventory_writes_files(self, temp_document_dir, local_env):
        """Files from a source directory are written under download_dir with sidecars."""
        result = await fs_app.load_inventory(temp_document_dir, "fs-src")

        assert len(result["inventory"]) == 2
        assert set(result["ingested"]) == {"test.md", "readme.md"}
        assert result["errors"] == []

        sd = local_store.source_dir("fs-src")
        assert (sd / "test.md").read_text() == "# Test Document\n\nThis is a test."
        assert (sd / "test.md.meta.json").exists()
        assert (sd / "readme.md").exists()
        meta = json.loads((sd / "test.md.meta.json").read_text())
        assert meta["ingestion_type"] == "fs"
        assert "source_url" not in meta

    @pytest.mark.asyncio
    async def test_load_inventory_skips_unchanged(self, temp_document_dir, local_env):
        """A second run with unchanged content writes nothing new."""
        await fs_app.load_inventory(temp_document_dir, "fs-src")
        result = await fs_app.load_inventory(temp_document_dir, "fs-src")

        assert result["to_process"] == []
        assert result["ingested"] == []

    @pytest.mark.asyncio
    async def test_load_inventory_delete_stale(self, temp_document_dir, local_env):
        """delete_stale removes documents no longer present in the source."""
        await fs_app.load_inventory(temp_document_dir, "fs-src")
        # Remove a source file, then re-run with delete_stale.
        (Path(temp_document_dir) / "readme.md").unlink()
        result = await fs_app.load_inventory(temp_document_dir, "fs-src", delete_stale=True)

        assert result["delete_stale_result"] == ["readme.md"]
        assert not (local_store.source_dir("fs-src") / "readme.md").exists()


class TestStatusReport:
    """Tests for status_report function."""

    @pytest.mark.asyncio
    async def test_status_report_with_directory(self, temp_document_dir, local_env, capsys):
        """Test status_report with directory path."""
        await fs_app.status_report(temp_document_dir, "fs-src")

        captured = capsys.readouterr()
        assert "Total files: 2" in captured.out
        assert "Files to process: 2" in captured.out
