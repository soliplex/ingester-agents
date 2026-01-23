"""Tests for soliplex.agents.server.routes.webdav module."""

import json
import tempfile
from pathlib import Path
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


@pytest.fixture
def temp_inventory_file():
    """Create a temporary inventory file for testing."""
    inventory = [
        {
            "path": "doc1.md",
            "sha256": "abc123",
            "metadata": {"size": 100, "content-type": "text/markdown"},
        },
        {
            "path": "doc2.pdf",
            "sha256": "def456",
            "metadata": {"size": 200, "content-type": "application/pdf"},
        },
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(inventory, f)
        temp_path = f.name
    yield temp_path
    Path(temp_path).unlink()


class TestValidateConfigRoute:
    """Tests for /api/v1/webdav/validate-config endpoint."""

    def test_validate_config_success_with_local_file(self, client, temp_inventory_file):
        """Test successful config validation with local file."""
        with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
            mock_app.resolve_config_path = AsyncMock(
                return_value=(
                    [{"path": "doc1.md", "valid": True, "metadata": {"content-type": "text/markdown"}}],
                    str(Path(temp_inventory_file).parent),
                )
            )
            mock_app.check_config.return_value = [
                {"path": "doc1.md", "valid": True, "metadata": {"content-type": "text/markdown"}},
            ]

            response = client.post(
                "/api/v1/webdav/validate-config",
                data={"config_path": temp_inventory_file},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["total_files"] == 1
            assert data["invalid_count"] == 0

    def test_validate_config_with_webdav_path(self, client):
        """Test config validation with WebDAV path."""
        with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
            mock_app.resolve_config_path = AsyncMock(
                return_value=(
                    [
                        {"path": "test.md", "metadata": {"content-type": "text/markdown"}},
                        {"path": "readme.pdf", "metadata": {"content-type": "application/pdf"}},
                    ],
                    "/documents",
                )
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

    def test_validate_config_with_invalid_files(self, client):
        """Test config validation with invalid files."""
        with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
            mock_app.resolve_config_path = AsyncMock(
                return_value=(
                    [
                        {"path": "doc1.md", "metadata": {"content-type": "text/markdown"}},
                        {"path": "archive.zip", "metadata": {"content-type": "application/zip"}},
                    ],
                    "/documents",
                )
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


class TestBuildConfigRoute:
    """Tests for /api/v1/webdav/build-config endpoint."""

    def test_build_config_success(self, client):
        """Test successful config build from WebDAV."""
        with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
            mock_app.build_config = AsyncMock(
                return_value=[
                    {
                        "path": "test.md",
                        "sha256": "abc123",
                        "metadata": {"size": 50, "content-type": "text/markdown"},
                    },
                    {
                        "path": "readme.pdf",
                        "sha256": "def456",
                        "metadata": {"size": 100, "content-type": "application/pdf"},
                    },
                ]
            )

            response = client.post(
                "/api/v1/webdav/build-config",
                data={
                    "webdav_path": "/documents",
                    "webdav_url": "https://webdav.example.com",
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["files_count"] == 2
            assert len(data["inventory"]) == 2

    def test_build_config_error(self, client):
        """Test build config with error."""
        with patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app:
            mock_app.build_config = AsyncMock(side_effect=ValueError("WebDAV URL is required"))

            response = client.post(
                "/api/v1/webdav/build-config",
                data={"webdav_path": "/documents"},
            )

            assert response.status_code == 400


class TestCheckStatusRoute:
    """Tests for /api/v1/webdav/check-status endpoint."""

    def test_check_status_success(self, client, temp_inventory_file):
        """Test successful status check."""
        with (
            patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app,
            patch("soliplex.agents.client.check_status") as mock_check_status,
        ):
            mock_app.resolve_config_path = AsyncMock(
                return_value=(
                    [
                        {"path": "doc1.md", "sha256": "abc123"},
                        {"path": "doc2.md", "sha256": "def456"},
                    ],
                    str(Path(temp_inventory_file).parent),
                )
            )
            mock_check_status.return_value = [
                {"path": "doc1.md", "sha256": "abc123", "status": "new"},
            ]

            response = client.post(
                "/api/v1/webdav/check-status",
                data={"config_path": temp_inventory_file, "source": "test-source"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["total_files"] == 2
            assert data["files_to_process"] == 1

    def test_check_status_with_detail(self, client):
        """Test status check with detail flag."""
        with (
            patch("soliplex.agents.server.routes.webdav.webdav_app") as mock_app,
            patch("soliplex.agents.client.check_status") as mock_check_status,
        ):
            to_process = [{"path": "doc1.md", "sha256": "abc123", "status": "new"}]
            mock_app.resolve_config_path = AsyncMock(return_value=([{"path": "doc1.md", "sha256": "abc123"}], "/documents"))
            mock_check_status.return_value = to_process

            response = client.post(
                "/api/v1/webdav/check-status",
                data={"config_path": "/documents", "source": "test-source", "detail": "true"},
            )

            assert response.status_code == 200
            data = response.json()
            assert "files" in data
            assert data["files"] == to_process


class TestRunInventoryRoute:
    """Tests for /api/v1/webdav/run-inventory endpoint."""

    def test_run_inventory_success(self, client, temp_inventory_file):
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
                data={"config_path": temp_inventory_file, "source": "test-source"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["inventory_count"] == 1
            assert data["to_process_count"] == 1
            assert data["ingested_count"] == 1
            assert data["error_count"] == 0
            assert data["batch_id"] == 123

    def test_run_inventory_with_webdav_path(self, client):
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

    def test_run_inventory_with_errors(self, client):
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

    def test_run_inventory_with_all_options(self, client):
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
