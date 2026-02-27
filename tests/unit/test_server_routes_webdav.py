"""Tests for soliplex.agents.server.routes.webdav module."""

from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from soliplex.agents.server import app
from soliplex.agents.server.auth import AuthenticatedUser


# Override auth dependency for testing
async def mock_get_current_user():
    return AuthenticatedUser(identity="test-user", method="none")


app.dependency_overrides = {}


@pytest.fixture
def client():
    """Create test client with auth disabled."""
    from soliplex.agents.server.auth import get_current_user

    app.dependency_overrides[get_current_user] = mock_get_current_user
    yield TestClient(app)
    app.dependency_overrides.clear()


# --- /api/v1/webdav/validate-config ---


def test_validate_config_with_webdav_path(client):
    """Test config validation with WebDAV path."""
    with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
        mock_app.build_config = AsyncMock(
            return_value=[
                {"path": "test.md", "metadata": {"content-type": "text/markdown"}},
                {"path": "readme.pdf", "metadata": {"content-type": "application/pdf"}},
            ]
        )
        mock_app.check_config.return_value = [
            {"path": "test.md", "valid": True, "metadata": {"content-type": "text/markdown"}},
            {"path": "readme.pdf", "valid": True, "metadata": {"content-type": "application/pdf"}},
        ]

        response = client.post(
            "/api/v1/webdav/validate-config",
            data={
                "config_path": "/documents",
                "webdav_url": "https://webdav.example.com",
                "webdav_username": "user",
                "webdav_password": "pass",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["total_files"] == 2


def test_validate_config_with_invalid_files(client):
    """Test config validation with invalid files."""
    with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
        mock_app.build_config = AsyncMock(
            return_value=[
                {"path": "doc1.md", "metadata": {"content-type": "text/markdown"}},
                {"path": "archive.zip", "metadata": {"content-type": "application/zip"}},
            ]
        )
        mock_app.check_config.return_value = [
            {"path": "doc1.md", "valid": True, "metadata": {"content-type": "text/markdown"}},
            {
                "path": "archive.zip",
                "valid": False,
                "reason": "Unsupported content type",
                "metadata": {"content-type": "application/zip"},
            },
        ]

        response = client.post(
            "/api/v1/webdav/validate-config",
            data={"config_path": "/documents"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["invalid_count"] == 1
        assert len(data["invalid_files"]) == 1


# --- /api/v1/webdav/check-status ---


def test_check_status_success(client):
    """Test successful status check."""
    with (
        patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app,
        patch("soliplex.agents.client.check_status") as mock_check_status,
    ):
        mock_app.build_config = AsyncMock(
            return_value=[
                {"path": "doc1.md", "sha256": "abc123"},
                {"path": "doc2.md", "sha256": "def456"},
            ]
        )
        mock_check_status.return_value = [
            {"path": "doc1.md", "sha256": "abc123", "status": "new"},
        ]

        response = client.post(
            "/api/v1/webdav/check-status",
            data={"config_path": "/documents", "source": "test-source"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["total_files"] == 2
        assert data["files_to_process"] == 1


def test_check_status_with_detail(client):
    """Test status check with detail flag."""
    with (
        patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app,
        patch("soliplex.agents.client.check_status") as mock_check_status,
    ):
        to_process = [{"path": "doc1.md", "sha256": "abc123", "status": "new"}]
        mock_app.build_config = AsyncMock(return_value=[{"path": "doc1.md", "sha256": "abc123"}])
        mock_check_status.return_value = to_process

        response = client.post(
            "/api/v1/webdav/check-status",
            data={"config_path": "/documents", "source": "test-source", "detail": "true"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "files" in data
        assert data["files"] == to_process


# --- /api/v1/webdav/run-inventory ---


def test_run_inventory_success(client):
    """Test successful inventory run."""
    with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
        mock_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [{"path": "doc1.md"}],
                "to_process": [{"path": "doc1.md"}],
                "batch_id": 123,
                "ingested": [{"path": "doc1.md"}],
                "errors": [],
                "workflow_result": {"status": "started"},
            }
        )

        response = client.post(
            "/api/v1/webdav/run-inventory",
            data={"config_path": "/documents", "source": "test-source"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["inventory_count"] == 1
        assert data["to_process_count"] == 1
        assert data["ingested_count"] == 1
        assert data["error_count"] == 0
        assert data["batch_id"] == 123


def test_run_inventory_with_webdav_path(client):
    """Test inventory run with WebDAV path."""
    with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
        mock_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [{"path": "test.md"}],
                "to_process": [],
                "batch_id": None,
                "ingested": [],
                "errors": [],
                "workflow_result": None,
            }
        )

        response = client.post(
            "/api/v1/webdav/run-inventory",
            data={
                "config_path": "/documents",
                "source": "test-source",
                "webdav_url": "https://webdav.example.com",
            },
        )

        assert response.status_code == 200
        mock_app.load_inventory.assert_called_once()


def test_run_inventory_with_errors(client):
    """Test inventory run with some errors."""
    with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
        mock_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [{"path": "doc1.md"}, {"path": "doc2.md"}],
                "to_process": [{"path": "doc1.md"}, {"path": "doc2.md"}],
                "batch_id": 123,
                "ingested": [{"path": "doc1.md"}],
                "errors": [{"path": "doc2.md", "error": "Failed to process"}],
                "workflow_result": None,
            }
        )

        response = client.post(
            "/api/v1/webdav/run-inventory",
            data={"config_path": "/documents", "source": "test-source"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["error_count"] == 1
        assert len(data["errors"]) == 1


def test_run_inventory_server_error(client):
    """Test inventory run with server error."""
    with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
        mock_app.load_inventory = AsyncMock(side_effect=Exception("Connection failed"))

        response = client.post(
            "/api/v1/webdav/run-inventory",
            data={"config_path": "/documents", "source": "test-source"},
        )

        assert response.status_code == 500


def test_run_inventory_with_all_options(client):
    """Test inventory run with all optional parameters."""
    with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
        mock_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [],
                "to_process": [],
                "batch_id": None,
                "ingested": [],
                "errors": [],
                "workflow_result": None,
            }
        )

        response = client.post(
            "/api/v1/webdav/run-inventory",
            data={
                "config_path": "/documents",
                "source": "test-source",
                "start": "10",
                "end": "50",
                "start_workflows": "true",
                "workflow_definition_id": "my-workflow",
                "param_set_id": "my-params",
                "priority": "5",
                "webdav_url": "https://webdav.example.com",
                "webdav_username": "user",
                "webdav_password": "pass",
                "endpoint_url": "http://localhost:9000/api/v1",
            },
        )

        assert response.status_code == 200
        # Verify load_inventory was called with the right path and source
        mock_app.load_inventory.assert_called_once()
        call_args = mock_app.load_inventory.call_args
        # Verify positional arguments
        assert call_args[0][0] == "/documents"  # config_path
        assert call_args[0][1] == "test-source"  # source


# --- /api/v1/webdav/run-from-urls ---


def test_run_from_urls_success(client):
    """Test successful run from URLs."""
    with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
        mock_app.load_inventory_from_urls = AsyncMock(
            return_value={
                "inventory": [{"path": "/documents/doc1.md"}],
                "to_process": [{"path": "/documents/doc1.md"}],
                "batch_id": 456,
                "ingested": [{"path": "/documents/doc1.md"}],
                "errors": [],
                "workflow_result": None,
            }
        )

        response = client.post(
            "/api/v1/webdav/run-from-urls",
            data={"urls_file": "/tmp/urls.txt", "source": "test-source"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["inventory_count"] == 1
        assert data["ingested_count"] == 1
        assert data["batch_id"] == 456
        mock_app.load_inventory_from_urls.assert_called_once()


def test_run_from_urls_error(client):
    """Test run from URLs with error."""
    with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
        mock_app.load_inventory_from_urls = AsyncMock(side_effect=FileNotFoundError("URLs file not found"))

        response = client.post(
            "/api/v1/webdav/run-from-urls",
            data={"urls_file": "/tmp/missing.txt", "source": "test-source"},
        )

        assert response.status_code == 404


def test_run_from_urls_server_error(client):
    """Test run from URLs with server error."""
    with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
        mock_app.load_inventory_from_urls = AsyncMock(side_effect=Exception("Connection failed"))

        response = client.post(
            "/api/v1/webdav/run-from-urls",
            data={"urls_file": "/tmp/urls.txt", "source": "test-source"},
        )

        assert response.status_code == 500
