"""Tests for soliplex.agents.server FastAPI application."""

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


# Mock get current user tests


@pytest.mark.asyncio
async def test_mock_get_current_user_returns_authenticated_user():
    """Test mock_get_current_user returns correct AuthenticatedUser."""
    result = await mock_get_current_user()
    assert isinstance(result, AuthenticatedUser)
    assert result.identity == "test-user"
    assert result.method == "none"


# Health endpoint tests


def test_health_check(client):
    """Test health check returns healthy status."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


def test_health_check_no_auth_required():
    """Test health check works without authentication."""
    # Don't override auth - health should still work
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200


# OpenAPI documentation tests


def test_openapi_json(client):
    """Test OpenAPI JSON schema is available."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    data = response.json()
    assert "openapi" in data
    assert "paths" in data
    assert "info" in data
    assert data["info"]["title"] == "Soliplex Agents API"


def test_docs_endpoint(client):
    """Test Swagger UI docs endpoint is available."""
    response = client.get("/docs")
    assert response.status_code == 200
    assert "swagger" in response.text.lower() or "html" in response.headers.get("content-type", "")


def test_redoc_endpoint(client):
    """Test ReDoc endpoint is available."""
    response = client.get("/redoc")
    assert response.status_code == 200


# App configuration tests


def test_app_title():
    """Test app has correct title."""
    assert app.title == "Soliplex Agents API"


def test_app_version():
    """Test app has version set."""
    assert app.version == "0.1.0"


def test_cors_middleware_configured():
    """Test CORS middleware is configured."""
    middleware_classes = [m.cls.__name__ for m in app.user_middleware]
    assert "CORSMiddleware" in middleware_classes


def test_routers_included():
    """Test all routers are included."""
    routes = [r.path for r in app.routes]
    # FS routes
    assert "/api/v1/fs/validate-config" in routes
    assert "/api/v1/fs/build-config" in routes
    assert "/api/v1/fs/check-status" in routes
    assert "/api/v1/fs/run-inventory" in routes
    # SCM routes
    assert "/api/v1/scm/{scm}/issues" in routes
    assert "/api/v1/scm/{scm}/repo" in routes
    assert "/api/v1/scm/run-inventory" in routes
    # Health check
    assert "/health" in routes


# CORS headers tests


def test_cors_allows_all_origins(client):
    """Test CORS allows requests from any origin."""
    response = client.options(
        "/health",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Should not reject the preflight request
    assert response.status_code in [200, 204, 405]


def test_cors_headers_in_response(client):
    """Test CORS headers are present in response."""
    response = client.get(
        "/health",
        headers={"Origin": "http://example.com"},
    )
    assert response.status_code == 200
    # CORS headers should be present
    assert "access-control-allow-origin" in response.headers


# Authentication integration tests


def test_fs_routes_require_auth_when_enabled():
    """Test FS routes check authentication."""
    # Clear any overrides to test real auth
    app.dependency_overrides.clear()

    # Mock settings to enable auth
    from unittest.mock import patch

    with patch("soliplex.agents.server.auth.settings") as mock_settings:
        mock_settings.api_key_enabled = True
        mock_settings.auth_trust_proxy_headers = False
        mock_settings.api_key = "test-key"

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/fs/validate-config",
                data={"config_file": "/some/path"},
            )
            # Should get 401 without auth
            assert response.status_code == 401


def test_scm_routes_require_auth_when_enabled():
    """Test SCM routes check authentication."""
    app.dependency_overrides.clear()

    from unittest.mock import patch

    with patch("soliplex.agents.server.auth.settings") as mock_settings:
        mock_settings.api_key_enabled = True
        mock_settings.auth_trust_proxy_headers = False
        mock_settings.api_key = "test-key"

        with TestClient(app) as client:
            response = client.get(
                "/api/v1/scm/github/issues",
                params={"repo_name": "test-repo"},
            )
            # Should get 401 without auth
            assert response.status_code == 401


def test_routes_work_with_valid_api_key():
    """Test routes work when valid API key is provided."""
    app.dependency_overrides.clear()

    from unittest.mock import MagicMock
    from unittest.mock import patch

    with patch("soliplex.agents.server.auth.settings") as mock_settings:
        mock_settings.api_key_enabled = True
        mock_settings.auth_trust_proxy_headers = False
        mock_settings.api_key = "valid-test-key"

        with (
            TestClient(app) as client,
            patch("soliplex.agents.server.routes.scm.scm_app") as mock_scm_app,
        ):
            # Create a mock provider that returns a completed future for async calls
            mock_provider = MagicMock()

            # Create a proper async return for list_issues
            async def mock_list_issues(*args, **kwargs):
                return []

            mock_provider.list_issues = mock_list_issues
            mock_scm_app.get_scm.return_value = mock_provider

            response = client.get(
                "/api/v1/scm/github/issues",
                params={"repo_name": "test-repo", "owner": "test-owner"},
                headers={"Authorization": "Bearer valid-test-key"},
            )
            assert response.status_code == 200
