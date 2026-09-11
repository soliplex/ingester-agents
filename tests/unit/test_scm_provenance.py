"""The URL an SCM document's sidecar records.

Both SCM write paths reach ``write_document`` with differently shaped rows:
``load_inventory`` goes through ``get_data``, which nests provider fields
under ``metadata``, while ``incremental_sync`` passes ``parse_file_rec``
output through flat. ``_source_url`` reads both, and these tests pin each
path end to end rather than only the helper, because the divergence between
the two shapes is what silently dropped the URL on both of them before.
"""

import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from soliplex.agents import local_state
from soliplex.agents import local_store
from soliplex.agents import store as agent_store
from soliplex.agents.config import SCM
from soliplex.agents.config import ContentFilter
from soliplex.agents.scm import app as scm_app

HTML_URL = "https://example.com/admin/test/src/branch/main/docs/test.md"
API_URL = "https://api.example.com/repos/admin/test/contents/docs/test.md"


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """Point download_dir and state_dir at temp directories."""
    monkeypatch.setattr(local_state.settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path / "dl"))
    return tmp_path


def _sidecar(source: str, *parts: str) -> dict:
    path = local_store.source_dir(source).joinpath(*parts)
    return json.loads(path.with_name(path.name + ".meta.json").read_text())


# --- _source_url ----------------------------------------------------------


def test_source_url_prefers_html_url_over_api_url():
    """The browsable URL wins: it is the one a reader can open."""
    assert scm_app._source_url({"html_url": HTML_URL, "url": API_URL}) == HTML_URL


def test_source_url_reads_the_flat_row_shape():
    """``parse_file_rec`` output, as ``incremental_sync`` receives it."""
    assert scm_app._source_url({"html_url": HTML_URL, "uri": "docs/test.md"}) == HTML_URL


def test_source_url_reads_the_nested_row_shape():
    """A row whose provider fields landed under ``metadata``."""
    assert scm_app._source_url({"metadata": {"html_url": HTML_URL}}) == HTML_URL


def test_source_url_falls_back_to_the_api_url():
    """Better a resolvable API endpoint than nothing at all."""
    assert scm_app._source_url({"url": API_URL}) == API_URL


def test_source_url_is_none_when_the_provider_gave_neither():
    assert scm_app._source_url({"uri": "docs/test.md"}) is None
    assert scm_app._source_url({"html_url": None, "metadata": {}}) is None


# --- load_inventory (get_data row shape) ----------------------------------


@pytest.mark.asyncio
async def test_load_inventory_records_source_url(local_env):
    source = "gitea:admin:test:files"
    with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
        provider = MagicMock()
        provider.list_repo_files = AsyncMock(
            return_value=[
                {
                    "uri": "docs/test.md",
                    "file_bytes": b"# Test",
                    "sha256": "abc",
                    "content-type": "text/markdown",
                    "last_updated": "2026-01-01T00:00:00Z",
                    "last_commit_sha": "def456",
                    "url": API_URL,
                    "html_url": HTML_URL,
                }
            ]
        )
        mock_get_scm.return_value = provider

        await scm_app.load_inventory(SCM.GITEA, "test", "admin", content_filter=ContentFilter.FILES, source=source)

    meta = _sidecar(source, "docs", "test.md")
    assert meta["ingestion_type"] == "scm"
    assert meta["source_url"] == HTML_URL
    assert meta["downloaded_time"]
    # The URL is the sidecar's own field, not operator-supplied metadata.
    assert "html_url" not in meta["metadata"]


@pytest.mark.asyncio
async def test_load_inventory_omits_source_url_when_provider_gave_none(local_env):
    """A provider shape without either URL must not fail the write."""
    source = "gitea:admin:test:files"
    with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
        provider = MagicMock()
        provider.list_repo_files = AsyncMock(
            return_value=[
                {
                    "uri": "docs/test.md",
                    "file_bytes": b"# Test",
                    "sha256": "abc",
                    "content-type": "text/markdown",
                    "last_updated": "2026-01-01T00:00:00Z",
                    "last_commit_sha": "def456",
                }
            ]
        )
        mock_get_scm.return_value = provider

        await scm_app.load_inventory(SCM.GITEA, "test", "admin", content_filter=ContentFilter.FILES, source=source)

    assert "source_url" not in _sidecar(source, "docs", "test.md")


@pytest.mark.asyncio
async def test_load_inventory_records_issue_source_url(local_env):
    source = "gitea:admin:test:issues"
    issue_url = "https://example.com/admin/test/issues/7"
    with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
        provider = MagicMock()
        provider.list_issues = AsyncMock(
            return_value=[
                {
                    "number": 7,
                    "title": "Test Issue",
                    "body": "body",
                    "state": "open",
                    "assignee": None,
                    "user": {"login": "testuser"},
                    "created_at": "2026-01-01T00:00:00Z",
                    "comment_count": 0,
                    "comments": [],
                    "html_url": issue_url,
                }
            ]
        )
        mock_get_scm.return_value = provider

        await scm_app.load_inventory(SCM.GITEA, "test", "admin", content_filter=ContentFilter.ISSUES, source=source)

    assert _sidecar(source, "admin", "test", "issues", "7.md")["source_url"] == issue_url


# --- incremental_sync (parse_file_rec row shape) --------------------------


@pytest.mark.asyncio
async def test_incremental_sync_records_source_url(local_env):
    """The flat row from ``get_single_file`` carries the URL through too."""
    source = "gitea:admin:test:files"
    local_state.set_sync_meta(source, "abc123", branch="main")

    with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
        provider = MagicMock()
        provider.list_commits_since = AsyncMock(return_value=[{"sha": "def456", "message": "Edit"}])
        provider.get_commit_details = AsyncMock(
            return_value={"sha": "def456", "files": [{"filename": "docs/test.md", "status": "modified"}]}
        )
        provider.get_single_file = AsyncMock(
            return_value={
                "name": "test.md",
                "uri": "docs/test.md",
                "path": "docs/test.md",
                "file_bytes": b"# Test",
                "sha256": "abc",
                "content-type": "text/markdown",
                "url": API_URL,
                "html_url": HTML_URL,
                "last_updated": "2026-01-01T00:00:00Z",
                "last_commit_sha": "def456",
            }
        )
        provider.list_issues = AsyncMock(return_value=[])
        mock_get_scm.return_value = provider

        result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin", content_filter=ContentFilter.FILES, source=source)

    assert result["ingested"] == ["docs/test.md"]
    assert _sidecar(source, "docs", "test.md")["source_url"] == HTML_URL
