"""Tests for soliplex.agents.server.routes.manifest module."""

from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from soliplex.agents.server import app
from soliplex.agents.server.auth import AuthenticatedUser
from soliplex.agents.server.locks import get_manifest_lock
from soliplex.agents.server.locks import reset_locks


async def mock_get_current_user():
    return AuthenticatedUser(identity="test-user", method="none")


@pytest.fixture(autouse=True)
def _clean_locks():
    """Reset the lock registry between tests."""
    reset_locks()
    yield
    reset_locks()


@pytest.fixture
def client():
    """Create test client with auth disabled."""
    from soliplex.agents.server.auth import get_current_user

    app.dependency_overrides[get_current_user] = mock_get_current_user
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_manifest(mid="test", name="Test"):
    """Return a minimal Manifest stub."""
    from soliplex.agents.config import Manifest

    return Manifest(
        id=mid,
        name=name,
        source=f"src-{mid}",
        components=[{"type": "fs", "name": "c", "path": "/p"}],
    )


# --- POST /api/v1/manifest/run ---


def test_run_manifests_success(client, tmp_path):
    """Test running manifests from a directory."""
    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifests_from_dir.return_value = [_make_manifest("m1")]
        mock_runner.run_manifest = AsyncMock(
            return_value={
                "manifest_id": "m1",
                "manifest_name": "Test",
                "results": [
                    {
                        "component": "docs",
                        "result": {"ingested": [1], "errors": []},
                    },
                ],
            }
        )

        response = client.post(
            "/api/v1/manifest/run",
            data={"path": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["manifest_count"] == 1
        assert data["total_components"] == 1
        assert data["total_errors"] == 0


def test_run_manifests_with_errors(client, tmp_path):
    """Test running manifests when components have errors."""
    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifests_from_dir.return_value = [_make_manifest("m1")]
        mock_runner.run_manifest = AsyncMock(
            return_value={
                "manifest_id": "m1",
                "manifest_name": "Test",
                "results": [
                    {
                        "component": "docs",
                        "result": {"ingested": [1], "errors": []},
                    },
                    {"component": "web", "error": "connection failed"},
                ],
            }
        )

        response = client.post(
            "/api/v1/manifest/run",
            data={"path": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["total_components"] == 2
        assert data["total_errors"] == 1


def test_run_manifests_file_not_found(client):
    """Test running manifests with non-existent path."""
    response = client.post(
        "/api/v1/manifest/run",
        data={"path": "/nonexistent/path"},
    )

    assert response.status_code == 404


def test_run_manifests_validation_error(client, tmp_path):
    """Test running manifests with duplicate IDs."""
    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifests_from_dir.side_effect = ValueError("Duplicate manifest IDs found: ['same']")

        response = client.post(
            "/api/v1/manifest/run",
            data={"path": str(tmp_path)},
        )

        assert response.status_code == 422
        assert "Duplicate" in response.json()["detail"]


def test_run_manifests_unexpected_error(client, tmp_path):
    """Test running manifests with unexpected error."""
    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifests_from_dir.side_effect = RuntimeError("unexpected")

        response = client.post(
            "/api/v1/manifest/run",
            data={"path": str(tmp_path)},
        )

        assert response.status_code == 500


def test_run_manifests_single_file(client, tmp_path):
    """Test running manifests from a single file."""
    f = tmp_path / "test.yml"
    f.write_text("id: t\n")

    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifest.return_value = _make_manifest("t")
        mock_runner.run_manifest = AsyncMock(
            return_value={
                "manifest_id": "t",
                "manifest_name": "Test",
                "results": [],
            }
        )

        response = client.post(
            "/api/v1/manifest/run",
            data={"path": str(f)},
        )

        assert response.status_code == 200
        mock_runner.load_manifest.assert_called_once_with(str(f))


def test_run_manifests_returns_409_when_busy(client, tmp_path):
    """Test that /run returns 409 when a manifest is already running."""
    lock = get_manifest_lock("busy-m")
    # Simulate a running manifest by holding the lock
    lock._locked = True  # low-level flag; fine for sync test

    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifests_from_dir.return_value = [_make_manifest("busy-m")]

        response = client.post(
            "/api/v1/manifest/run",
            data={"path": str(tmp_path)},
        )

    assert response.status_code == 409
    assert "busy-m" in response.json()["detail"]


# --- POST /api/v1/manifest/run-single ---


def test_run_single_manifest_success(client, tmp_path):
    """Test running a single manifest file."""
    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifest.return_value = _make_manifest("t")
        mock_runner.run_manifest = AsyncMock(
            return_value={
                "manifest_id": "t",
                "manifest_name": "Test",
                "results": [
                    {
                        "component": "c",
                        "result": {"ingested": [], "errors": []},
                    },
                ],
            }
        )

        response = client.post(
            "/api/v1/manifest/run-single",
            data={"path": str(tmp_path / "test.yml")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["manifest_id"] == "t"
        assert data["component_count"] == 1
        assert data["error_count"] == 0


def test_run_single_manifest_not_found(client):
    """Test running a single manifest with non-existent file."""
    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifest.side_effect = FileNotFoundError("not found")

        response = client.post(
            "/api/v1/manifest/run-single",
            data={"path": "/nonexistent.yml"},
        )

        assert response.status_code == 404


def test_run_single_manifest_invalid_yaml(client):
    """Test running a single manifest with invalid YAML."""
    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifest.side_effect = ValueError("Invalid YAML")

        response = client.post(
            "/api/v1/manifest/run-single",
            data={"path": "/bad.yml"},
        )

        assert response.status_code == 422


def test_run_single_manifest_type_error(client):
    """Test running a single manifest with non-mapping YAML."""
    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifest.side_effect = TypeError("Expected a YAML mapping")

        response = client.post(
            "/api/v1/manifest/run-single",
            data={"path": "/list.yml"},
        )

        assert response.status_code == 422


def test_run_single_manifest_unexpected_error(client):
    """Test running a single manifest with unexpected error."""
    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifest.side_effect = RuntimeError("boom")

        response = client.post(
            "/api/v1/manifest/run-single",
            data={"path": "/err.yml"},
        )

        assert response.status_code == 500


def test_run_single_manifest_returns_409_when_busy(client):
    """Test that /run-single returns 409 when the manifest is running."""
    lock = get_manifest_lock("busy-s")
    lock._locked = True

    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifest.return_value = _make_manifest("busy-s")

        response = client.post(
            "/api/v1/manifest/run-single",
            data={"path": "/some.yml"},
        )

    assert response.status_code == 409
    assert "busy-s" in response.json()["detail"]


# --- POST /api/v1/manifest/validate ---


def test_validate_manifest_file(client, tmp_path):
    """Test validating a single manifest file."""
    from soliplex.agents.config import Manifest

    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifest.return_value = Manifest(
            id="t",
            name="Test",
            source="s",
            schedule={"cron": "0 * * * *"},
            components=[{"type": "fs", "name": "c", "path": "/p"}],
        )

        f = tmp_path / "test.yml"
        f.write_text("id: t\n")

        response = client.post(
            "/api/v1/manifest/validate",
            data={"path": str(f)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["manifest_count"] == 1
        assert data["manifests"][0]["id"] == "t"
        assert data["manifests"][0]["has_schedule"] is True


def test_validate_manifest_dir(client, tmp_path):
    """Test validating a directory of manifests."""
    from soliplex.agents.config import Manifest

    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifests_from_dir.return_value = [
            Manifest(
                id="a",
                name="A",
                source="s",
                components=[{"type": "fs", "name": "c", "path": "/p"}],
            ),
            Manifest(
                id="b",
                name="B",
                source="s",
                components=[{"type": "fs", "name": "c", "path": "/p"}],
            ),
        ]

        response = client.post(
            "/api/v1/manifest/validate",
            data={"path": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["manifest_count"] == 2


def test_validate_manifest_not_found(client):
    """Test validating a non-existent path."""
    response = client.post(
        "/api/v1/manifest/validate",
        data={"path": "/nonexistent/path"},
    )

    assert response.status_code == 404


def test_validate_manifest_invalid(client, tmp_path):
    """Test validating an invalid manifest."""
    f = tmp_path / "bad.yml"
    f.write_text("id: t\n")

    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifest.side_effect = ValueError("bad yaml")

        response = client.post(
            "/api/v1/manifest/validate",
            data={"path": str(f)},
        )

        assert response.status_code == 422


def test_validate_manifest_unexpected_error(client, tmp_path):
    """Test validating with unexpected error."""
    f = tmp_path / "err.yml"
    f.write_text("id: t\n")

    with patch("soliplex.agents.server.routes.manifest.manifest_runner") as mock_runner:
        mock_runner.load_manifest.side_effect = RuntimeError("unexpected")

        response = client.post(
            "/api/v1/manifest/validate",
            data={"path": str(f)},
        )

        assert response.status_code == 500
