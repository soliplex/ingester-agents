"""Tests for soliplex.agents.server.routes.fs module."""

import json
import os
import tempfile
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
    os.unlink(temp_path)


@pytest.fixture
def temp_document_dir():
    """Create a temporary directory with test documents."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        with open(os.path.join(tmpdir, "test.md"), "w") as f:
            f.write("# Test Document\n\nThis is a test.")
        with open(os.path.join(tmpdir, "readme.md"), "w") as f:
            f.write("# README\n\nProject description.")
        yield tmpdir


class TestValidateConfigRoute:
    """Tests for /api/v1/fs/validate-config endpoint."""

    def test_validate_config_success(self, client, temp_inventory_file):
        """Test successful config validation."""
        with patch("soliplex.agents.server.routes.fs.fs_app") as mock_fs_app:
            mock_fs_app.read_config = AsyncMock(
                return_value=[
                    {"path": "doc1.md", "valid": True, "metadata": {"content-type": "text/markdown"}},
                ]
            )
            mock_fs_app.check_config.return_value = [
                {"path": "doc1.md", "valid": True, "metadata": {"content-type": "text/markdown"}},
            ]

            response = client.post(
                "/api/v1/fs/validate-config",
                data={"config_file": temp_inventory_file},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["total_files"] == 1
            assert data["invalid_count"] == 0

    def test_validate_config_with_invalid_files(self, client, temp_inventory_file):
        """Test config validation with invalid files."""
        with patch("soliplex.agents.server.routes.fs.fs_app") as mock_fs_app:
            mock_fs_app.read_config = AsyncMock(
                return_value=[
                    {"path": "doc1.md", "metadata": {"content-type": "text/markdown"}},
                    {"path": "archive.zip", "metadata": {"content-type": "application/zip"}},
                ]
            )
            mock_fs_app.check_config.return_value = [
                {"path": "doc1.md", "valid": True, "metadata": {"content-type": "text/markdown"}},
                {
                    "path": "archive.zip",
                    "valid": False,
                    "reason": "Unsupported content type",
                    "metadata": {"content-type": "application/zip"},
                },
            ]

            response = client.post(
                "/api/v1/fs/validate-config",
                data={"config_file": temp_inventory_file},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["invalid_count"] == 1
            assert len(data["invalid_files"]) == 1
            assert data["invalid_files"][0]["path"] == "archive.zip"

    def test_validate_config_file_not_found(self, client):
        """Test validation with non-existent file."""
        response = client.post(
            "/api/v1/fs/validate-config",
            data={"config_file": "/nonexistent/path/inventory.json"},
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


class TestBuildConfigRoute:
    """Tests for /api/v1/fs/build-config endpoint."""

    def test_build_config_success(self, client, temp_document_dir):
        """Test successful config build."""
        with patch("soliplex.agents.server.routes.fs.fs_app") as mock_fs_app:
            mock_fs_app.build_config = AsyncMock(
                return_value=[
                    {"path": "test.md", "sha256": "abc123", "metadata": {"size": 50, "content-type": "text/markdown"}},
                    {"path": "readme.md", "sha256": "def456", "metadata": {"size": 100, "content-type": "text/markdown"}},
                ]
            )

            response = client.post(
                "/api/v1/fs/build-config",
                data={"path": temp_document_dir},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["files_count"] == 2
            assert "inventory_file" in data
            assert len(data["inventory"]) == 2

    def test_build_config_directory_not_found(self, client):
        """Test build config with non-existent directory."""
        response = client.post(
            "/api/v1/fs/build-config",
            data={"path": "/nonexistent/directory"},
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_build_config_path_is_file(self, client, temp_inventory_file):
        """Test build config when path is a file, not directory."""
        response = client.post(
            "/api/v1/fs/build-config",
            data={"path": temp_inventory_file},
        )

        assert response.status_code == 400
        assert "not a directory" in response.json()["detail"].lower()


class TestCheckStatusRoute:
    """Tests for /api/v1/fs/check-status endpoint."""

    def test_check_status_success(self, client, temp_inventory_file):
        """Test successful status check."""
        with (
            patch("soliplex.agents.server.routes.fs.fs_app") as mock_fs_app,
            patch("soliplex.agents.client.check_status") as mock_check_status,
        ):
            mock_fs_app.read_config = AsyncMock(
                return_value=[
                    {"path": "doc1.md", "sha256": "abc123"},
                    {"path": "doc2.md", "sha256": "def456"},
                ]
            )
            mock_check_status.return_value = [
                {"path": "doc1.md", "sha256": "abc123", "status": "new"},
            ]

            response = client.post(
                "/api/v1/fs/check-status",
                data={"config_file": temp_inventory_file, "source": "test-source"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["total_files"] == 2
            assert data["files_to_process"] == 1

    def test_check_status_with_detail(self, client, temp_inventory_file):
        """Test status check with detail flag."""
        with (
            patch("soliplex.agents.server.routes.fs.fs_app") as mock_fs_app,
            patch("soliplex.agents.client.check_status") as mock_check_status,
        ):
            to_process = [{"path": "doc1.md", "sha256": "abc123", "status": "new"}]
            mock_fs_app.read_config = AsyncMock(return_value=[{"path": "doc1.md", "sha256": "abc123"}])
            mock_check_status.return_value = to_process

            response = client.post(
                "/api/v1/fs/check-status",
                data={"config_file": temp_inventory_file, "source": "test-source", "detail": "true"},
            )

            assert response.status_code == 200
            data = response.json()
            assert "files" in data
            assert data["files"] == to_process

    def test_check_status_file_not_found(self, client):
        """Test status check with non-existent file."""
        response = client.post(
            "/api/v1/fs/check-status",
            data={"config_file": "/nonexistent/inventory.json", "source": "test-source"},
        )

        assert response.status_code == 404


class TestRunInventoryRoute:
    """Tests for /api/v1/fs/run-inventory endpoint."""

    def test_run_inventory_success(self, client, temp_inventory_file):
        """Test successful inventory run."""
        with patch("soliplex.agents.server.routes.fs.fs_app") as mock_fs_app:
            mock_fs_app.load_inventory = AsyncMock(
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
                "/api/v1/fs/run-inventory",
                data={"config_file": temp_inventory_file, "source": "test-source"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["inventory_count"] == 1
            assert data["to_process_count"] == 1
            assert data["ingested_count"] == 1
            assert data["error_count"] == 0
            assert data["batch_id"] == 123

    def test_run_inventory_with_errors(self, client, temp_inventory_file):
        """Test inventory run with some errors."""
        with patch("soliplex.agents.server.routes.fs.fs_app") as mock_fs_app:
            mock_fs_app.load_inventory = AsyncMock(
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
                "/api/v1/fs/run-inventory",
                data={"config_file": temp_inventory_file, "source": "test-source"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["error_count"] == 1
            assert len(data["errors"]) == 1

    def test_run_inventory_with_directory(self, client, temp_document_dir):
        """Test inventory run when directory is provided."""
        with patch("soliplex.agents.server.routes.fs.fs_app") as mock_fs_app:
            mock_fs_app.build_config = AsyncMock(
                return_value=[{"path": "test.md", "sha256": "abc123", "metadata": {"size": 50}}]
            )
            mock_fs_app.load_inventory = AsyncMock(
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
                "/api/v1/fs/run-inventory",
                data={"config_file": temp_document_dir, "source": "test-source"},
            )

            assert response.status_code == 200
            # build_config should have been called
            mock_fs_app.build_config.assert_called_once()

    def test_run_inventory_with_all_options(self, client, temp_inventory_file):
        """Test inventory run with all optional parameters."""
        with patch("soliplex.agents.server.routes.fs.fs_app") as mock_fs_app:
            mock_fs_app.load_inventory = AsyncMock(
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
                "/api/v1/fs/run-inventory",
                data={
                    "config_file": temp_inventory_file,
                    "source": "test-source",
                    "start": "5",
                    "end": "10",
                    "start_workflows": "false",
                    "workflow_definition_id": "wf-123",
                    "param_set_id": "params-456",
                    "priority": "5",
                },
            )

            assert response.status_code == 200
            mock_fs_app.load_inventory.assert_called_once()
            call_kwargs = mock_fs_app.load_inventory.call_args
            assert call_kwargs[1]["start_workflows"] is False
            assert call_kwargs[1]["workflow_definition_id"] == "wf-123"
            assert call_kwargs[1]["param_set_id"] == "params-456"
            assert call_kwargs[1]["priority"] == 5

    def test_run_inventory_path_not_found(self, client):
        """Test inventory run with non-existent path."""
        response = client.post(
            "/api/v1/fs/run-inventory",
            data={"config_file": "/nonexistent/path", "source": "test-source"},
        )

        assert response.status_code == 404
