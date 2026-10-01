"""Tests for soliplex.agents.server.routes.scm module."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from soliplex.agents.server import app
from soliplex.agents.server.auth import AuthenticatedUser


# Override auth dependency for testing
async def mock_get_current_user():
    return AuthenticatedUser(identity="test-user", method="none")


@pytest.fixture
def client():
    """Create test client with auth disabled."""
    from soliplex.agents.server.auth import get_current_user

    app.dependency_overrides[get_current_user] = mock_get_current_user
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def mock_scm_provider():
    """Create a mock SCM provider."""
    provider = MagicMock()
    provider.list_issues = AsyncMock(return_value=[])
    provider.list_repo_files = AsyncMock(return_value=[])
    return provider


# Tests for /api/v1/scm/{scm}/issues endpoint


def test_list_issues_github_success(client, mock_scm_provider):
    """Test listing GitHub issues."""
    issues = [
        {
            "number": 1,
            "title": "Test Issue 1",
            "body": "Issue body 1",
            "state": "open",
            "created_at": "2024-01-01T00:00:00Z",
            "assignee": None,
            "comment_count": 2,
        },
        {
            "number": 2,
            "title": "Test Issue 2",
            "body": "Issue body 2",
            "state": "closed",
            "created_at": "2024-01-02T00:00:00Z",
            "assignee": "user1",
            "comment_count": 0,
        },
    ]
    mock_scm_provider.list_issues = AsyncMock(return_value=issues)

    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.get_scm.return_value = mock_scm_provider

        response = client.get(
            "/api/v1/scm/github/issues",
            params={"repo_name": "test-repo", "owner": "test-owner"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["scm"] == "github"
        assert data["repo"] == "test-repo"
        assert data["owner"] == "test-owner"
        assert data["issue_count"] == 2
        assert len(data["issues"]) == 2


def test_list_issues_gitea_success(client, mock_scm_provider):
    """Test listing Gitea issues."""
    mock_scm_provider.list_issues = AsyncMock(return_value=[{"number": 1, "title": "Gitea Issue", "body": "Body"}])

    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.get_scm.return_value = mock_scm_provider

        response = client.get(
            "/api/v1/scm/gitea/issues",
            params={"repo_name": "test-repo", "owner": "admin"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["scm"] == "gitea"
        assert data["issue_count"] == 1


def test_list_issues_missing_owner(client, mock_scm_provider):
    """Test listing issues returns 422 when owner is not specified."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.get_scm.return_value = mock_scm_provider

        response = client.get(
            "/api/v1/scm/github/issues",
            params={"repo_name": "test-repo"},
        )

        assert response.status_code == 422


def test_list_issues_empty(client, mock_scm_provider):
    """Test listing issues when repository has no issues."""
    mock_scm_provider.list_issues = AsyncMock(return_value=[])

    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.get_scm.return_value = mock_scm_provider

        response = client.get(
            "/api/v1/scm/github/issues",
            params={"repo_name": "empty-repo", "owner": "test-owner"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["issue_count"] == 0
        assert data["issues"] == []


# Tests for /api/v1/scm/{scm}/repo endpoint


def test_get_repo_github_success(client, mock_scm_provider):
    """Test getting GitHub repository files."""
    files = [
        {
            "name": "README.md",
            "uri": "/owner/repo/README.md",
            "sha256": "abc123",
            "content-type": "text/markdown",
            "last_updated": "2024-01-01T00:00:00Z",
        },
        {
            "name": "docs/guide.md",
            "uri": "/owner/repo/docs/guide.md",
            "sha256": "def456",
            "content-type": "text/markdown",
            "last_updated": "2024-01-02T00:00:00Z",
        },
    ]
    mock_scm_provider.list_repo_files = AsyncMock(return_value=files)

    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.get_scm.return_value = mock_scm_provider

        response = client.get(
            "/api/v1/scm/github/repo",
            params={"repo_name": "test-repo", "owner": "test-owner"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["scm"] == "github"
        assert data["file_count"] == 2
        assert len(data["files"]) == 2
        # Verify file bytes are not included
        for f in data["files"]:
            assert "file_bytes" not in f


def test_get_repo_gitea_success(client, mock_scm_provider):
    """Test getting Gitea repository files."""
    files = [{"name": "config.md", "uri": "/admin/repo/config.md", "sha256": "xyz789"}]
    mock_scm_provider.list_repo_files = AsyncMock(return_value=files)

    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.get_scm.return_value = mock_scm_provider

        response = client.get(
            "/api/v1/scm/gitea/repo",
            params={"repo_name": "test-repo", "owner": "admin"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["scm"] == "gitea"
        assert data["file_count"] == 1


def test_get_repo_empty(client, mock_scm_provider):
    """Test getting repository with no matching files."""
    mock_scm_provider.list_repo_files = AsyncMock(return_value=[])

    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.get_scm.return_value = mock_scm_provider

        response = client.get(
            "/api/v1/scm/github/repo",
            params={"repo_name": "empty-repo", "owner": "test-owner"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["file_count"] == 0
        assert data["files"] == []


def test_get_repo_uses_settings_extensions(client, mock_scm_provider):
    """Test that repo listing uses configured extensions."""
    mock_scm_provider.list_repo_files = AsyncMock(return_value=[])

    with (
        patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app,
        patch("soliplex.agents.server.routes.scm.settings") as mock_settings,
    ):
        mock_scm_app.get_scm.return_value = mock_scm_provider
        mock_settings.extensions = ["md", "pdf", "docx"]

        response = client.get(
            "/api/v1/scm/github/repo",
            params={"repo_name": "test-repo", "owner": "test-owner"},
        )

        assert response.status_code == 200
        mock_scm_provider.list_repo_files.assert_called_once()
        call_args = mock_scm_provider.list_repo_files.call_args
        assert call_args[0][2] == ["md", "pdf", "docx"]


# Tests for SCM enum validation in routes


def test_invalid_scm_value_issues(client):
    """Test invalid SCM value returns 422."""
    response = client.get(
        "/api/v1/scm/invalid-scm/issues",
        params={"repo_name": "test-repo"},
    )
    assert response.status_code == 422


def test_invalid_scm_value_repo(client):
    """Test invalid SCM value returns 422."""
    response = client.get(
        "/api/v1/scm/invalid-scm/repo",
        params={"repo_name": "test-repo"},
    )
    assert response.status_code == 422
