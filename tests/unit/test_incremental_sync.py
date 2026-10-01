"""Unit tests for incremental sync and SCM provider commit helpers."""

import datetime
import pathlib
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


def create_async_context_manager(return_value):
    """Create an async context manager that returns the given value."""
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=return_value)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """Point download_dir and state_dir at temp directories."""
    monkeypatch.setattr(local_state.settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(agent_store.settings, "download_dir", str(tmp_path / "dl"))
    return tmp_path


# --- incremental_sync (local storage) ---


@pytest.mark.asyncio
async def test_incremental_sync_no_state(local_env):
    """First sync should fall back to a full sync when no state exists."""
    with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
        mock_provider = MagicMock()
        mock_provider.list_repo_files = AsyncMock(return_value=[])
        mock_provider.list_issues = AsyncMock(return_value=[])
        mock_get_scm.return_value = mock_provider

        result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin")

        assert "inventory" in result
        assert "to_process" in result
        # full sync records a sync marker (timestamp set even with no commit)
        assert local_state.get_sync_meta("gitea:admin:test:all")["last_sync_date"] is not None


@pytest.mark.asyncio
async def test_incremental_sync_with_changes(local_env):
    """Incremental sync should write only changed files and advance the commit."""
    source = "gitea:admin:test:all"
    local_state.set_sync_meta(source, "abc123", branch="main")

    with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
        mock_provider = MagicMock()
        mock_provider.list_commits_since = AsyncMock(
            return_value=[
                {"sha": "def456", "message": "Update file1.md"},
                {"sha": "ghi789", "message": "Update file2.md"},
            ]
        )
        mock_provider.get_commit_details = AsyncMock(
            side_effect=[
                {"sha": "def456", "files": [{"filename": "file1.md", "status": "modified"}]},
                {"sha": "ghi789", "files": [{"filename": "file2.md", "status": "modified"}]},
            ]
        )
        mock_provider.get_single_file = AsyncMock(
            return_value={
                "uri": "file1.md",
                "file_bytes": b"test content",
                "content-type": "text/markdown",
                "sha256": "deadbeef",
                "metadata": {},
            }
        )
        mock_provider.list_issues = AsyncMock(return_value=[])
        mock_get_scm.return_value = mock_provider

        result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin")

        assert result["status"] == "synced"
        assert result["commits_processed"] == 2
        assert result["files_changed"] == 2
        # changed file was written under the source folder
        assert (local_store.source_dir(source) / "file1.md").read_bytes() == b"test content"
        # commit marker advanced to newest commit
        assert local_state.get_sync_meta(source)["last_commit_sha"] == "def456"


@pytest.mark.asyncio
async def test_incremental_sync_up_to_date(local_env):
    """No changes should return up-to-date status."""
    source = "gitea:admin:test:all"
    local_state.set_sync_meta(source, "abc123")

    with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
        mock_provider = MagicMock()
        mock_provider.list_commits_since = AsyncMock(return_value=[])
        mock_provider.list_issues = AsyncMock(return_value=[])
        mock_get_scm.return_value = mock_provider

        result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin")

        assert result["status"] == "up-to-date"
        assert result["commits_processed"] == 0
        assert result["files_changed"] == 0


@pytest.mark.asyncio
async def test_incremental_sync_with_removed_files(local_env):
    """Incremental sync should delete locally files removed in the source."""
    source = "gitea:admin:test:all"
    # Seed a stale file + state entry.
    await local_store.write_document(source, "old_file.md", b"old", "text/markdown", {})
    local_state.upsert_file(source, "old_file.md", "oldsha", mime_type="text/markdown")
    local_state.set_sync_meta(source, "abc123", branch="main")

    with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
        mock_provider = MagicMock()
        mock_provider.list_commits_since = AsyncMock(return_value=[{"sha": "def456", "message": "Remove old file"}])
        mock_provider.get_commit_details = AsyncMock(
            return_value={"sha": "def456", "files": [{"filename": "old_file.md", "status": "removed"}]}
        )
        mock_provider.list_issues = AsyncMock(return_value=[])
        mock_get_scm.return_value = mock_provider

        result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin")

        assert result["status"] == "synced"
        assert result["files_removed"] == 1
        # removed file and its state entry are gone
        assert not (local_store.source_dir(source) / "old_file.md").exists()
        assert "old_file.md" not in local_state.load_file_state(source)


@pytest.mark.asyncio
async def test_incremental_sync_issues_reconciliation(local_env):
    """ISSUES-only sync should prune issues no longer present in the source."""
    source = "gitea:admin:test:issues"
    # Seed a stale issue locally.
    await local_store.write_document(source, "/admin/test/issues/9", b"# old", "text/markdown", {})
    local_state.upsert_file(source, "/admin/test/issues/9", "s9", mime_type="text/markdown")
    local_state.set_sync_meta(source, "abc123", last_sync_date=datetime.datetime(2026, 1, 1))

    with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
        mock_provider = MagicMock()
        mock_provider.list_issues = AsyncMock(return_value=[])
        mock_get_scm.return_value = mock_provider

        await scm_app.incremental_sync(SCM.GITEA, "test", "admin", content_filter=ContentFilter.ISSUES)

        # stale issue reconciled away
        assert not (local_store.source_dir(source) / "admin" / "test" / "issues" / "9.md").exists()
        assert "/admin/test/issues/9" not in local_state.load_file_state(source)


# --- SCM provider commit helpers (unchanged behaviour) ---


@pytest.mark.asyncio
async def test_list_commits_since(mock_response):
    """Test listing commits since a specific SHA."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider()

    first_page_resp = mock_response(
        200,
        [
            {"sha": "commit3", "message": "Third commit"},
            {"sha": "commit2", "message": "Second commit"},
            {"sha": "commit1", "message": "First commit"},
        ],
    )

    mock_sess = MagicMock()
    mock_sess.get = AsyncMock(return_value=first_page_resp)
    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            commits = await provider.list_commits_since("test", "admin", "commit1", "main")

            assert len(commits) == 2
            assert commits[0]["sha"] == "commit3"
            assert commits[1]["sha"] == "commit2"


@pytest.mark.asyncio
async def test_list_commits_since_no_marker(mock_response):
    """Test listing commits when no marker is provided (get all recent)."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider()

    page_resp = mock_response(
        200,
        [
            {"sha": "commit3", "message": "Third commit"},
            {"sha": "commit2", "message": "Second commit"},
        ],
    )
    empty_resp = mock_response(200, [])

    mock_sess = MagicMock()
    mock_sess.get = AsyncMock(side_effect=[page_resp, empty_resp])
    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            commits = await provider.list_commits_since("test", "admin", None, "main")

            assert len(commits) == 2
            assert commits[0]["sha"] == "commit3"


@pytest.mark.asyncio
async def test_get_commit_details(mock_response):
    """Test getting detailed commit information."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider()

    commit_resp = mock_response(
        200,
        {
            "sha": "abc123",
            "message": "Test commit",
            "files": [
                {"filename": "file1.md", "status": "modified"},
                {"filename": "file2.md", "status": "added"},
            ],
        },
    )

    mock_sess = MagicMock()
    mock_sess.get = AsyncMock(return_value=commit_resp)
    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        result = await provider.get_commit_details("test", "admin", "abc123")

        assert result["sha"] == "abc123"
        assert len(result["files"]) == 2


@pytest.mark.asyncio
async def test_get_single_file(mock_response):
    """Test getting a single file from repository."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return "2024-01-01T00:00:00Z"

    provider = TestProvider()

    import base64

    content = base64.b64encode(b"# Test file content").decode()

    file_resp = mock_response(
        200,
        {
            "name": "test.md",
            "path": "docs/test.md",
            "url": "http://test/repos/admin/test/contents/docs/test.md",
            "content": content,
            "last_commit_sha": "abc123",
        },
    )

    mock_sess = MagicMock()
    mock_sess.get = AsyncMock(return_value=file_resp)
    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        result = await provider.get_single_file("test", "admin", "docs/test.md", "main")

        assert result["name"] == "test.md"
        assert result["uri"] == "docs/test.md"
        assert "sha256" in result


@pytest.mark.asyncio
async def test_list_commits_since_pagination(mock_response):
    """Test list_commits_since with pagination across multiple pages."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider()

    first_page = mock_response(200, [{"sha": "commit3"}, {"sha": "commit2"}])
    second_page = mock_response(200, [{"sha": "commit1"}])

    mock_sess = MagicMock()
    mock_sess.get = AsyncMock(side_effect=[first_page, second_page])
    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            commits = await provider.list_commits_since("test", "admin", "commit1", "main", limit=2)

            assert len(commits) == 2


@pytest.mark.asyncio
async def test_list_commits_since_empty_first_page(mock_response):
    """Test list_commits_since when first page is empty."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider()

    empty_resp = mock_response(200, [])

    mock_sess = MagicMock()
    mock_sess.get = AsyncMock(return_value=empty_resp)
    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            commits = await provider.list_commits_since("test", "admin", "commit1", "main")

            assert len(commits) == 0


@pytest.mark.asyncio
async def test_list_commits_since_max_pages_limit(mock_response):
    """Test list_commits_since when max pages limit is reached."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider()

    responses = []
    for i in range(11):
        page_commits = [{"sha": f"commit_{i}_{j}"} for j in range(100)]
        responses.append(mock_response(200, page_commits))

    mock_sess = MagicMock()
    mock_sess.get = AsyncMock(side_effect=responses)
    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            commits = await provider.list_commits_since("test", "admin", None, "main", limit=100)

            assert len(commits) == 1000


# ---------------------------------------------------------------------------
# Pre-process: an encrypted PDF is a content decision, not a failure
# ---------------------------------------------------------------------------

_PDF_FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures" / "pdf"


def _pdf_check_run(source):
    """The pre-process run of a manifest that lists `check_pdf_password`."""
    from soliplex.agents.config import Manifest
    from soliplex.agents.manifest import pre_process

    manifest = Manifest(
        id="m",
        name="M",
        source=source,
        config={
            "pre_process": [
                {"method": "soliplex.agents.manifest.pre_processors:check_pdf_password", "mime_types": ["application/pdf"]}
            ]
        },
        components=[{"type": "fs", "name": "c", "path": "/x"}],
    )
    return pre_process.PreProcessRun(manifest_id="m", source=source, steps=pre_process.resolve_steps(manifest))


def _locked_pdf_provider():
    provider = MagicMock()
    provider.list_commits_since = AsyncMock(return_value=[{"sha": "def456", "message": "Add locked.pdf"}])
    provider.get_commit_details = AsyncMock(
        return_value={"sha": "def456", "files": [{"filename": "locked.pdf", "status": "modified"}]}
    )
    provider.get_single_file = AsyncMock(
        return_value={
            "uri": "locked.pdf",
            "file_bytes": (_PDF_FIXTURES / "user_password.pdf").read_bytes(),
            "content-type": "application/pdf",
            "sha256": "aabbcc",
            "metadata": {},
        }
    )
    provider.list_issues = AsyncMock(return_value=[])
    provider.list_all_files = AsyncMock(return_value=[])
    return provider


@pytest.mark.asyncio
async def test_encrypted_pdf_is_skipped_without_pinning_the_cursor(local_env):
    """A skipped document is not stored, but is not an error either.

    The cursor only advances when the run had no errors, so counting a skip
    as one would make every run fetch the same commit and skip the same file
    forever. The state row is still recorded, so a full run does not re-fetch
    it until upstream changes.
    """
    from soliplex.agents.manifest import pre_process

    source = "gitea:admin:test:all"
    local_state.set_sync_meta(source, "abc123", branch="main")

    with patch("soliplex.agents.scm.app.get_scm", return_value=_locked_pdf_provider()):
        with pre_process.activate(_pdf_check_run(source)) as run:
            result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin", delete_stale=False)

    assert result["errors"] == []
    assert result["new_commit_sha"] == "def456"
    assert local_state.get_sync_meta(source)["last_commit_sha"] == "def456"
    assert run.skipped == [
        {
            "uri": "locked.pdf",
            "method": "soliplex.agents.manifest.pre_processors:check_pdf_password",
            "message": "password protected",
        }
    ]
    assert not (local_store.source_dir(source) / "locked.pdf").exists()
    assert "locked.pdf" in local_state.load_file_state(source)
    assert local_state.get_pre_process_document(source, "locked.pdf")["status"] == "skip"


@pytest.mark.asyncio
async def test_encrypted_pdf_removes_the_previously_stored_version(local_env):
    """An upstream replacement that is now encrypted must not leave the old one indexed."""
    from soliplex.agents.manifest import pre_process

    source = "gitea:admin:test:all"
    local_state.set_sync_meta(source, "abc123", branch="main")
    valid = (_PDF_FIXTURES / "valid.pdf").read_bytes()
    await local_store.write_document(source, "locked.pdf", valid, "application/pdf")
    stored = local_store.source_dir(source) / "locked.pdf"
    assert stored.exists()

    with patch("soliplex.agents.scm.app.get_scm", return_value=_locked_pdf_provider()):
        with pre_process.activate(_pdf_check_run(source)):
            await scm_app.incremental_sync(SCM.GITEA, "test", "admin", delete_stale=False)

    assert not stored.exists()
    assert not stored.with_name("locked.pdf.meta.json").exists()


@pytest.mark.asyncio
async def test_skip_does_not_block_delete_stale(local_env):
    """Stale cleanup still runs, so the relocation repair still runs with it."""
    from soliplex.agents.manifest import pre_process

    source = "gitea:admin:test:all"
    local_state.set_sync_meta(source, "abc123", branch="main")

    with (
        patch("soliplex.agents.scm.app.get_scm", return_value=_locked_pdf_provider()),
        patch("soliplex.agents.scm.app.list_all_uris", AsyncMock(return_value=[])) as mock_list,
        patch("soliplex.agents.local_state.prune_documents", AsyncMock(return_value=[])) as mock_prune,
    ):
        with pre_process.activate(_pdf_check_run(source)) as run:
            result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin", delete_stale=True)

    assert result["errors"] == []
    assert run.skipped
    assert mock_list.called
    assert mock_prune.called


@pytest.mark.asyncio
async def test_full_inventory_encrypted_pdf_is_not_refetched(local_env):
    """load_inventory: once skipped, an unchanged PDF is not selected again."""
    from soliplex.agents.manifest import pre_process

    source = "gitea:admin:test:all"
    row = {
        "uri": "locked.pdf",
        "file_bytes": (_PDF_FIXTURES / "user_password.pdf").read_bytes(),
        "content-type": "application/pdf",
        "sha256": "aabbcc",
        "metadata": {},
    }
    with patch("soliplex.agents.scm.app.get_data", AsyncMock(return_value=[row])):
        with pre_process.activate(_pdf_check_run(source)) as run:
            first = await scm_app.load_inventory(SCM.GITEA, "test", "admin", source=source)
            second = await scm_app.load_inventory(SCM.GITEA, "test", "admin", source=source)

    assert first["errors"] == []
    assert [r["uri"] for r in first["to_process"]] == ["locked.pdf"]
    assert second["to_process"] == []
    assert len(run.skipped) == 1
    assert not (local_store.source_dir(source) / "locked.pdf").exists()


def _failing_sync_provider(**overrides):
    """A provider with one new commit touching one file, for failure tests."""
    provider = MagicMock()
    provider.list_commits_since = AsyncMock(return_value=[{"sha": "def456", "message": "Update file1.md"}])
    provider.get_commit_details = AsyncMock(
        return_value={"sha": "def456", "files": [{"filename": "file1.md", "status": "modified"}]}
    )
    provider.get_single_file = AsyncMock(
        return_value={
            "uri": "file1.md",
            "file_bytes": b"x",
            "content-type": "text/markdown",
            "sha256": "aa",
            "metadata": {},
        }
    )
    provider.list_issues = AsyncMock(return_value=[])
    for name, value in overrides.items():
        setattr(provider, name, value)
    return provider


@pytest.mark.asyncio
async def test_fetch_failure_is_reported_and_holds_the_cursor(local_env):
    """A file that fails to fetch reaches `errors`, so the runner sees it."""
    source = "gitea:admin:test:all"
    local_state.set_sync_meta(source, "abc123", branch="main")
    provider = _failing_sync_provider(get_single_file=AsyncMock(side_effect=RuntimeError("502 Bad Gateway")))

    with (
        patch("soliplex.agents.scm.app.get_scm", return_value=provider),
        patch("soliplex.agents.scm.app.list_all_uris", AsyncMock(return_value=[])) as mock_list,
    ):
        result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin", delete_stale=True)

    assert result["errors"] == [{"uri": "file1.md", "error": "502 Bad Gateway", "stage": "fetch"}]
    assert result["new_commit_sha"] == "abc123"
    assert local_state.get_sync_meta(source)["last_commit_sha"] == "abc123"
    # A failed fetch leaves the inventory incomplete, so nothing is pruned.
    assert not mock_list.called


@pytest.mark.asyncio
async def test_commit_failure_is_reported_and_does_not_skip_the_commit(local_env):
    """A commit whose file list can't be read must not be advanced past.

    Its changed files are unknown, so moving the cursor beyond it would lose
    them for good: no later run would look at that commit again.
    """
    source = "gitea:admin:test:all"
    local_state.set_sync_meta(source, "abc123", branch="main")
    provider = _failing_sync_provider(get_commit_details=AsyncMock(side_effect=RuntimeError("timeout")))

    with patch("soliplex.agents.scm.app.get_scm", return_value=provider):
        result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin")

    assert result["errors"] == [{"uri": "commit:def456", "error": "timeout", "stage": "commit"}]
    assert result["new_commit_sha"] == "abc123"
    assert local_state.get_sync_meta(source)["last_commit_sha"] == "abc123"
    provider.get_single_file.assert_not_awaited()
