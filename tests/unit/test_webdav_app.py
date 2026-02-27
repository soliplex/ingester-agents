"""Tests for soliplex.agents.webdav.app module."""

import json
from pathlib import Path
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


# --- create_webdav_client ---


def test_create_webdav_client_with_params():
    """Test creating WebDAV client with explicit parameters."""
    client = webdav_app.create_webdav_client(url="https://webdav.example.com", username="user", password="pass")
    assert client is not None


def test_create_webdav_client_no_url():
    """Test creating WebDAV client without URL raises error."""
    with patch("soliplex.agents.webdav.app.settings") as mock_settings:
        mock_settings.webdav_url = None
        with pytest.raises(ValueError, match="WebDAV URL is required"):
            webdav_app.create_webdav_client()


# --- build_config ---


@pytest.mark.asyncio
async def test_build_config(mock_webdav_client):
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


# --- recursive_listdir_webdav ---


@pytest.mark.asyncio
async def test_recursive_listdir_webdav_flat(mock_webdav_client):
    """Test listing files in a flat WebDAV directory."""
    files = await webdav_app.recursive_listdir_webdav(mock_webdav_client, "/documents")

    assert isinstance(files, list)
    assert len(files) == 2
    assert all("path" in f and "size" in f for f in files)


@pytest.mark.asyncio
async def test_recursive_listdir_webdav_nested():
    """Test listing files in nested WebDAV directories."""
    mock_client = MagicMock()
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


# --- list_config ---


@pytest.mark.asyncio
async def test_list_config_no_downloads(mock_webdav_client):
    """Test that list_config only lists files without downloading content."""
    with (
        patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client),
        patch("soliplex.agents.webdav.app.recursive_listdir_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = [
            {"path": "/documents/test.md", "size": 100},
            {"path": "/documents/readme.pdf", "size": 200},
        ]

        config = await webdav_app.list_config("/documents")

        assert len(config) == 2
        assert config[0]["path"] == "test.md" or config[0]["path"] == "readme.pdf"
        assert all("metadata" in item for item in config)
        assert all("content-type" in item["metadata"] for item in config)
        # No sha256 since no content was downloaded
        assert all("sha256" not in item for item in config)


# --- export_urls_to_file ---


@pytest.mark.asyncio
async def test_export_urls_to_file(tmp_path):
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
async def test_export_urls_to_file_trailing_slash(tmp_path):
    """Test that trailing slashes on base_path are handled."""
    config = [{"path": "file.md"}]
    output_file = str(tmp_path / "urls.txt")

    await webdav_app.export_urls_to_file(config, "/documents/", output_file)

    async with aiofiles.open(output_file) as f:
        content = await f.read()
    assert "/documents/file.md" in content


# --- build_config_from_urls ---


@pytest.mark.asyncio
async def test_build_config_from_urls(tmp_path, mock_webdav_client):
    """Test building config from URL file."""
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n/documents/readme.pdf\n")

    with patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client):
        config, results = await webdav_app.build_config_from_urls(urls_file)

    assert len(config) == 2
    assert config[0]["path"] == "/documents/test.md"
    assert config[1]["path"] == "/documents/readme.pdf"
    assert all("sha256" in item for item in config)
    assert all("metadata" in item for item in config)
    assert all("size" in item["metadata"] for item in config)
    assert all("content-type" in item["metadata"] for item in config)
    assert len(results) == 2
    assert all(r["status"] == "success" for r in results)


@pytest.mark.asyncio
async def test_build_config_from_urls_extension_filtering(tmp_path, mock_webdav_client):
    """Test that extension filtering returns skipped status."""
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n/documents/archive.zip\n")

    with patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client):
        config, results = await webdav_app.build_config_from_urls(urls_file)

    # Only .md should pass (zip is not in default extensions)
    paths = [item["path"] for item in config]
    assert "/documents/test.md" in paths
    assert "/documents/archive.zip" not in paths
    assert len(results) == 2
    assert results[0]["status"] == "success"
    assert results[1]["status"] == "skipped"


@pytest.mark.asyncio
async def test_build_config_from_urls_blank_lines(tmp_path, mock_webdav_client):
    """Test that blank lines are skipped."""
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n\n  \n/documents/readme.pdf\n")

    with patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_webdav_client):
        config, results = await webdav_app.build_config_from_urls(urls_file)

    assert len(config) == 2
    assert len(results) == 2


@pytest.mark.asyncio
async def test_build_config_from_urls_download_error(tmp_path):
    """Test that a download error is captured per-URL without stopping."""
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/good.md\n/documents/bad.md\n/documents/also_good.pdf\n")

    mock_client = MagicMock()
    call_count = 0

    def mock_download_fileobj(path, buffer):
        nonlocal call_count
        call_count += 1
        if "bad.md" in path:
            raise ConnectionError("Server unavailable")
        buffer.write(b"test content")

    mock_client.download_fileobj = mock_download_fileobj

    with patch("soliplex.agents.webdav.app.create_webdav_client", return_value=mock_client):
        config, results = await webdav_app.build_config_from_urls(urls_file)

    assert len(config) == 2  # good.md and also_good.pdf
    assert len(results) == 3
    assert results[0]["status"] == "success"
    assert results[1]["status"] == "error"
    assert "Server unavailable" in results[1]["error_message"]
    assert results[2]["status"] == "success"


# --- validate_config ---


@pytest.mark.asyncio
async def test_validate_config_with_webdav_path(mock_webdav_client, capsys):
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


# --- export_urls (standalone command) ---


@pytest.mark.asyncio
async def test_export_urls_uses_list_config(capsys, tmp_path):
    """Test export_urls uses list_config (no downloads) and writes file."""
    output_file = str(tmp_path / "exported.txt")
    with patch("soliplex.agents.webdav.app.list_config", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = [
            {"path": "test.md", "metadata": {"size": 100, "content-type": "text/markdown"}},
            {"path": "sub/readme.pdf", "metadata": {"size": 200, "content-type": "application/pdf"}},
        ]

        await webdav_app.export_urls("/documents", output_file)

        mock_list.assert_called_once_with("/documents", None, None, None)
        captured = capsys.readouterr()
        assert "Found 2 files" in captured.out
        assert "Exported 2 URLs" in captured.out
        async with aiofiles.open(output_file) as f:
            content = await f.read()
        lines = [line for line in content.splitlines() if line.strip()]
        assert lines == ["/documents/test.md", "/documents/sub/readme.pdf"]


# --- load_inventory ---


@pytest.mark.asyncio
async def test_load_inventory_with_webdav_path(mock_webdav_client, monkeypatch):
    """Test load_inventory with WebDAV path."""
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
async def test_load_inventory_with_prebuilt_config(monkeypatch):
    """Test load_inventory with pre-built config skips build_config."""
    mock_check_status = AsyncMock(return_value=[])
    mock_find_batch = AsyncMock(return_value=None)
    mock_create_batch = AsyncMock(return_value=123)

    monkeypatch.setattr("soliplex.agents.client.check_status", mock_check_status)
    monkeypatch.setattr("soliplex.agents.client.find_batch_for_source", mock_find_batch)
    monkeypatch.setattr("soliplex.agents.client.create_batch", mock_create_batch)

    prebuilt_config = [{"path": "/docs/test.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}}]

    with patch("soliplex.agents.webdav.app.build_config", new_callable=AsyncMock) as mock_build:
        result = await webdav_app.load_inventory(
            "",
            "test-source",
            start_workflows=False,
            config=prebuilt_config,
        )

        mock_build.assert_not_called()
        assert result["inventory"] == prebuilt_config


# --- load_inventory_from_urls ---


@pytest.mark.asyncio
async def test_load_inventory_from_urls(mock_webdav_client, monkeypatch, tmp_path):
    """Test delegation to build_config_from_urls + load_inventory with results file."""
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
    # Check url_results are included in the return value
    assert "url_results" in result
    assert len(result["url_results"]) == 1
    assert result["url_results"][0]["status"] == "success"
    # Check results file was written
    assert "url_results_path" in result
    results_path = result["url_results_path"]
    assert Path(results_path).exists()
    async with aiofiles.open(results_path) as f:
        written = json.loads(await f.read())
    assert len(written) == 1
    assert written[0]["url"] == "/documents/test.md"


@pytest.mark.asyncio
async def test_load_inventory_from_urls_skip_hash_check(monkeypatch, tmp_path):
    """Test skip_hash_check skips downloads and status check."""
    mock_find_batch = AsyncMock(return_value=42)
    mock_do_ingest = AsyncMock(return_value={"id": 1})

    monkeypatch.setattr("soliplex.agents.client.find_batch_for_source", mock_find_batch)
    monkeypatch.setattr("soliplex.agents.webdav.app.do_ingest", mock_do_ingest)

    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n/documents/report.pdf\n/documents/archive.zip\n")

    result = await webdav_app.load_inventory_from_urls(
        urls_file,
        "test-source",
        start_workflows=False,
        skip_hash_check=True,
    )

    # zip should be filtered out, 2 files should be ingested
    assert len(result["inventory"]) == 2
    assert len(result["to_process"]) == 2
    assert len(result["ingested"]) == 2
    assert mock_do_ingest.call_count == 2
    # check_status should not have been called
    assert "url_results" in result
    assert len(result["url_results"]) == 3
    assert result["url_results"][2]["status"] == "skipped"
    # Results file should exist
    assert Path(result["url_results_path"]).exists()


# --- _read_urls_as_config ---


@pytest.mark.asyncio
async def test_read_urls_as_config(tmp_path):
    """Test lightweight URL reading without downloads."""
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n/documents/archive.zip\n\n/documents/readme.pdf\n")

    config, results = await webdav_app._read_urls_as_config(urls_file)

    # zip is filtered out
    assert len(config) == 2
    assert config[0]["path"] == "/documents/test.md"
    assert config[1]["path"] == "/documents/readme.pdf"
    assert all("content-type" in item["metadata"] for item in config)
    # No sha256 or size since nothing was downloaded
    assert all("sha256" not in item for item in config)
    # Results track all 3 non-blank lines
    assert len(results) == 3
    assert results[0]["status"] == "success"
    assert results[1]["status"] == "skipped"
    assert results[2]["status"] == "success"


# --- load_inventory with skip_status_check ---


@pytest.mark.asyncio
async def test_load_inventory_skip_status_check(monkeypatch):
    """Test load_inventory with skip_status_check treats all files as needing processing."""
    mock_check_status = AsyncMock(return_value=[])
    mock_find_batch = AsyncMock(return_value=42)
    mock_do_ingest = AsyncMock(return_value={"id": 1})

    monkeypatch.setattr("soliplex.agents.client.check_status", mock_check_status)
    monkeypatch.setattr("soliplex.agents.client.find_batch_for_source", mock_find_batch)
    monkeypatch.setattr("soliplex.agents.webdav.app.do_ingest", mock_do_ingest)

    config = [
        {"path": "/docs/a.md", "metadata": {"content-type": "text/markdown"}},
        {"path": "/docs/b.pdf", "metadata": {"content-type": "application/pdf"}},
    ]

    result = await webdav_app.load_inventory(
        "",
        "test-source",
        start_workflows=False,
        config=config,
        skip_status_check=True,
    )

    # check_status should NOT have been called
    mock_check_status.assert_not_called()
    # All files should be processed
    assert len(result["to_process"]) == 2
    assert len(result["ingested"]) == 2
    assert mock_do_ingest.call_count == 2


# --- status_report ---


@pytest.mark.asyncio
async def test_status_report_with_webdav_path(mock_webdav_client, monkeypatch, capsys):
    """Test status_report with WebDAV path."""
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
