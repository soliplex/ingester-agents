"""Tests for soliplex.agents.server.routes.web module."""

import json
from unittest.mock import AsyncMock
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


# --- POST /api/v1/web/run-inventory ---


def test_run_inventory_success(client):
    """Test successful web inventory run."""
    with patch("soliplex.agents.server.routes.web.web_app") as mock_web_app:
        mock_web_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [{"path": "http://example.com"}],
                "to_process": [{"path": "http://example.com"}],
                "batch_id": 42,
                "ingested": [{"result": "success"}],
                "errors": [],
                "workflow_result": None,
            }
        )

        response = client.post(
            "/api/v1/web/run-inventory",
            data={
                "urls": json.dumps(["http://example.com"]),
                "source": "web-test",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["inventory_count"] == 1
        assert data["to_process_count"] == 1
        assert data["ingested_count"] == 1
        assert data["error_count"] == 0
        assert data["batch_id"] == 42


def test_run_inventory_with_errors(client):
    """Test web inventory run with ingestion errors."""
    with patch("soliplex.agents.server.routes.web.web_app") as mock_web_app:
        mock_web_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [{"path": "http://a.com"}, {"path": "http://b.com"}],
                "to_process": [{"path": "http://a.com"}, {"path": "http://b.com"}],
                "batch_id": 42,
                "ingested": [{"result": "success"}],
                "errors": [{"uri": "http://b.com", "error": "fetch failed"}],
                "workflow_result": None,
            }
        )

        response = client.post(
            "/api/v1/web/run-inventory",
            data={
                "urls": json.dumps(["http://a.com", "http://b.com"]),
                "source": "web-test",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["error_count"] == 1
        assert len(data["errors"]) == 1


def test_run_inventory_with_all_options(client):
    """Test web inventory with all optional parameters."""
    with patch("soliplex.agents.server.routes.web.web_app") as mock_web_app:
        mock_web_app.load_inventory = AsyncMock(
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
            "/api/v1/web/run-inventory",
            data={
                "urls": json.dumps(["http://example.com"]),
                "source": "web-test",
                "start_workflows": "true",
                "workflow_definition_id": "wf-1",
                "param_set_id": "ps-1",
                "priority": "5",
                "metadata": json.dumps({"project": "test"}),
            },
        )

        assert response.status_code == 200
        mock_web_app.load_inventory.assert_called_once()
        call_kwargs = mock_web_app.load_inventory.call_args
        assert call_kwargs.kwargs["start_workflows"] is True
        assert call_kwargs.kwargs["workflow_definition_id"] == "wf-1"
        assert call_kwargs.kwargs["extra_metadata"] == {"project": "test"}


def test_run_inventory_invalid_json(client):
    """Test web inventory with invalid JSON urls."""
    response = client.post(
        "/api/v1/web/run-inventory",
        data={
            "urls": "not-json",
            "source": "web-test",
        },
    )

    assert response.status_code == 422
    assert "Invalid JSON" in response.json()["detail"]


def test_run_inventory_urls_not_array(client):
    """Test web inventory when urls is not an array."""
    response = client.post(
        "/api/v1/web/run-inventory",
        data={
            "urls": json.dumps("http://example.com"),
            "source": "web-test",
        },
    )

    assert response.status_code == 422
    assert "must be a JSON array" in response.json()["detail"]


def test_run_inventory_value_error(client):
    """Test web inventory with validation error."""
    with patch("soliplex.agents.server.routes.web.web_app") as mock_web_app:
        mock_web_app.load_inventory = AsyncMock(
            side_effect=ValueError("start_workflows requires both workflow_definition_id and param_set_id")
        )

        response = client.post(
            "/api/v1/web/run-inventory",
            data={
                "urls": json.dumps(["http://example.com"]),
                "source": "web-test",
            },
        )

        assert response.status_code == 422


def test_run_inventory_unexpected_error(client):
    """Test web inventory with unexpected error."""
    with patch("soliplex.agents.server.routes.web.web_app") as mock_web_app:
        mock_web_app.load_inventory = AsyncMock(side_effect=RuntimeError("boom"))

        response = client.post(
            "/api/v1/web/run-inventory",
            data={
                "urls": json.dumps(["http://example.com"]),
                "source": "web-test",
            },
        )

        assert response.status_code == 500


# --- POST /api/v1/web/run-from-file ---


def test_run_from_file_success(client):
    """Test successful web inventory from uploaded file."""
    with patch("soliplex.agents.server.routes.web.web_app") as mock_web_app:
        mock_web_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [{"path": "http://example.com"}],
                "to_process": [{"path": "http://example.com"}],
                "batch_id": 42,
                "ingested": [{"result": "success"}],
                "errors": [],
                "workflow_result": None,
            }
        )

        file_content = b"http://example.com\nhttp://other.com\n"
        response = client.post(
            "/api/v1/web/run-from-file",
            data={"source": "web-test"},
            files={"file": ("urls.txt", file_content, "text/plain")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["ingested_count"] == 1
        # Verify URLs were parsed from file content
        mock_web_app.load_inventory.assert_called_once()
        call_args = mock_web_app.load_inventory.call_args
        assert call_args[0][0] == ["http://example.com", "http://other.com"]


def test_run_from_file_unexpected_error(client):
    """Test web inventory from uploaded file with unexpected error."""
    with patch("soliplex.agents.server.routes.web.web_app") as mock_web_app:
        mock_web_app.load_inventory = AsyncMock(side_effect=RuntimeError("boom"))

        file_content = b"http://example.com\n"
        response = client.post(
            "/api/v1/web/run-from-file",
            data={"source": "web-test"},
            files={"file": ("urls.txt", file_content, "text/plain")},
        )

        assert response.status_code == 500


def test_run_from_file_with_metadata(client):
    """Test web inventory from uploaded file with extra metadata."""
    with patch("soliplex.agents.server.routes.web.web_app") as mock_web_app:
        mock_web_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [],
                "to_process": [],
                "batch_id": None,
                "ingested": [],
                "errors": [],
                "workflow_result": None,
            }
        )

        file_content = b"http://example.com\n"
        response = client.post(
            "/api/v1/web/run-from-file",
            data={
                "source": "web-test",
                "metadata": json.dumps({"env": "prod"}),
            },
            files={"file": ("urls.txt", file_content, "text/plain")},
        )

        assert response.status_code == 200
        call_kwargs = mock_web_app.load_inventory.call_args.kwargs
        assert call_kwargs["extra_metadata"] == {"env": "prod"}


def test_run_from_file_empty(client):
    """Test web inventory from uploaded empty file."""
    with patch("soliplex.agents.server.routes.web.web_app") as mock_web_app:
        mock_web_app.load_inventory = AsyncMock(
            return_value={
                "inventory": [],
                "to_process": [],
                "batch_id": None,
                "ingested": [],
                "errors": [],
                "workflow_result": None,
            }
        )

        file_content = b"\n\n"
        response = client.post(
            "/api/v1/web/run-from-file",
            data={"source": "web-test"},
            files={"file": ("urls.txt", file_content, "text/plain")},
        )

        assert response.status_code == 200
        call_args = mock_web_app.load_inventory.call_args
        assert call_args[0][0] == []
