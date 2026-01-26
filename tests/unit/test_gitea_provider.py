"""Tests for soliplex.agents.scm.gitea module."""

from unittest.mock import patch

import pytest

from soliplex.agents.scm.gitea import GiteaProvider


@pytest.fixture
def gitea_provider():
    """Create Gitea provider instance."""
    return GiteaProvider()


@pytest.fixture
def gitea_provider_with_owner():
    """Create Gitea provider instance with custom owner."""
    return GiteaProvider(owner="custom_owner")


# Basic provider methods tests


def test_get_default_owner(gitea_provider):
    """Test get_default_owner returns settings value."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_owner = "gitea-owner"
        assert gitea_provider.get_default_owner() == "gitea-owner"


def test_get_base_url(gitea_provider):
    """Test get_base_url returns Gitea URL from settings."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_base_url = "https://gitea.example.com/api/v1"
        assert gitea_provider.get_base_url() == "https://gitea.example.com/api/v1"


def test_get_auth_token(gitea_provider):
    """Test get_auth_token returns settings value."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = "gitea-token-123"
        assert gitea_provider.get_auth_token() == "gitea-token-123"


def test_get_last_updated(gitea_provider):
    """Test get_last_updated extracts last_committer_date from record."""
    rec = {
        "name": "test.md",
        "last_committer_date": "2024-01-15T12:00:00Z",
        "sha": "abc123",
    }
    assert gitea_provider.get_last_updated(rec) == "2024-01-15T12:00:00Z"


def test_get_last_updated_missing_field(gitea_provider):
    """Test get_last_updated returns None when field is missing."""
    rec = {"name": "test.md", "sha": "abc123"}
    assert gitea_provider.get_last_updated(rec) is None


# Integration tests


def test_initialization_with_default_owner(gitea_provider):
    """Test Gitea provider uses default owner from settings."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_owner = "default-gitea-owner"
        provider = GiteaProvider()
        assert provider.owner == "default-gitea-owner"


def test_initialization_with_custom_owner(gitea_provider_with_owner):
    """Test Gitea provider uses custom owner when provided."""
    assert gitea_provider_with_owner.owner == "custom_owner"


def test_build_url(gitea_provider):
    """Test build_url constructs correct Gitea API URL."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_base_url = "https://gitea.example.com/api/v1"
        mock_settings.scm_owner = "test-owner"
        mock_settings.scm_auth_token = "test-token"
        provider = GiteaProvider()
        url = provider.build_url("/repos/owner/repo")
        assert url == "https://gitea.example.com/api/v1/repos/owner/repo"
