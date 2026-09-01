"""Tests for soliplex.agents.server.routes.manifest module."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from soliplex.agents.server import app
from soliplex.agents.server.auth import AuthenticatedUser


async def mock_get_current_user():
    return AuthenticatedUser(identity="test-user", method="none")


@pytest.fixture
def client():
    """Create test client with auth disabled."""
    from soliplex.agents.server.auth import get_current_user

    app.dependency_overrides[get_current_user] = mock_get_current_user
    yield TestClient(app)
    app.dependency_overrides.clear()


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
