"""Shared pytest fixtures for unit tests."""

import os

# Set environment variables before any imports
os.environ.setdefault("GITEA_URL", "https://gitea.example.com/api/v1")
os.environ.setdefault("GITEA_TOKEN", "test_gitea_token")
os.environ.setdefault("GITEA_OWNER", "test_owner")
os.environ.setdefault("GH_TOKEN", "test_gh_token")
os.environ.setdefault("GH_OWNER", "test_gh_owner")
os.environ.setdefault("ENDPOINT_URL", "http://localhost:8000/api/v1")

import json
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import aiohttp
import pytest


@pytest.fixture
def mock_settings(monkeypatch):
    """Mock settings for testing."""
    mock_settings = MagicMock()
    mock_settings.gitea_url = "https://gitea.example.com/api/v1"
    mock_settings.gitea_token = "test_gitea_token"
    mock_settings.gitea_owner = "test_owner"
    mock_settings.gh_token = "test_gh_token"
    mock_settings.gh_owner = "test_gh_owner"
    mock_settings.extensions = ["md", "pdf"]
    mock_settings.endpoint_url = "http://localhost:8000/api/v1"

    monkeypatch.setattr("soliplex.agents.scm.lib.config.settings", mock_settings)
    monkeypatch.setattr("soliplex.agents.client.settings", mock_settings)

    return mock_settings


def create_async_context_manager(return_value):
    """Create an async context manager that returns the given value."""
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=return_value)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@pytest.fixture
def mock_response():
    """Create a mock aiohttp response."""
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
def mock_session(mock_response):
    """Create a mock aiohttp session."""
    def _mock_session(responses: list[tuple[int, dict | list | None]] | None = None):
        session = AsyncMock(spec=aiohttp.ClientSession)

        if responses:
            # Multiple responses for multiple calls
            response_mocks = [mock_response(status, data) for status, data in responses]
            # Create proper async context managers
            async_contexts = []
            for r in response_mocks:
                ctx = AsyncMock()
                ctx.__aenter__ = AsyncMock(return_value=r)
                ctx.__aexit__ = AsyncMock(return_value=None)
                async_contexts.append(ctx)
            session.get.side_effect = async_contexts
            session.post.side_effect = async_contexts
        else:
            # Single default response
            default_response = mock_response(200, {"result": "success"})
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=default_response)
            ctx.__aexit__ = AsyncMock(return_value=None)
            session.get.return_value = ctx
            session.post.return_value = ctx

        return session

    return _mock_session


@pytest.fixture
def sample_file_record():
    """Sample file record from SCM API."""
    return {
        "name": "test.md",
        "path": "docs/test.md",
        "url": "https://api.example.com/repos/owner/repo/contents/docs/test.md",
        "type": "file",
        "content": "VGVzdCBjb250ZW50",  # "Test content" in base64
        "sha": "abc123",
        "last_committer_date": "2024-01-01T00:00:00Z",
    }


@pytest.fixture
def sample_issue():
    """Sample issue from SCM API."""
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
def sample_comment():
    """Sample comment from SCM API."""
    return {
        "id": 1,
        "body": "Test comment",
        "user": {"login": "testuser"},
        "issue_url": "https://api.example.com/repos/owner/repo/issues/1",
        "created_at": "2024-01-01T00:00:00Z",
    }
