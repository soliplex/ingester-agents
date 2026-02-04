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
    provider.get_default_owner.return_value = "default-owner"
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


def test_list_issues_default_owner(client, mock_scm_provider):
    """Test listing issues uses default owner when not specified."""
    mock_scm_provider.list_issues = AsyncMock(return_value=[])

    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.get_scm.return_value = mock_scm_provider

        response = client.get(
            "/api/v1/scm/github/issues",
            params={"repo_name": "test-repo"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["owner"] == "default-owner"


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


# Tests for /api/v1/scm/run-inventory endpoint


def test_run_inventory_github_success(client):
    """Test successful GitHub inventory run."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [{"uri": "/owner/repo/doc.md"}, {"uri": "/owner/repo/issues/1"}],
                "to_process": [{"uri": "/owner/repo/doc.md"}],
                "ingested": ["/owner/repo/doc.md"],
                "errors": [],
                "workflow_result": {"status": "started", "run_group_id": 123},
            }
        )

        response = client.post(
            "/api/v1/scm/run-inventory",
            data={
                "scm": "github",
                "repo_name": "test-repo",
                "owner": "test-owner",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["scm"] == "github"
        assert data["repo"] == "test-repo"
        assert data["owner"] == "test-owner"
        assert data["inventory_count"] == 2
        assert data["to_process_count"] == 1
        assert data["ingested_count"] == 1
        assert data["error_count"] == 0


def test_run_inventory_gitea_success(client):
    """Test successful Gitea inventory run."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [],
                "to_process": [],
                "ingested": [],
                "errors": [],
                "workflow_result": None,
            }
        )

        response = client.post(
            "/api/v1/scm/run-inventory",
            data={
                "scm": "gitea",
                "repo_name": "test-repo",
                "owner": "admin",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["scm"] == "gitea"


def test_run_inventory_with_errors(client):
    """Test inventory run with some errors."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [{"uri": "/owner/repo/doc1.md"}, {"uri": "/owner/repo/doc2.md"}],
                "to_process": [{"uri": "/owner/repo/doc1.md"}, {"uri": "/owner/repo/doc2.md"}],
                "ingested": ["/owner/repo/doc1.md"],
                "errors": [{"uri": "/owner/repo/doc2.md", "error": "API rate limit exceeded"}],
                "workflow_result": None,
            }
        )

        response = client.post(
            "/api/v1/scm/run-inventory",
            data={
                "scm": "github",
                "repo_name": "test-repo",
                "owner": "test-owner",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["error_count"] == 1
        assert len(data["errors"]) == 1
        assert "rate limit" in data["errors"][0]["error"].lower()


def test_run_inventory_with_all_options(client):
    """Test inventory run with all optional parameters."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [],
                "to_process": [],
                "ingested": [],
                "errors": [],
                "workflow_result": None,
            }
        )

        response = client.post(
            "/api/v1/scm/run-inventory",
            data={
                "scm": "github",
                "repo_name": "test-repo",
                "owner": "test-owner",
                "start_workflows": "false",
                "workflow_definition_id": "wf-custom",
                "param_set_id": "params-custom",
                "priority": "10",
            },
        )

        assert response.status_code == 200
        mock_scm_app.load_inventory.assert_called_once()
        call_kwargs = mock_scm_app.load_inventory.call_args[1]
        assert call_kwargs["start_workflows"] is False
        assert call_kwargs["workflow_definition_id"] == "wf-custom"
        assert call_kwargs["param_set_id"] == "params-custom"
        assert call_kwargs["priority"] == 10


def test_run_inventory_nothing_to_process(client):
    """Test inventory run when nothing needs processing."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [{"uri": "/owner/repo/doc.md"}],
                "to_process": [],
                "ingested": [],
                "errors": [],
                "workflow_result": None,
            }
        )

        response = client.post(
            "/api/v1/scm/run-inventory",
            data={
                "scm": "github",
                "repo_name": "test-repo",
                "owner": "test-owner",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["inventory_count"] == 1
        assert data["to_process_count"] == 0
        assert data["ingested_count"] == 0


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


def test_invalid_scm_value_run_inventory(client):
    """Test invalid SCM value returns 422."""
    response = client.post(
        "/api/v1/scm/run-inventory",
        data={
            "scm": "invalid-scm",
            "repo_name": "test-repo",
            "owner": "test-owner",
        },
    )
    assert response.status_code == 422


# Tests for /api/v1/scm/incremental-sync endpoint


def test_incremental_sync_github_success(client):
    """Test successful GitHub incremental sync."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.incremental_sync = AsyncMock(
            return_value={
                "status": "synced",
                "commits_processed": 3,
                "files_changed": 5,
                "files_removed": 1,
                "ingested": ["/owner/repo/doc.md", "/owner/repo/guide.md"],
                "errors": [],
                "workflow_result": {"status": "started", "run_group_id": 456},
                "new_commit_sha": "abc123def456",
            }
        )

        response = client.post(
            "/api/v1/scm/incremental-sync",
            data={
                "scm": "github",
                "repo_name": "test-repo",
                "owner": "test-owner",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "synced"
        assert data["scm"] == "github"
        assert data["repo"] == "test-repo"
        assert data["owner"] == "test-owner"
        assert data["branch"] == "main"
        assert data["commits_processed"] == 3
        assert data["files_changed"] == 5
        assert data["files_removed"] == 1
        assert data["ingested_count"] == 2
        assert data["error_count"] == 0
        assert data["new_commit_sha"] == "abc123def456"


def test_incremental_sync_gitea_success(client):
    """Test successful Gitea incremental sync."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.incremental_sync = AsyncMock(
            return_value={
                "status": "synced",
                "commits_processed": 1,
                "files_changed": 2,
                "files_removed": 0,
                "ingested": ["/admin/repo/config.md"],
                "errors": [],
                "workflow_result": None,
                "new_commit_sha": "xyz789",
            }
        )

        response = client.post(
            "/api/v1/scm/incremental-sync",
            data={
                "scm": "gitea",
                "repo_name": "test-repo",
                "owner": "admin",
                "branch": "develop",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["scm"] == "gitea"
        assert data["branch"] == "develop"


def test_incremental_sync_up_to_date(client):
    """Test incremental sync when repo is up to date."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.incremental_sync = AsyncMock(
            return_value={
                "status": "up-to-date",
                "commits_processed": 0,
                "files_changed": 0,
                "ingested": [],
                "errors": [],
            }
        )

        response = client.post(
            "/api/v1/scm/incremental-sync",
            data={
                "scm": "github",
                "repo_name": "test-repo",
                "owner": "test-owner",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "up-to-date"
        assert data["commits_processed"] == 0
        assert data["ingested_count"] == 0


def test_incremental_sync_with_errors(client):
    """Test incremental sync with some errors."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.incremental_sync = AsyncMock(
            return_value={
                "status": "synced",
                "commits_processed": 2,
                "files_changed": 3,
                "files_removed": 0,
                "ingested": ["/owner/repo/doc1.md"],
                "errors": [{"uri": "/owner/repo/doc2.md", "error": "API error"}],
                "workflow_result": None,
                "new_commit_sha": "abc123",
            }
        )

        response = client.post(
            "/api/v1/scm/incremental-sync",
            data={
                "scm": "github",
                "repo_name": "test-repo",
                "owner": "test-owner",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["error_count"] == 1
        assert len(data["errors"]) == 1


def test_incremental_sync_error_response(client):
    """Test incremental sync when an error occurs."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.incremental_sync = AsyncMock(
            return_value={
                "error": "Failed to get sync state: connection timeout",
            }
        )

        response = client.post(
            "/api/v1/scm/incremental-sync",
            data={
                "scm": "github",
                "repo_name": "test-repo",
                "owner": "test-owner",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert "connection timeout" in data["error"]


def test_incremental_sync_with_all_options(client):
    """Test incremental sync with all optional parameters."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        mock_scm_app.incremental_sync = AsyncMock(
            return_value={
                "status": "synced",
                "commits_processed": 1,
                "files_changed": 1,
                "files_removed": 0,
                "ingested": ["/owner/repo/doc.md"],
                "errors": [],
                "workflow_result": {"status": "started"},
                "new_commit_sha": "abc123",
            }
        )

        response = client.post(
            "/api/v1/scm/incremental-sync",
            data={
                "scm": "github",
                "repo_name": "test-repo",
                "owner": "test-owner",
                "branch": "feature-branch",
                "start_workflows": "false",
                "workflow_definition_id": "wf-custom",
                "param_set_id": "params-custom",
                "priority": "5",
            },
        )

        assert response.status_code == 200
        mock_scm_app.incremental_sync.assert_called_once()
        call_kwargs = mock_scm_app.incremental_sync.call_args[1]
        assert call_kwargs["branch"] == "feature-branch"
        assert call_kwargs["start_workflows"] is False
        assert call_kwargs["workflow_definition_id"] == "wf-custom"
        assert call_kwargs["param_set_id"] == "params-custom"
        assert call_kwargs["priority"] == 5


def test_incremental_sync_falls_back_to_full_sync(client):
    """Test incremental sync falls back to full sync when no sync state exists."""
    with patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app:
        # When no sync state exists, incremental_sync calls load_inventory internally
        # and returns the full inventory result
        mock_scm_app.incremental_sync = AsyncMock(
            return_value={
                "inventory": [{"uri": "/owner/repo/doc.md"}],
                "to_process": [{"uri": "/owner/repo/doc.md"}],
                "ingested": ["/owner/repo/doc.md"],
                "errors": [],
                "workflow_result": None,
            }
        )

        response = client.post(
            "/api/v1/scm/incremental-sync",
            data={
                "scm": "github",
                "repo_name": "test-repo",
                "owner": "test-owner",
            },
        )

        assert response.status_code == 200
        data = response.json()
        # Should return ok status and include ingested files
        assert data["ingested_count"] == 1


def test_invalid_scm_value_incremental_sync(client):
    """Test invalid SCM value returns 422 for incremental sync."""
    response = client.post(
        "/api/v1/scm/incremental-sync",
        data={
            "scm": "invalid-scm",
            "repo_name": "test-repo",
            "owner": "test-owner",
        },
    )
    assert response.status_code == 422
