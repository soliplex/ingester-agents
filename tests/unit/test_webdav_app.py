"""Tests for soliplex.agents.webdav.app module."""

import hashlib
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import aiofiles
import pytest

from soliplex.agents import local_state
from soliplex.agents import local_store
from soliplex.agents.webdav import app as webdav_app
from soliplex.agents.webdav.async_client import WebDAVResponse


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """Point download_dir and state_dir at temp directories."""
    monkeypatch.setattr(local_store.settings, "download_dir", str(tmp_path / "dl"))
    monkeypatch.setattr(local_state.settings, "state_dir", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def mock_webdav_client():
    """Create a mock async WebDAV client."""
    client = AsyncMock()
    client.ls.return_value = [
        {"name": "test.md", "type": "file", "size": 100, "etag": '"etag1"', "content_length": 100},
        {"name": "readme.pdf", "type": "file", "size": 200, "etag": '"etag2"', "content_length": 200},
    ]
    client.download.return_value = (b"test content", "text/markdown")
    client.info.return_value = {"etag": '"etag_info"'}
    client.head.return_value = WebDAVResponse(status=200, headers={"etag": '"etag_head"'})
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# --- build_config ---


@pytest.mark.asyncio
async def test_build_config(mock_webdav_client, local_env):
    """No cached state → sha256 deferred (None)."""
    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_webdav_client),
        patch("soliplex.agents.webdav.app.recursive_listdir_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = [
            {"path": "/documents/test.md", "size": 100},
            {"path": "/documents/readme.pdf", "size": 200},
        ]

        config = await webdav_app.build_config("/documents")

    assert len(config) == 2
    assert config[0]["path"] in ("test.md", "readme.pdf")
    assert all(item["sha256"] is None for item in config)
    assert all("metadata" in item for item in config)


@pytest.mark.asyncio
async def test_build_config_etag_cache_hit(local_env):
    """Matching ETag in local state skips download and reuses cached SHA256."""
    local_state.upsert_file("s", "test.md", "cached_hash_abc", etag='"etag1"', size=100)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.download.side_effect = AssertionError("Should not download")

    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client),
        patch("soliplex.agents.webdav.app.recursive_listdir_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = [{"path": "/documents/test.md", "size": 100, "etag": '"etag1"'}]
        config = await webdav_app.build_config("/documents", source="s")

    assert len(config) == 1
    assert config[0]["sha256"] == "cached_hash_abc"
    assert config[0]["path"] == "test.md"


@pytest.mark.asyncio
async def test_build_config_etag_cache_miss(local_env):
    """Mismatched ETag defers download (sha256=None) and carries the new etag."""
    local_state.upsert_file("s", "test.md", "old_hash", etag='"old_etag"')

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.download.side_effect = AssertionError("Should not download")

    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client),
        patch("soliplex.agents.webdav.app.recursive_listdir_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = [{"path": "/documents/test.md", "size": 100, "etag": '"new_etag"'}]
        config = await webdav_app.build_config("/documents", source="s")

    assert config[0]["sha256"] is None
    assert config[0]["_etag"] == '"new_etag"'


@pytest.mark.asyncio
async def test_build_config_no_etag_from_server(local_env):
    """Missing server ETag → sha256=None and no _etag recorded."""
    local_state.upsert_file("s", "test.md", "cached_hash", etag='"cached_etag"')

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.download.side_effect = AssertionError("Should not download")
    mock_client.head.return_value = WebDAVResponse(status=200, headers={})

    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client),
        patch("soliplex.agents.webdav.app.recursive_listdir_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = [{"path": "/documents/test.md", "size": 100}]
        config = await webdav_app.build_config("/documents", source="s")

    assert config[0]["sha256"] is None
    assert "_etag" not in config[0]


@pytest.mark.asyncio
async def test_build_config_no_downloads_on_cache_miss(local_env):
    """build_config never downloads; it defers to the write step."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.download.side_effect = AssertionError("Should not download")
    # head() returns a response with a real (sync) headers mapping; a bare
    # AsyncMock would make headers.get(...) an un-awaited coroutine.
    head_resp = MagicMock()
    head_resp.headers = {}
    mock_client.head = AsyncMock(return_value=head_resp)

    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client),
        patch("soliplex.agents.webdav.app.recursive_listdir_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = [
            {"path": "/documents/good.md", "size": 100},
            {"path": "/documents/also_good.pdf", "size": 300},
        ]
        config = await webdav_app.build_config("/documents")

    assert len(config) == 2
    assert all(item["sha256"] is None for item in config)


# --- recursive_listdir_webdav ---


@pytest.mark.asyncio
async def test_recursive_listdir_webdav_flat(mock_webdav_client):
    files = await webdav_app.recursive_listdir_webdav(mock_webdav_client, "/documents")
    assert len(files) == 2
    assert all("path" in f and "size" in f for f in files)
    assert sorted(f["path"] for f in files) == ["/documents/readme.pdf", "/documents/test.md"]
    assert all("etag" in f for f in files)


@pytest.mark.asyncio
async def test_recursive_listdir_webdav_nested():
    mock_client = AsyncMock()
    mock_client.ls = AsyncMock(
        side_effect=[
            [
                {"name": "subdir", "type": "directory"},
                {"name": "file1.md", "type": "file", "size": 100, "content_length": 100},
            ],
            [
                {"name": "file2.md", "type": "file", "size": 200, "content_length": 200},
            ],
        ]
    )

    files = await webdav_app.recursive_listdir_webdav(mock_client, "/documents")

    second_call_path = mock_client.ls.call_args_list[1].args[0]
    assert second_call_path == "/documents/subdir"
    paths = sorted(f["path"] for f in files)
    assert paths == ["/documents/file1.md", "/documents/subdir/file2.md"]


@pytest.mark.asyncio
async def test_recursive_listdir_webdav_reraises_timeout():
    mock_client = AsyncMock()
    mock_client.ls.side_effect = TimeoutError("Connection timed out")
    with pytest.raises(TimeoutError):
        await webdav_app.recursive_listdir_webdav(mock_client, "/documents")


@pytest.mark.asyncio
async def test_recursive_listdir_webdav_reraises_connection_error():
    mock_client = AsyncMock()
    mock_client.ls.side_effect = ConnectionError("Connection refused")
    with pytest.raises(ConnectionError, match="Connection refused"):
        await webdav_app.recursive_listdir_webdav(mock_client, "/documents")


@pytest.mark.asyncio
async def test_recursive_listdir_webdav_swallows_other_errors():
    mock_client = AsyncMock()
    mock_client.ls.side_effect = PermissionError("Access denied")
    files = await webdav_app.recursive_listdir_webdav(mock_client, "/documents")
    assert files == []


# --- list_config ---


@pytest.mark.asyncio
async def test_list_config_no_downloads(mock_webdav_client):
    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_webdav_client),
        patch("soliplex.agents.webdav.app.recursive_listdir_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = [
            {"path": "/documents/test.md", "size": 100},
            {"path": "/documents/readme.pdf", "size": 200},
        ]
        config = await webdav_app.list_config("/documents")

    assert len(config) == 2
    assert all("metadata" in item for item in config)
    assert all("content-type" in item["metadata"] for item in config)
    assert all("sha256" not in item for item in config)


# --- export_urls_to_file ---


@pytest.mark.asyncio
async def test_export_urls_to_file(tmp_path):
    config = [{"path": "report.md"}, {"path": "sub/readme.pdf"}]
    output_file = str(tmp_path / "urls.txt")
    count = await webdav_app.export_urls_to_file(config, "/documents", output_file)
    assert count == 2
    async with aiofiles.open(output_file) as f:
        content = await f.read()
    lines = [line for line in content.splitlines() if line.strip()]
    assert lines == ["/documents/report.md", "/documents/sub/readme.pdf"]


@pytest.mark.asyncio
async def test_export_urls_to_file_trailing_slash(tmp_path):
    config = [{"path": "file.md"}]
    output_file = str(tmp_path / "urls.txt")
    await webdav_app.export_urls_to_file(config, "/documents/", output_file)
    async with aiofiles.open(output_file) as f:
        content = await f.read()
    assert "/documents/file.md" in content


# --- build_config_from_urls ---


@pytest.mark.asyncio
async def test_build_config_from_urls(tmp_path, mock_webdav_client, local_env):
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n/documents/readme.pdf\n")

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_webdav_client):
        config, results = await webdav_app.build_config_from_urls(urls_file)

    assert len(config) == 2
    assert config[0]["path"] == "/documents/test.md"
    assert all(item["sha256"] is None for item in config)
    assert all("_etag" in item for item in config)
    assert len(results) == 2
    assert all(r["status"] == "success" for r in results)


@pytest.mark.asyncio
async def test_build_config_from_urls_extension_filtering(tmp_path, mock_webdav_client, local_env):
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n/documents/archive.zip\n")

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_webdav_client):
        config, results = await webdav_app.build_config_from_urls(urls_file)

    paths = [item["path"] for item in config]
    assert "/documents/test.md" in paths
    assert "/documents/archive.zip" not in paths
    assert results[0]["status"] == "success"
    assert results[1]["status"] == "skipped"


@pytest.mark.asyncio
async def test_build_config_from_urls_blank_lines(tmp_path, mock_webdav_client, local_env):
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n\n  \n/documents/readme.pdf\n")

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_webdav_client):
        config, results = await webdav_app.build_config_from_urls(urls_file)

    assert len(config) == 2
    assert len(results) == 2


@pytest.mark.asyncio
async def test_build_config_from_urls_info_error_all_succeed(tmp_path, local_env):
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/good.md\n/documents/also_good.pdf\n")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.info.side_effect = Exception("info failed")
    mock_client.head.side_effect = Exception("HEAD failed")

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client):
        config, results = await webdav_app.build_config_from_urls(urls_file)

    assert len(config) == 2
    assert all(r["status"] == "success" for r in results)
    assert all(item["sha256"] is None for item in config)
    assert all("_etag" not in item for item in config)


@pytest.mark.asyncio
async def test_build_config_from_urls_etag_cache_hit(tmp_path, local_env):
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n")

    local_state.upsert_file("s", "/documents/test.md", "cached_hash", etag='"cached_etag"', size=42)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.info.return_value = {"etag": '"cached_etag"'}
    mock_client.download.side_effect = AssertionError("Should not download")

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client):
        config, results = await webdav_app.build_config_from_urls(urls_file, source="s")

    assert config[0]["sha256"] == "cached_hash"
    assert results[0]["status"] == "success"


@pytest.mark.asyncio
async def test_build_config_from_urls_info_error_no_download(tmp_path, local_env):
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.info.side_effect = Exception("PROPFIND failed")
    mock_client.head.side_effect = Exception("HEAD failed")
    mock_client.download.side_effect = AssertionError("Should not download")

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client):
        config, results = await webdav_app.build_config_from_urls(urls_file)

    assert config[0]["sha256"] is None
    assert "_etag" not in config[0]
    assert results[0]["status"] == "success"


# --- validate_config / export_urls ---


@pytest.mark.asyncio
async def test_validate_config_with_webdav_path(capsys):
    with patch("soliplex.agents.webdav.app.build_config", new_callable=AsyncMock) as mock_build:
        mock_build.return_value = [
            {"path": "test.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}}
        ]
        await webdav_app.validate_config("/documents")
        captured = capsys.readouterr()
        assert "Total files: 1" in captured.out


@pytest.mark.asyncio
async def test_export_urls_uses_list_config(capsys, tmp_path):
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


# --- load_inventory ---


@pytest.mark.asyncio
async def test_load_inventory_with_webdav_path(local_env):
    with (
        patch("soliplex.agents.webdav.app.build_config", new_callable=AsyncMock) as mock_build,
        patch("soliplex.agents.webdav.app.do_ingest", new_callable=AsyncMock, return_value={"result": "success"}),
    ):
        mock_build.return_value = [
            {"path": "test.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}}
        ]
        result = await webdav_app.load_inventory("/documents", "test-source")

    assert len(result["inventory"]) == 1
    assert result["ingested"] == ["test.md"]


@pytest.mark.asyncio
async def test_load_inventory_with_prebuilt_config(local_env):
    prebuilt = [{"path": "/docs/test.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}}]
    with (
        patch("soliplex.agents.webdav.app.build_config", new_callable=AsyncMock) as mock_build,
        patch("soliplex.agents.webdav.app.do_ingest", new_callable=AsyncMock, return_value={"result": "success"}),
    ):
        result = await webdav_app.load_inventory("", "test-source", config=prebuilt)
        mock_build.assert_not_called()
    assert result["inventory"] == prebuilt


@pytest.mark.asyncio
async def test_load_inventory_processes_all_new(local_env):
    """Fresh state → every config row is processed."""
    config = [
        {"path": "cached.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}},
        {"path": "uncached.md", "sha256": None, "_etag": '"etag1"', "metadata": {"size": 0, "content-type": "text/markdown"}},
    ]
    with patch("soliplex.agents.webdav.app.do_ingest", new_callable=AsyncMock, return_value={"result": "success"}):
        result = await webdav_app.load_inventory("", "test-source", config=config)
    assert len(result["to_process"]) == 2


@pytest.mark.asyncio
async def test_load_inventory_passes_etag_to_do_ingest(local_env):
    """The _etag from a config record is forwarded to do_ingest."""
    config = [
        {
            "path": "file.md",
            "sha256": None,
            "_etag": '"etag_value"',
            "metadata": {"size": 0, "content-type": "text/markdown"},
        },
    ]
    with patch(
        "soliplex.agents.webdav.app.do_ingest",
        new_callable=AsyncMock,
        return_value={"result": "success"},
    ) as mock_ingest:
        await webdav_app.load_inventory("", "test-source", config=config)

    assert mock_ingest.call_args.kwargs["etag"] == '"etag_value"'


# --- do_ingest ---


@pytest.mark.asyncio
async def test_do_ingest_returns_error_on_download_failure(local_env):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.download.side_effect = TimeoutError("Connection timed out")

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client):
        result = await webdav_app.do_ingest(
            base_path="/webdav/docs",
            uri="test.md",
            meta={},
            source="test-source",
            mime_type="text/markdown",
            webdav_url="http://dav",
        )

    assert "error" in result
    assert "Connection timed out" in result["error"]
    assert "_sha256" not in result


@pytest.mark.asyncio
async def test_do_ingest_returns_sha256_on_success(local_env):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.download.return_value = (b"file content", None)
    mock_client.head.return_value = WebDAVResponse(status=200, headers={})

    expected_sha = hashlib.sha256(b"file content", usedforsecurity=False).hexdigest()

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client):
        result = await webdav_app.do_ingest(
            base_path="/webdav/docs",
            uri="test.md",
            meta={},
            source="test-source",
            mime_type="text/markdown",
            webdav_url="http://dav",
        )

    assert result["_sha256"] == expected_sha
    assert result["_size"] == len(b"file content")
    # file written under the source folder and state updated
    assert (local_store.source_dir("test-source") / "test.md").read_bytes() == b"file content"
    assert local_state.load_file_state("test-source")["test.md"]["sha256"] == expected_sha


@pytest.mark.asyncio
async def test_do_ingest_local_file_returns_sha256(tmp_path, local_env):
    test_file = tmp_path / "test.md"
    test_file.write_bytes(b"local content")
    expected_sha = hashlib.sha256(b"local content", usedforsecurity=False).hexdigest()

    result = await webdav_app.do_ingest(
        base_path=str(tmp_path),
        uri="test.md",
        meta={},
        source="test-source",
        mime_type="text/markdown",
    )

    assert result["_sha256"] == expected_sha
    assert result["_size"] == len(b"local content")


# --- load_inventory_from_urls ---


@pytest.mark.asyncio
async def test_load_inventory_from_urls(mock_webdav_client, tmp_path, local_env):
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n")

    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_webdav_client),
        patch(
            "soliplex.agents.webdav.app.do_ingest",
            new_callable=AsyncMock,
            return_value={"result": "success", "_sha256": "abc", "_size": 42},
        ),
    ):
        result = await webdav_app.load_inventory_from_urls(urls_file, "test-source")

    assert len(result["inventory"]) == 1
    assert result["inventory"][0]["path"] == "/documents/test.md"
    assert result["url_results"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_load_inventory_from_urls_updates_state(tmp_path, local_env):
    """A real do_ingest run writes the file and records local state."""
    urls_file = str(tmp_path / "urls.txt")
    async with aiofiles.open(urls_file, "w") as f:
        await f.write("/documents/test.md\n")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.info.return_value = {"etag": '"new_etag"'}
    mock_client.download.return_value = (b"downloaded", None)
    mock_client.head.return_value = WebDAVResponse(status=200, headers={})

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client):
        await webdav_app.load_inventory_from_urls(urls_file, "test-source")

    state = local_state.load_file_state("test-source")
    assert "/documents/test.md" in state
    assert state["/documents/test.md"]["sha256"] == hashlib.sha256(b"downloaded", usedforsecurity=False).hexdigest()
