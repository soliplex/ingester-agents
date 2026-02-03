"""Shared pytest fixtures for functional tests.

Environment variables are set here to match pyproject.toml [tool.pytest_env].
These need to be set before importing modules that use pydantic_settings.
"""

import importlib
import os

import pytest

# Set environment variables for functional tests
# These match the values in pyproject.toml [tool.pytest_env]
# os.environ.setdefault("scm_auth_token", "test_scm_auth_token")
os.environ.setdefault("scm_auth_username", "gitea_admin")
os.environ.setdefault("scm_auth_password", "test_password")
os.environ.setdefault("scm_owner", "gitea_admin")
os.environ.setdefault("scm_base_url", "http://localhost:3000/api/v1")
os.environ.setdefault("LOG_LEVEL", "INFO")


@pytest.fixture(scope="session", autouse=True)
def reload_settings():
    """Reload settings module to pick up environment variables.

    pydantic_settings reads env vars at import time. Since modules may be
    imported before this conftest runs, we reload the config module to
    ensure it picks up the correct values.
    """
    import soliplex.agents.config as config_module
    import soliplex.agents.scm.base as base_module

    # Reload the config module to re-read environment variables
    importlib.reload(config_module)

    # Update the settings reference in base module
    base_module.settings = config_module.settings

    return


@pytest.fixture
def mock_response():
    """Create a mock aiohttp response."""
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock

    import aiohttp

    def _mock_response(status: int = 200, json_data: dict | list | None = None, text_data: str | None = None):
        response = AsyncMock(spec=aiohttp.ClientResponse)
        response.status = status

        if json_data is not None:
            response.json = AsyncMock(return_value=json_data)

        if text_data is not None:
            response.text = AsyncMock(return_value=text_data)
            response.read = AsyncMock(return_value=text_data.encode())
        else:
            response.read = AsyncMock(return_value=b"test content")

        response.raise_for_status = MagicMock()

        return response

    return _mock_response


@pytest.fixture
def sample_issue():
    """Sample issue for testing."""
    return {
        "id": 1,
        "number": 1,
        "title": "Test Issue",
        "body": "Test issue body",
        "state": "open",
        "url": "https://api.example.com/repos/owner/repo/issues/1",
        "user": {"login": "testuser"},
        "created_at": "2024-01-01T00:00:00Z",
    }


@pytest.fixture
def sample_file_record():
    """Sample file record for testing."""
    return {
        "name": "test.md",
        "path": "docs/test.md",
        "url": "https://api.example.com/repos/owner/repo/contents/docs/test.md",
        "type": "file",
        "content": "VGVzdCBjb250ZW50",
        "sha": "abc123",
        "last_committer_date": "2024-01-01T00:00:00Z",
        "last_commit_sha": "def456",
    }
