"""Unit tests for incremental sync functionality."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from soliplex.agents import client
from soliplex.agents.scm import app as scm_app


def create_async_context_manager(return_value):
    """Create an async context manager that returns the given value."""
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=return_value)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@pytest.mark.asyncio
async def test_incremental_sync_no_state():
    """First sync should fallback to full sync when no state exists."""
    with patch("soliplex.agents.scm.app.client") as mock_client:
        # Mock no sync state (last_commit_sha is None)
        mock_client.get_sync_state = AsyncMock(return_value={"source_id": "gitea:admin:test", "last_commit_sha": None})

        # Mock full sync dependencies
        mock_client.find_batch_for_source = AsyncMock(return_value=None)
        mock_client.create_batch = AsyncMock(return_value=1)
        mock_client.check_status = AsyncMock(return_value=[])

        with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
            mock_provider = MagicMock()
            mock_provider.list_repo_files = AsyncMock(return_value=[])
            mock_provider.list_issues = AsyncMock(return_value=[])
            mock_get_scm.return_value = mock_provider

            from soliplex.agents.config import SCM

            result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin")

            # Should have called full inventory load which returns inventory and to_process
            assert "inventory" in result
            assert "to_process" in result


@pytest.mark.asyncio
async def test_incremental_sync_with_changes():
    """Incremental sync should process only changed files."""
    with patch("soliplex.agents.scm.app.client") as mock_client:
        # Mock sync state with existing commit
        mock_client.get_sync_state = AsyncMock(
            return_value={
                "source_id": "gitea:admin:test",
                "last_commit_sha": "abc123",
                "branch": "main",
            }
        )

        # Mock batch management
        mock_client.find_batch_for_source = AsyncMock(return_value=1)
        mock_client.do_ingest = AsyncMock(return_value={"result": "success"})
        mock_client.update_sync_state = AsyncMock(return_value={"last_commit_sha": "def456"})

        with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
            mock_provider = MagicMock()

            # Mock 2 new commits
            mock_provider.list_commits_since = AsyncMock(
                return_value=[
                    {"sha": "def456", "message": "Update file1.md"},
                    {"sha": "ghi789", "message": "Update file2.md"},
                ]
            )

            # Mock commit details
            mock_provider.get_commit_details = AsyncMock(
                side_effect=[
                    {
                        "sha": "def456",
                        "files": [{"filename": "file1.md", "status": "modified"}],
                    },
                    {
                        "sha": "ghi789",
                        "files": [{"filename": "file2.md", "status": "modified"}],
                    },
                ]
            )

            # Mock file fetching
            mock_provider.get_single_file = AsyncMock(
                return_value={
                    "uri": "/admin/test/file1.md",
                    "file_bytes": b"test content",
                    "content-type": "text/markdown",
                    "metadata": {},
                }
            )

            mock_get_scm.return_value = mock_provider

            from soliplex.agents.config import SCM

            result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin")

            assert result["status"] == "synced"
            assert result["commits_processed"] == 2
            assert result["files_changed"] == 2


@pytest.mark.asyncio
async def test_incremental_sync_up_to_date():
    """No changes should return up-to-date status."""
    with patch("soliplex.agents.scm.app.client") as mock_client:
        mock_client.get_sync_state = AsyncMock(
            return_value={
                "source_id": "gitea:admin:test",
                "last_commit_sha": "abc123",
            }
        )

        with patch("soliplex.agents.scm.app.get_scm") as mock_get_scm:
            mock_provider = MagicMock()

            # No new commits
            mock_provider.list_commits_since = AsyncMock(return_value=[])

            mock_get_scm.return_value = mock_provider

            from soliplex.agents.config import SCM

            result = await scm_app.incremental_sync(SCM.GITEA, "test", "admin")

            assert result["status"] == "up-to-date"
            assert result["commits_processed"] == 0
            assert result["files_changed"] == 0


@pytest.mark.asyncio
async def test_get_sync_state(mock_response, mock_session):
    """Test getting sync state from ingester."""
    mock_resp = mock_response(
        200,
        {
            "source_id": "gitea:admin:test",
            "last_commit_sha": "abc123",
            "last_sync_date": "2026-01-16T10:00:00",
            "branch": "main",
        },
    )

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(return_value=create_async_context_manager(mock_resp))

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)

        result = await client.get_sync_state("gitea:admin:test")

        assert result["source_id"] == "gitea:admin:test"
        assert result["last_commit_sha"] == "abc123"


@pytest.mark.asyncio
async def test_get_sync_state_not_found(mock_response):
    """Test getting sync state when none exists."""
    mock_resp = mock_response(404, None)

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(return_value=create_async_context_manager(mock_resp))

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)

        result = await client.get_sync_state("gitea:admin:test")

        # Should return default state when 404
        assert result["source_id"] == "gitea:admin:test"
        assert result["last_commit_sha"] is None


@pytest.mark.asyncio
async def test_update_sync_state(mock_response):
    """Test updating sync state."""
    mock_resp = mock_response(
        200,
        {
            "source_id": "gitea:admin:test",
            "last_commit_sha": "def456",
            "last_sync_date": "2026-01-16T11:00:00",
            "branch": "main",
        },
    )

    mock_sess = MagicMock()
    mock_sess.put = MagicMock(return_value=create_async_context_manager(mock_resp))

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)

        result = await client.update_sync_state("gitea:admin:test", "def456", "main", {"test": "metadata"})

        assert result["last_commit_sha"] == "def456"


@pytest.mark.asyncio
async def test_reset_sync_state(mock_response):
    """Test resetting sync state."""
    mock_resp = mock_response(200, {"message": "Sync state deleted"})

    mock_sess = MagicMock()
    mock_sess.delete = MagicMock(return_value=create_async_context_manager(mock_resp))

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)

        result = await client.reset_sync_state("gitea:admin:test")

        assert "message" in result


@pytest.mark.asyncio
async def test_reset_sync_state_not_found(mock_response):
    """Test resetting sync state when none exists."""
    mock_resp = mock_response(404, None)

    mock_sess = MagicMock()
    mock_sess.delete = MagicMock(return_value=create_async_context_manager(mock_resp))

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)

        result = await client.reset_sync_state("gitea:admin:test")

        assert result["message"] == "No sync state found for gitea:admin:test"


@pytest.mark.asyncio
async def test_list_commits_since(mock_response):
    """Test listing commits since a specific SHA."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_default_owner(self):
            return "admin"

        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider()

    # Create mock response for first page with commits
    first_page_resp = mock_response(
        200,
        [
            {"sha": "commit3", "message": "Third commit"},
            {"sha": "commit2", "message": "Second commit"},
            {"sha": "commit1", "message": "First commit"},  # This is our marker
        ],
    )

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(return_value=create_async_context_manager(first_page_resp))

    # Create async context manager for get_session
    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            commits = await provider.list_commits_since("test", "admin", "commit1", "main")

            # Should return commits before commit1 (commit3 and commit2)
            # But the function logic stops when it finds the marker
            # Since we found the marker immediately, commits array should have
            # commit3 and commit2 (everything up until commit1)
            assert len(commits) == 2
            assert commits[0]["sha"] == "commit3"
            assert commits[1]["sha"] == "commit2"


@pytest.mark.asyncio
async def test_list_commits_since_no_marker(mock_response):
    """Test listing commits when no marker is provided (get all recent)."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_default_owner(self):
            return "admin"

        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider()

    # Create mock response
    page_resp = mock_response(
        200,
        [
            {"sha": "commit3", "message": "Third commit"},
            {"sha": "commit2", "message": "Second commit"},
        ],
    )

    # Second call returns empty to stop pagination
    empty_resp = mock_response(200, [])

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(
        side_effect=[
            create_async_context_manager(page_resp),
            create_async_context_manager(empty_resp),
        ]
    )

    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            # No marker = get all recent commits
            commits = await provider.list_commits_since("test", "admin", None, "main")

            # Should return all commits
            assert len(commits) == 2
            assert commits[0]["sha"] == "commit3"


@pytest.mark.asyncio
async def test_get_commit_details(mock_response):
    """Test getting detailed commit information."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_default_owner(self):
            return "admin"

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
    mock_sess.get = MagicMock(return_value=create_async_context_manager(commit_resp))

    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            result = await provider.get_commit_details("test", "admin", "abc123")

            assert result["sha"] == "abc123"
            assert len(result["files"]) == 2


@pytest.mark.asyncio
async def test_get_single_file(mock_response):
    """Test getting a single file from repository."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_default_owner(self):
            return "admin"

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
        },
    )

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(return_value=create_async_context_manager(file_resp))

    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            result = await provider.get_single_file("test", "admin", "docs/test.md", "main")

            assert result["name"] == "test.md"
            assert result["uri"] == "docs/test.md"
            assert "sha256" in result


# Additional coverage tests for exception handling


@pytest.mark.asyncio
async def test_get_sync_state_exception():
    """Test get_sync_state exception handling."""
    mock_sess = MagicMock()
    mock_sess.get = MagicMock(side_effect=Exception("Network error"))

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)

        result = await client.get_sync_state("gitea:admin:test")

        assert "error" in result
        assert "Network error" in result["error"]


@pytest.mark.asyncio
async def test_update_sync_state_exception():
    """Test update_sync_state exception handling."""
    mock_sess = MagicMock()
    mock_sess.put = MagicMock(side_effect=Exception("Network error"))

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)

        result = await client.update_sync_state("gitea:admin:test", "abc123")

        assert "error" in result
        assert "Network error" in result["error"]


@pytest.mark.asyncio
async def test_update_sync_state_without_metadata(mock_response):
    """Test update_sync_state without metadata (covers the if metadata branch)."""
    mock_resp = mock_response(
        200,
        {
            "source_id": "gitea:admin:test",
            "last_commit_sha": "def456",
            "branch": "main",
        },
    )

    mock_sess = MagicMock()
    mock_sess.put = MagicMock(return_value=create_async_context_manager(mock_resp))

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)

        # Call without metadata parameter
        result = await client.update_sync_state("gitea:admin:test", "def456", "main")

        assert result["last_commit_sha"] == "def456"


@pytest.mark.asyncio
async def test_reset_sync_state_exception():
    """Test reset_sync_state exception handling."""
    mock_sess = MagicMock()
    mock_sess.delete = MagicMock(side_effect=Exception("Network error"))

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)

        result = await client.reset_sync_state("gitea:admin:test")

        assert "error" in result
        assert "Network error" in result["error"]


@pytest.mark.asyncio
async def test_list_commits_since_pagination(mock_response):
    """Test list_commits_since with pagination across multiple pages."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_default_owner(self):
            return "admin"

        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider()

    # First page (limit=2 commits)
    first_page = mock_response(200, [{"sha": "commit3"}, {"sha": "commit2"}])
    # Second page with marker
    second_page = mock_response(200, [{"sha": "commit1"}])

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(
        side_effect=[
            create_async_context_manager(first_page),
            create_async_context_manager(second_page),
        ]
    )

    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            # Use limit=2 to force pagination
            commits = await provider.list_commits_since("test", "admin", "commit1", "main", limit=2)

            # Should return commit3 and commit2 (stopping at commit1)
            assert len(commits) == 2


@pytest.mark.asyncio
async def test_list_commits_since_empty_first_page(mock_response):
    """Test list_commits_since when first page is empty."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_default_owner(self):
            return "admin"

        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider()

    # Empty response
    empty_resp = mock_response(200, [])

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(return_value=create_async_context_manager(empty_resp))

    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            commits = await provider.list_commits_since("test", "admin", "commit1", "main")

            assert len(commits) == 0


@pytest.mark.asyncio
async def test_list_commits_since_max_pages_limit(mock_response):
    """Test list_commits_since when max pages limit is reached (covers while loop exit on max_pages)."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_default_owner(self):
            return "admin"

        def get_base_url(self):
            return "http://test"

        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider()

    # Create 11 pages of responses (to exceed max_pages=10)
    # Each page returns limit=100 commits to trigger pagination
    responses = []
    for i in range(11):
        # Each page returns exactly limit commits so pagination continues
        page_commits = [{"sha": f"commit_{i}_{j}"} for j in range(100)]
        responses.append(create_async_context_manager(mock_response(200, page_commits)))

    mock_sess = MagicMock()
    mock_sess.get = MagicMock(side_effect=responses)

    session_ctx = create_async_context_manager(mock_sess)

    with patch.object(provider, "get_session", return_value=session_ctx):
        with patch.object(provider, "validate_response", AsyncMock()):
            # No marker, so we should collect all until max_pages is hit
            commits = await provider.list_commits_since("test", "admin", None, "main", limit=100)

            # Should have collected 10 pages * 100 commits = 1000 commits (stopped at max_pages)
            assert len(commits) == 1000
