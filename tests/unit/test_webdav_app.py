"""Tests for soliplex.agents.webdav.app module."""

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch
from xml.etree.ElementTree import ParseError

import aiofiles
import pytest

from soliplex.agents import WebDAVListingError
from soliplex.agents import local_state
from soliplex.agents import local_store
from soliplex.agents import store as agent_store
from soliplex.agents.webdav import app as webdav_app
from soliplex.agents.webdav.async_client import AsyncWebDAVClient
from soliplex.agents.webdav.async_client import ClientError
from soliplex.agents.webdav.async_client import InsufficientStorage
from soliplex.agents.webdav.async_client import ResourceNotFound
from soliplex.agents.webdav.async_client import RetryableHTTPError
from soliplex.agents.webdav.async_client import WebDAVResponse


@pytest.fixture(autouse=True)
def _no_listing_retry_delay(monkeypatch):
    """Retry failed subtrees without the production pause."""
    monkeypatch.setattr(webdav_app.settings, "webdav_listing_retry_delay", 0)


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """Point download_dir and state_dir at temp directories."""
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path / "dl"))
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
        patch("soliplex.agents.webdav.app.walk_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = webdav_app.ListingResult(
            [
                {"path": "/documents/test.md", "size": 100},
                {"path": "/documents/readme.pdf", "size": 200},
            ]
        )

        config, _ = await webdav_app.build_config("/documents")

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
        patch("soliplex.agents.webdav.app.walk_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = webdav_app.ListingResult([{"path": "/documents/test.md", "size": 100, "etag": '"etag1"'}])
        config, _ = await webdav_app.build_config("/documents", source="s")

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
        patch("soliplex.agents.webdav.app.walk_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = webdav_app.ListingResult([{"path": "/documents/test.md", "size": 100, "etag": '"new_etag"'}])
        config, _ = await webdav_app.build_config("/documents", source="s")

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
        patch("soliplex.agents.webdav.app.walk_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = webdav_app.ListingResult([{"path": "/documents/test.md", "size": 100}])
        config, _ = await webdav_app.build_config("/documents", source="s")

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
        patch("soliplex.agents.webdav.app.walk_webdav", new_callable=AsyncMock) as mock_ls,
    ):
        mock_ls.return_value = webdav_app.ListingResult(
            [
                {"path": "/documents/good.md", "size": 100},
                {"path": "/documents/also_good.pdf", "size": 300},
            ]
        )
        config, _ = await webdav_app.build_config("/documents")

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
async def test_recursive_listdir_webdav_raises_on_root_failure():
    """An unexpected error at the root surfaces; it never becomes an empty listing."""
    mock_client = AsyncMock()
    mock_client.ls.side_effect = PermissionError("Access denied")
    with pytest.raises(PermissionError):
        await webdav_app.recursive_listdir_webdav(mock_client, "/documents")


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
async def test_build_config_from_urls_drops_invalid_lines(tmp_path, mock_webdav_client, local_env):
    # Only absolute paths are WebDAV paths: comments, full URLs and relative
    # paths never reach the client.
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("# list\n/documents/test.md\nhttps://dav/x.md\ndocs/y.md\n", encoding="utf-8")

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_webdav_client):
        config, results = await webdav_app.build_config_from_urls(str(urls_file))

    assert [item["path"] for item in config] == ["/documents/test.md"]
    assert [r["url"] for r in results] == ["/documents/test.md"]


@pytest.mark.asyncio
async def test_build_config_from_urls_html_raises(tmp_path, mock_webdav_client, local_env):
    from soliplex.agents import UrlsFileFormatError

    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("<!DOCTYPE html>\n<html><body>Sign in</body></html>\n", encoding="utf-8")

    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_webdav_client) as mock_create,
        pytest.raises(UrlsFileFormatError, match="returned HTML"),
    ):
        await webdav_app.build_config_from_urls(str(urls_file))

    mock_create.assert_not_called()


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
        mock_build.return_value = (
            [{"path": "test.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}}],
            [],
        )
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
        mock_list.assert_called_once_with("/documents", None, None, None, exclude_paths=None)
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
        mock_build.return_value = (
            [{"path": "test.md", "sha256": "abc", "metadata": {"size": 100, "content-type": "text/markdown"}}],
            [],
        )
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
async def test_do_ingest_returns_not_found_on_404(local_env):
    from soliplex.agents.webdav.async_client import ResourceNotFound

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.download.side_effect = ResourceNotFound("/webdav/docs/gone.md")

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client):
        result = await webdav_app.do_ingest(
            base_path="/webdav/docs",
            uri="gone.md",
            meta={},
            source="test-source",
            mime_type="text/markdown",
            webdav_url="http://dav",
        )

    assert result == {"not_found": True, "uri": "gone.md"}
    assert "error" not in result


@pytest.mark.asyncio
async def test_load_inventory_404_deletes_when_delete_stale(local_env):
    # A previously-downloaded file that 404s on this run is removed from disk
    # and state (via reconcile), and reported in not_found rather than errors.
    source = "wd-src"
    await local_store.write_document(source, "gone.md", b"old", "text/markdown", {})
    local_state.upsert_file(source, "gone.md", None, mime_type="text/markdown")
    assert (local_store.source_dir(source) / "gone.md").exists()

    from soliplex.agents.webdav.async_client import ResourceNotFound

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.download.side_effect = ResourceNotFound("/gone.md")
    mock_client.head.return_value = WebDAVResponse(status=200, headers={})

    # The listing still shows the file (race); sha256=None forces reprocessing.
    config = [{"path": "gone.md", "sha256": None, "metadata": {"content-type": "text/markdown"}}]

    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=mock_client):
        result = await webdav_app.load_inventory("", source, config=config, webdav_url="http://dav", delete_stale=True)

    assert result["not_found"] == ["gone.md"]
    assert result["errors"] == []
    assert not (local_store.source_dir(source) / "gone.md").exists()
    assert "gone.md" not in local_state.load_file_state(source)


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
    target = local_store.source_dir("test-source") / "test.md"
    assert target.read_bytes() == b"file content"
    assert local_state.load_file_state("test-source")["test.md"]["sha256"] == expected_sha
    # the sidecar records the webdav ingestion type and the full download URL
    sidecar = json.loads((target.parent / "test.md.meta.json").read_text())
    assert sidecar["ingestion_type"] == "webdav"
    assert sidecar["source_url"] == "http://dav/webdav/docs/test.md"
    assert sidecar["downloaded_time"]


@pytest.mark.asyncio
async def test_do_ingest_without_url_omits_source_url(local_env):
    """An injected client with no configured URL yields no download URL.

    ``source_url`` is built from ``webdav_url``, so a caller that supplies its
    own client and no URL leaves the sidecar without one rather than recording
    a half-formed path.
    """
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.download.return_value = (b"file content", None)
    expected_sha = hashlib.sha256(b"file content", usedforsecurity=False).hexdigest()

    result = await webdav_app.do_ingest(
        base_path="/webdav/docs",
        uri="test.md",
        meta={},
        source="test-source",
        mime_type="text/markdown",
        client=mock_client,
    )

    assert result["_sha256"] == expected_sha
    assert result["_size"] == len(b"file content")
    # no URL to HEAD against, so the validator lookup is skipped entirely
    mock_client.head.assert_not_awaited()
    target = local_store.source_dir("test-source") / "test.md"
    sidecar = json.loads((target.parent / "test.md.meta.json").read_text())
    assert sidecar["ingestion_type"] == "webdav"
    assert "source_url" not in sidecar


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


# --- Phase A: shared client, bounded concurrency, parallel listing ---


def _row(name, etag='"e"'):
    """A cache-miss inventory row for *name*."""
    return {
        "path": name,
        "sha256": None,
        "_etag": etag,
        "metadata": {"size": 0, "content-type": "text/markdown"},
    }


@pytest.mark.asyncio
async def test_load_inventory_reuses_one_client_for_the_whole_run(local_env, monkeypatch):
    """One session for N files, not one per file."""
    monkeypatch.setattr(webdav_app.settings, "webdav_max_concurrent_requests", 3)
    sessions = []
    closed = []

    async def fake_ensure_session(self):
        sessions.append(id(self))
        return object()

    async def fake_aclose(self):
        closed.append(id(self))

    async def fake_download(self, path):
        return b"body", "text/markdown"

    config = [_row(f"doc{i}.md") for i in range(12)]
    with (
        patch.object(AsyncWebDAVClient, "_ensure_session", fake_ensure_session),
        patch.object(AsyncWebDAVClient, "aclose", fake_aclose),
        patch.object(AsyncWebDAVClient, "download", fake_download),
        patch.object(webdav_app.local_store, "write_document", new_callable=AsyncMock),
        patch.object(webdav_app.local_state, "upsert_file"),
    ):
        result = await webdav_app.load_inventory("", "test-source", config=config, webdav_url="https://dav.example.com")

    assert len(result["ingested"]) == 12
    assert len(sessions) == 1
    assert len(closed) == 1


@pytest.mark.asyncio
async def test_load_inventory_downloads_up_to_the_configured_concurrency(local_env, monkeypatch):
    """Downloads overlap, bounded by webdav_max_concurrent_requests."""
    monkeypatch.setattr(webdav_app.settings, "webdav_max_concurrent_requests", 4)
    inflight = 0
    peak = 0

    async def fake_do_ingest(*args, **kwargs):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return {"result": "success"}

    config = [_row(f"doc{i}.md") for i in range(12)]
    with patch("soliplex.agents.webdav.app.do_ingest", side_effect=fake_do_ingest):
        result = await webdav_app.load_inventory("", "test-source", config=config)

    assert len(result["ingested"]) == 12
    assert peak == 4


@pytest.mark.asyncio
async def test_load_inventory_merges_outcomes_in_inventory_order(local_env, monkeypatch):
    """Results land in inventory order regardless of completion order."""
    monkeypatch.setattr(webdav_app.settings, "webdav_max_concurrent_requests", 6)

    async def fake_do_ingest(base_path, uri, *args, **kwargs):
        # Finish in reverse order so completion order cannot be the source.
        await asyncio.sleep(0.02 - 0.002 * int(uri[3]))
        if uri == "doc1.md":
            return {"error": "boom"}
        if uri == "doc2.md":
            return {"not_found": True, "uri": uri}
        if uri == "doc3.md":
            raise RuntimeError("exploded")
        if uri == "doc4.md":
            return {"skipped": "content type not allowed", "uri": uri}
        return {"result": "success"}

    config = [_row(f"doc{i}.md") for i in range(6)]
    with patch("soliplex.agents.webdav.app.do_ingest", side_effect=fake_do_ingest):
        result = await webdav_app.load_inventory("", "test-source", config=config)

    assert result["ingested"] == ["doc0.md", "doc5.md"]
    assert [e["uri"] for e in result["errors"]] == ["doc1.md", "doc3.md"]
    assert result["errors"][1]["error"] == "exploded"
    assert result["not_found"] == ["doc2.md"]


@pytest.mark.asyncio
async def test_delete_stale_still_suppressed_when_a_concurrent_download_fails(local_env, monkeypatch):
    """A single failure anywhere must still block the stale sweep."""
    monkeypatch.setattr(webdav_app.settings, "webdav_max_concurrent_requests", 5)

    async def fake_do_ingest(base_path, uri, *args, **kwargs):
        if uri == "doc3.md":
            raise RuntimeError("exploded")
        return {"result": "success"}

    config = [_row(f"doc{i}.md") for i in range(6)]
    with (
        patch("soliplex.agents.webdav.app.do_ingest", side_effect=fake_do_ingest),
        patch.object(webdav_app.local_state, "reconcile_documents", new_callable=AsyncMock) as mock_reconcile,
    ):
        result = await webdav_app.load_inventory("", "test-source", config=config, delete_stale=True)

    assert result["delete_stale_result"] is None
    mock_reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_recursive_listdir_handles_tree_deeper_than_the_limit(monkeypatch):
    """A chain deeper than the semaphore must not deadlock.

    The limiter is held for one PROPFIND at a time; holding it across the
    recursive gather would wedge here.
    """
    monkeypatch.setattr(webdav_app.settings, "webdav_max_concurrent_requests", 2)
    depth = 8

    async def fake_ls(path, detail=True):
        level = path.count("/")
        if level < depth:
            return [
                {"name": "sub", "type": "directory", "content_length": 0},
                {"name": f"f{level}.md", "type": "file", "content_length": 1, "etag": '"e"'},
            ]
        return [{"name": f"leaf{level}.md", "type": "file", "content_length": 1, "etag": '"e"'}]

    client = AsyncMock()
    client.ls = AsyncMock(side_effect=fake_ls)

    files = await asyncio.wait_for(webdav_app.recursive_listdir_webdav(client, "/root"), timeout=5)
    assert len(files) == depth


@pytest.mark.asyncio
async def test_recursive_listdir_lists_siblings_concurrently(monkeypatch):
    """Sibling directories are listed in parallel up to the limit."""
    monkeypatch.setattr(webdav_app.settings, "webdav_max_concurrent_requests", 4)
    inflight = 0
    peak = 0

    async def fake_ls(path, detail=True):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        if path == "/root":
            return [{"name": f"d{i}", "type": "directory", "content_length": 0} for i in range(8)]
        return [{"name": "f.md", "type": "file", "content_length": 1, "etag": '"e"'}]

    client = AsyncMock()
    client.ls = AsyncMock(side_effect=fake_ls)

    files = await webdav_app.recursive_listdir_webdav(client, "/root")
    assert len(files) == 8
    assert peak == 4


@pytest.mark.asyncio
async def test_recursive_listdir_propagates_connection_errors_from_a_subtree():
    """A connection-class failure anywhere aborts the whole walk."""

    async def fake_ls(path, detail=True):
        if path == "/root":
            return [{"name": "d0", "type": "directory", "content_length": 0}]
        raise TimeoutError("gone")

    client = AsyncMock()
    client.ls = AsyncMock(side_effect=fake_ls)

    with pytest.raises(TimeoutError):
        await webdav_app.recursive_listdir_webdav(client, "/root")


@pytest.mark.asyncio
async def test_walk_webdav_records_a_non_connection_error_and_keeps_siblings():
    """A non-connection failure leaves that subtree out, records it, and keeps the siblings."""

    async def fake_ls(path, detail=True):
        if path == "/root":
            return [
                {"name": "bad", "type": "directory", "content_length": 0},
                {"name": "good", "type": "directory", "content_length": 0},
            ]
        if path.endswith("/bad"):
            raise ValueError("malformed listing")
        return [{"name": "f.md", "type": "file", "content_length": 1, "etag": '"e"'}]

    client = AsyncMock()
    client.ls = AsyncMock(side_effect=fake_ls)

    result = await webdav_app.walk_webdav(client, "/root")
    assert [f["path"] for f in result.files] == ["/root/good/f.md"]
    assert result.errors == [{"path": "/root/bad", "error": "ValueError: malformed listing"}]


# --- listing failures: a gap in the walk must never look like a removal ---


def _tree_client(failures: dict[str, BaseException]):
    """A client serving /root/{a,b,c}/f.md, plus /root/a/deep/f.md.

    *failures* maps a directory path to the exception its PROPFIND raises.
    """
    tree = {
        "/root": [
            {"name": "top.md", "type": "file", "content_length": 1, "etag": '"e"'},
            {"name": "a", "type": "directory", "content_length": 0},
            {"name": "b", "type": "directory", "content_length": 0},
            {"name": "c", "type": "directory", "content_length": 0},
        ],
        "/root/a": [
            {"name": "f.md", "type": "file", "content_length": 1, "etag": '"e"'},
            {"name": "deep", "type": "directory", "content_length": 0},
        ],
        "/root/a/deep": [{"name": "f.md", "type": "file", "content_length": 1, "etag": '"e"'}],
        "/root/b": [{"name": "f.md", "type": "file", "content_length": 1, "etag": '"e"'}],
        "/root/c": [{"name": "f.md", "type": "file", "content_length": 1, "etag": '"e"'}],
    }

    async def fake_ls(path, detail=True):
        if path in failures:
            raise failures[path]
        return tree[path]

    client = AsyncMock()
    client.ls = AsyncMock(side_effect=fake_ls)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


_ROOT_FAILURES = [
    RetryableHTTPError(503, "unavailable"),
    ClientError("HTTP 401: unauthorized"),
    ClientError("HTTP 403: forbidden"),
    InsufficientStorage("/root"),
    ClientError("Expected 207 multistatus, got 200"),
    ParseError("not well-formed"),
    ResourceNotFound("/root"),
    PermissionError("unexpected"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", _ROOT_FAILURES, ids=lambda e: type(e).__name__)
async def test_walk_webdav_raises_on_root_failure(exc):
    client = _tree_client({"/root": exc})
    with pytest.raises(type(exc)):
        await webdav_app.walk_webdav(client, "/root")


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", _ROOT_FAILURES, ids=lambda e: type(e).__name__)
async def test_recursive_listdir_webdav_raises_on_root_failure_of_any_kind(exc):
    client = _tree_client({"/root": exc})
    with pytest.raises(type(exc)):
        await webdav_app.recursive_listdir_webdav(client, "/root")


@pytest.mark.asyncio
async def test_walk_webdav_records_a_forbidden_subtree():
    client = _tree_client({"/root/b": ClientError("HTTP 403: forbidden")})

    result = await webdav_app.walk_webdav(client, "/root")

    assert [f["path"] for f in result.files] == [
        "/root/top.md",
        "/root/a/f.md",
        "/root/a/deep/f.md",
        "/root/c/f.md",
    ]
    assert result.errors == [{"path": "/root/b", "error": "ClientError: HTTP 403: forbidden"}]


@pytest.mark.asyncio
async def test_walk_webdav_reports_a_nested_failure_at_the_top():
    client = _tree_client({"/root/a/deep": RetryableHTTPError(503, "unavailable")})

    result = await webdav_app.walk_webdav(client, "/root")

    assert "/root/a/f.md" in [f["path"] for f in result.files]
    assert "/root/a/deep/f.md" not in [f["path"] for f in result.files]
    assert [e["path"] for e in result.errors] == ["/root/a/deep"]
    assert result.errors[0]["error"].startswith("RetryableHTTPError: ")


@pytest.mark.asyncio
async def test_walk_webdav_records_a_subtree_404_instead_of_aborting():
    client = _tree_client({"/root/c": ResourceNotFound("/root/c")})

    result = await webdav_app.walk_webdav(client, "/root")

    assert [e["path"] for e in result.errors] == ["/root/c"]
    assert len(result.files) == 4


@pytest.mark.asyncio
async def test_walk_webdav_records_a_malformed_subtree_body():
    client = _tree_client({"/root/a": ParseError("not well-formed")})

    result = await webdav_app.walk_webdav(client, "/root")

    assert result.errors == [{"path": "/root/a", "error": "ParseError: not well-formed"}]
    assert [f["path"] for f in result.files] == ["/root/top.md", "/root/b/f.md", "/root/c/f.md"]


@pytest.mark.asyncio
async def test_walk_webdav_still_aborts_on_a_subtree_timeout():
    client = _tree_client({"/root/a/deep": TimeoutError("gone")})
    with pytest.raises(TimeoutError):
        await webdav_app.walk_webdav(client, "/root")


@pytest.mark.asyncio
async def test_walk_webdav_logs_one_summary_record_for_an_incomplete_walk(caplog):
    client = _tree_client({"/root/a": ClientError("HTTP 403"), "/root/b": ClientError("HTTP 403")})

    with caplog.at_level("ERROR", logger="soliplex.agents.webdav.app"):
        await webdav_app.walk_webdav(client, "/root")

    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("Error listing WebDAV subtree /root/a") == 1
    assert messages.count("Error listing WebDAV subtree /root/b") == 1
    assert "WebDAV listing of /root incomplete: 2 subtree(s) failed; stale removal will be skipped" in messages


@pytest.mark.asyncio
async def test_recursive_listdir_webdav_is_strict_about_subtrees():
    client = _tree_client({"/root/b": ClientError("HTTP 403")})

    with pytest.raises(WebDAVListingError) as excinfo:
        await webdav_app.recursive_listdir_webdav(client, "/root")

    assert excinfo.value.path == "/root"
    assert [e["path"] for e in excinfo.value.errors] == ["/root/b"]
    assert "/root/b" in str(excinfo.value)


@pytest.mark.asyncio
async def test_build_config_returns_listing_errors(local_env):
    client = _tree_client({"/root/b": ClientError("HTTP 403")})

    config, listing_errors = await webdav_app.build_config("/root", client=client)

    assert sorted(r["path"] for r in config) == ["a/deep/f.md", "a/f.md", "c/f.md", "top.md"]
    assert listing_errors == [{"path": "/root/b", "error": "ClientError: HTTP 403"}]


@pytest.mark.asyncio
async def test_build_config_raises_on_root_failure(local_env):
    client = _tree_client({"/root": ClientError("HTTP 401")})
    with pytest.raises(ClientError):
        await webdav_app.build_config("/root", client=client)


@pytest.mark.asyncio
async def test_load_inventory_listing_errors_block_delete_stale_but_listed_files_download(local_env):
    client = _tree_client({"/root/b": ClientError("HTTP 403")})
    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=client),
        patch(
            "soliplex.agents.webdav.app.do_ingest", new_callable=AsyncMock, return_value={"result": "success"}
        ) as mock_ingest,
        patch.object(webdav_app.local_state, "reconcile_documents", new_callable=AsyncMock) as mock_reconcile,
    ):
        result = await webdav_app.load_inventory("/root", "test-source", webdav_url="http://dav", delete_stale=True)

    assert result["errors"] == [{"uri": "/root/b", "error": "ClientError: HTTP 403", "stage": "listing"}]
    assert sorted(result["ingested"]) == ["a/deep/f.md", "a/f.md", "c/f.md", "top.md"]
    assert mock_ingest.await_count == 4
    assert result["delete_stale_result"] is None
    mock_reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_inventory_root_listing_failure_raises_and_deletes_nothing(local_env):
    client = _tree_client({"/root": RetryableHTTPError(503, "unavailable")})
    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=client),
        patch.object(webdav_app.local_state, "reconcile_documents", new_callable=AsyncMock) as mock_reconcile,
        pytest.raises(RetryableHTTPError),
    ):
        await webdav_app.load_inventory("/root", "test-source", webdav_url="http://dav", delete_stale=True)

    mock_reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_config_raises_on_a_failed_subtree():
    client = _tree_client({"/root/b": ClientError("HTTP 403")})
    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=client),
        pytest.raises(WebDAVListingError),
    ):
        await webdav_app.list_config("/root")


@pytest.mark.asyncio
async def test_export_urls_writes_nothing_on_a_failed_subtree(tmp_path):
    client = _tree_client({"/root/b": ClientError("HTTP 403")})
    output_file = tmp_path / "exported.txt"
    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=client),
        pytest.raises(WebDAVListingError),
    ):
        await webdav_app.export_urls("/root", str(output_file))

    assert not output_file.exists()


@pytest.mark.asyncio
async def test_validate_config_raises_on_a_failed_subtree(local_env):
    client = _tree_client({"/root/b": ClientError("HTTP 403")})
    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=client),
        pytest.raises(WebDAVListingError),
    ):
        await webdav_app.validate_config("/root")


@pytest.mark.asyncio
async def test_status_report_raises_on_a_failed_subtree(local_env):
    client = _tree_client({"/root/b": ClientError("HTTP 403")})
    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=client),
        pytest.raises(WebDAVListingError),
    ):
        await webdav_app.status_report("/root", "test-source")


# --- exclude_paths: folders skipped on purpose ---


def _paths(result):
    return [f["path"] for f in result.files]


@pytest.mark.asyncio
async def test_walk_webdav_excluded_folder_is_never_listed_and_is_not_an_error():
    client = _tree_client({"/root/b": ClientError("HTTP 403: forbidden")})

    result = await webdav_app.walk_webdav(client, "/root", exclude_paths=["b"])

    assert result.errors == []
    assert result.excluded == ["/root/b"]
    assert "/root/b" not in [c.args[0] for c in client.ls.call_args_list]
    assert _paths(result) == ["/root/top.md", "/root/a/f.md", "/root/a/deep/f.md", "/root/c/f.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern,excluded",
    [
        ("a/deep", ["/root/a/deep"]),
        ("/a/deep/", ["/root/a/deep"]),
        ("**/deep", ["/root/a/deep"]),
        ("*/deep", ["/root/a/deep"]),
        ("deep", []),  # relative to the root: only a top-level 'deep'
        ("[ab]", ["/root/a", "/root/b"]),
        ("*.md", ["/root/top.md"]),  # '*' stays within one segment
        ("**/f.md", ["/root/a/f.md", "/root/a/deep/f.md", "/root/b/f.md", "/root/c/f.md"]),
    ],
)
async def test_walk_webdav_exclude_paths_glob_semantics(pattern, excluded):
    client = _tree_client({})

    result = await webdav_app.walk_webdav(client, "/root", exclude_paths=[pattern])

    assert sorted(result.excluded) == sorted(excluded)
    assert not set(excluded) & set(_paths(result))


@pytest.mark.asyncio
async def test_walk_webdav_exclude_matches_the_decoded_name():
    async def fake_ls(path, detail=True):
        if path == "/root":
            return [
                {"name": "Human%20Resources", "type": "directory", "content_length": 0},
                {"name": "ok.md", "type": "file", "content_length": 1},
            ]
        raise AssertionError(f"listed {path}")

    client = AsyncMock()
    client.ls = AsyncMock(side_effect=fake_ls)

    result = await webdav_app.walk_webdav(client, "/root", exclude_paths=["Human Resources"])

    assert result.excluded == ["/root/Human%20Resources"]
    assert _paths(result) == ["/root/ok.md"]


@pytest.mark.asyncio
async def test_walk_webdav_exclude_relative_to_the_server_root():
    async def fake_ls(path, detail=True):
        if path == "/":
            return [
                {"name": "a", "type": "directory", "content_length": 0},
                {"name": "b", "type": "directory", "content_length": 0},
            ]
        return [{"name": "f.md", "type": "file", "content_length": 1}]

    client = AsyncMock()
    client.ls = AsyncMock(side_effect=fake_ls)

    result = await webdav_app.walk_webdav(client, "/", exclude_paths=["a"])

    assert result.excluded == ["/a"]
    assert _paths(result) == ["/b/f.md"]


def test_normalize_exclude_paths_rejects_an_empty_pattern():
    with pytest.raises(ValueError, match="empty pattern"):
        webdav_app.normalize_exclude_paths(["ok", " / "])


@pytest.mark.asyncio
async def test_walk_webdav_logs_exclusions(caplog):
    client = _tree_client({})
    with caplog.at_level("INFO", logger="soliplex.agents.webdav.app"):
        await webdav_app.walk_webdav(client, "/root", exclude_paths=["b"])

    messages = [r.getMessage() for r in caplog.records]
    assert "Excluding WebDAV path /root/b" in messages
    assert "WebDAV listing of /root: 1 path(s) excluded by exclude_paths" in messages


@pytest.mark.asyncio
async def test_recursive_listdir_webdav_honours_exclude_paths():
    client = _tree_client({"/root/b": ClientError("HTTP 403")})

    files = await webdav_app.recursive_listdir_webdav(client, "/root", exclude_paths=["b"])

    assert "/root/b/f.md" not in [f["path"] for f in files]


@pytest.mark.asyncio
async def test_load_inventory_excluded_folder_lets_delete_stale_run(local_env):
    """Excluded documents are absent on purpose, so the clean-up proceeds without them."""
    client = _tree_client({"/root/b": ClientError("HTTP 403")})
    with (
        patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=client),
        patch("soliplex.agents.webdav.app.do_ingest", new_callable=AsyncMock, return_value={"result": "success"}),
        patch.object(
            webdav_app.local_state, "reconcile_documents", new_callable=AsyncMock, return_value=["b/f.md"]
        ) as mock_reconcile,
    ):
        result = await webdav_app.load_inventory(
            "/root", "test-source", webdav_url="http://dav", delete_stale=True, exclude_paths=["b"]
        )

    assert result["errors"] == []
    mock_reconcile.assert_awaited_once_with("test-source", {"top.md", "a/f.md", "a/deep/f.md", "c/f.md"})
    assert result["delete_stale_result"] == ["b/f.md"]


@pytest.mark.asyncio
async def test_export_urls_honours_exclude_paths(tmp_path):
    client = _tree_client({"/root/b": ClientError("HTTP 403")})
    output_file = tmp_path / "exported.txt"
    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=client):
        await webdav_app.export_urls("/root", str(output_file), exclude_paths=["b"])

    assert output_file.read_text().splitlines() == ["/root/top.md", "/root/a/f.md", "/root/a/deep/f.md", "/root/c/f.md"]


@pytest.mark.asyncio
async def test_validate_and_status_honour_exclude_paths(local_env, capsys):
    client = _tree_client({"/root/b": ClientError("HTTP 403")})
    with patch("soliplex.agents.webdav.app.create_async_webdav_client", return_value=client):
        await webdav_app.validate_config("/root", exclude_paths=["b"])
        await webdav_app.status_report("/root", "test-source", exclude_paths=["b"])

    out = capsys.readouterr().out
    assert "Total files: 4" in out


# --- retries of failed subtrees ---


def _flaky(failures: dict[str, list[BaseException]]):
    """A tree client whose directories fail with each queued exception in turn, then list."""
    healthy = _tree_client({})

    async def fake_ls(path, detail=True):
        queue = failures.get(path)
        if queue:
            raise queue.pop(0)
        return await healthy.ls(path, detail=detail)

    client = AsyncMock()
    client.ls = AsyncMock(side_effect=fake_ls)
    return client


@pytest.mark.asyncio
async def test_walk_webdav_retry_recovers_a_transient_subtree_failure(caplog):
    client = _flaky({"/root/a": [RetryableHTTPError(503, "unavailable")]})

    with caplog.at_level("INFO", logger="soliplex.agents.webdav.app"):
        result = await webdav_app.walk_webdav(client, "/root")

    assert result.errors == []
    assert sorted(_paths(result)) == ["/root/a/deep/f.md", "/root/a/f.md", "/root/b/f.md", "/root/c/f.md", "/root/top.md"]
    messages = [r.getMessage() for r in caplog.records]
    assert "Retrying 1 failed WebDAV subtree(s) under /root in 0.0s (pass 1 of 1)" in messages
    assert "WebDAV subtree /root/a listed on retry" in messages
    # Recovered: nothing reported at ERROR.
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


@pytest.mark.asyncio
async def test_walk_webdav_retry_gives_up_after_the_configured_passes(monkeypatch):
    monkeypatch.setattr(webdav_app.settings, "webdav_listing_retries", 2)
    failures = [ClientError("HTTP 403")] * 5
    client = _flaky({"/root/b": list(failures)})

    result = await webdav_app.walk_webdav(client, "/root")

    assert result.errors == [{"path": "/root/b", "error": "ClientError: HTTP 403"}]
    assert [c.args[0] for c in client.ls.call_args_list].count("/root/b") == 3


@pytest.mark.asyncio
async def test_walk_webdav_retry_disabled(monkeypatch):
    monkeypatch.setattr(webdav_app.settings, "webdav_listing_retries", 0)
    client = _flaky({"/root/b": [ClientError("HTTP 403")]})

    result = await webdav_app.walk_webdav(client, "/root")

    assert [e["path"] for e in result.errors] == ["/root/b"]
    assert [c.args[0] for c in client.ls.call_args_list].count("/root/b") == 1


@pytest.mark.asyncio
async def test_walk_webdav_retry_reports_a_nested_failure_found_on_retry():
    # '/root/a' fails, then lists on retry, but its 'deep' child fails that time.
    client = _flaky(
        {
            "/root/a": [RetryableHTTPError(503, "unavailable")],
            "/root/a/deep": [ParseError("not well-formed")],
        }
    )

    result = await webdav_app.walk_webdav(client, "/root")

    assert result.errors == [{"path": "/root/a/deep", "error": "ParseError: not well-formed"}]
    assert "/root/a/f.md" in _paths(result)


@pytest.mark.asyncio
async def test_walk_webdav_retry_still_aborts_on_a_connection_error():
    client = _flaky({"/root/a": [ClientError("HTTP 403"), TimeoutError("gone")]})
    with pytest.raises(TimeoutError):
        await webdav_app.walk_webdav(client, "/root")


@pytest.mark.asyncio
async def test_walk_webdav_retry_backs_off(monkeypatch):
    monkeypatch.setattr(webdav_app.settings, "webdav_listing_retries", 3)
    monkeypatch.setattr(webdav_app.settings, "webdav_listing_retry_delay", 1.5)
    client = _flaky({"/root/b": [ClientError("HTTP 403")] * 4})

    with patch("soliplex.agents.webdav.app.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await webdav_app.walk_webdav(client, "/root")

    assert [c.args[0] for c in mock_sleep.await_args_list] == [1.5, 3.0, 6.0]


@pytest.mark.asyncio
async def test_walk_webdav_does_not_retry_a_complete_walk():
    client = _tree_client({})
    with patch("soliplex.agents.webdav.app.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await webdav_app.walk_webdav(client, "/root")

    assert result.errors == []
    mock_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_walk_webdav_final_error_log_carries_the_exception(caplog):
    client = _tree_client({"/root/b": ClientError("HTTP 403")})
    with caplog.at_level("ERROR", logger="soliplex.agents.webdav.app"):
        await webdav_app.walk_webdav(client, "/root")

    (record,) = [r for r in caplog.records if r.getMessage() == "Error listing WebDAV subtree /root/b"]
    assert isinstance(record.exc_info[1], ClientError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [ClientError("HTTP 401: unauthorized"), ClientError("HTTP 403: forbidden")],
    ids=["401", "403"],
)
async def test_walk_webdav_retries_auth_failures(exc):
    """401/403 are fixed upstream (credential, ACL), so they are retried like a 5xx."""
    client = _flaky({"/root/b": [exc]})

    result = await webdav_app.walk_webdav(client, "/root")

    assert result.errors == []
    assert "/root/b/f.md" in _paths(result)
