"""Tests for soliplex.agents.server.routes.manifest module."""

from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from soliplex.agents.server import app
from soliplex.agents.server.auth import AuthenticatedUser
from soliplex.agents.server.manifest_queue import EnqueueResult


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


# --- POST /api/v1/manifest/run ---


def _write_manifest(directory, name, mid):
    """Write a minimal valid manifest YAML file and return its path."""
    path = directory / name
    path.write_text(
        f"id: {mid}\nname: Manifest {mid}\nsource: src-{mid}\ncomponents:\n  - type: fs\n    name: comp\n    path: /data\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def manifest_dir(tmp_path):
    """Point the route at *tmp_path* as MANIFEST_DIR."""
    with patch("soliplex.agents.server.routes.manifest.settings") as ms:
        ms.manifest_dir = str(tmp_path)
        yield tmp_path


@pytest.fixture
def mock_enqueue():
    """Patch the queue so no manifest actually runs."""
    with patch(
        "soliplex.agents.server.manifest_queue.enqueue_manifest",
        new_callable=AsyncMock,
        return_value=EnqueueResult.QUEUED,
    ) as mock:
        yield mock


def test_run_manifest_queues_it(client, manifest_dir, mock_enqueue):
    path = _write_manifest(manifest_dir, "a.yml", "aaa")

    response = client.post("/api/v1/manifest/run", data={"manifest_id": "aaa"})

    assert response.status_code == 202
    assert response.json() == {"status": "queued", "manifest_id": "aaa"}
    mock_enqueue.assert_awaited_once_with("aaa", str(path))


def test_run_manifest_already_pending_is_coalesced(client, manifest_dir, mock_enqueue):
    _write_manifest(manifest_dir, "a.yml", "aaa")
    mock_enqueue.return_value = EnqueueResult.COALESCED

    response = client.post("/api/v1/manifest/run", data={"manifest_id": "aaa"})

    assert response.status_code == 202
    assert response.json() == {"status": "already_queued", "manifest_id": "aaa"}


def test_run_manifest_unknown_id(client, manifest_dir, mock_enqueue):
    _write_manifest(manifest_dir, "a.yml", "aaa")

    response = client.post("/api/v1/manifest/run", data={"manifest_id": "nope"})

    assert response.status_code == 404
    assert "'nope'" in response.json()["detail"]
    assert "failed to load" not in response.json()["detail"]
    mock_enqueue.assert_not_awaited()


def test_run_manifest_unknown_id_hints_at_invalid_files(client, manifest_dir, mock_enqueue):
    (manifest_dir / "broken.yml").write_text("id: [unclosed\n", encoding="utf-8")

    response = client.post("/api/v1/manifest/run", data={"manifest_id": "nope"})

    assert response.status_code == 404
    assert "1 manifest file(s) failed to load" in response.json()["detail"]


def test_run_manifest_duplicate_id(client, manifest_dir, mock_enqueue):
    _write_manifest(manifest_dir, "a.yml", "dup")
    _write_manifest(manifest_dir, "b.yml", "dup")

    response = client.post("/api/v1/manifest/run", data={"manifest_id": "dup"})

    assert response.status_code == 409
    mock_enqueue.assert_not_awaited()


def test_run_manifest_without_manifest_dir(client, mock_enqueue):
    with patch("soliplex.agents.server.routes.manifest.settings") as ms:
        ms.manifest_dir = None
        response = client.post("/api/v1/manifest/run", data={"manifest_id": "aaa"})

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]
    mock_enqueue.assert_not_awaited()


def test_run_manifest_dir_is_not_a_directory(client, tmp_path, mock_enqueue):
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    with patch("soliplex.agents.server.routes.manifest.settings") as ms:
        ms.manifest_dir = str(not_a_dir)
        response = client.post("/api/v1/manifest/run", data={"manifest_id": "aaa"})

    assert response.status_code == 503
    assert "not a directory" in response.json()["detail"]


def test_run_manifest_queue_not_running(client, manifest_dir, mock_enqueue):
    _write_manifest(manifest_dir, "a.yml", "aaa")
    mock_enqueue.return_value = EnqueueResult.NOT_STARTED

    response = client.post("/api/v1/manifest/run", data={"manifest_id": "aaa"})

    assert response.status_code == 503
    assert "queue is not running" in response.json()["detail"]


def test_run_manifest_requires_manifest_id(client, manifest_dir, mock_enqueue):
    response = client.post("/api/v1/manifest/run", data={})

    assert response.status_code == 422


# --- GET /api/v1/manifest/queue ---


def test_queue_lists_pending_manifests_sorted(client):
    with patch(
        "soliplex.agents.server.manifest_queue.pending_manifests",
        return_value=frozenset({"bbb", "aaa"}),
    ):
        response = client.get("/api/v1/manifest/queue")

    assert response.status_code == 200
    assert response.json() == {"pending": ["aaa", "bbb"]}


def test_queue_empty(client):
    response = client.get("/api/v1/manifest/queue")

    assert response.status_code == 200
    assert response.json() == {"pending": []}
