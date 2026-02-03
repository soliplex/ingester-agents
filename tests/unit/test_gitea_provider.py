"""Tests for soliplex.agents.scm.gitea module."""

from unittest.mock import patch

import pytest
from pydantic import SecretStr

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


# Authentication method tests


def test_get_auth_headers_with_token(gitea_provider):
    """Test get_auth_headers returns token auth when token is provided."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = SecretStr("gitea-token-456")
        mock_settings.scm_auth_username = None
        mock_settings.scm_auth_password = None

        headers = gitea_provider.get_auth_headers()

        assert headers == {"Authorization": "token gitea-token-456"}


def test_get_auth_headers_with_basic_auth(gitea_provider):
    """Test get_auth_headers returns basic auth when username and password are provided."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = None
        mock_settings.scm_auth_username = "giteauser"
        mock_settings.scm_auth_password = SecretStr("giteapass")

        headers = gitea_provider.get_auth_headers()

        # base64("giteauser:giteapass") = "Z2l0ZWF1c2VyOmdpdGVhcGFzcw=="
        assert headers == {"Authorization": "Basic Z2l0ZWF1c2VyOmdpdGVhcGFzcw=="}


def test_get_auth_headers_token_priority(gitea_provider):
    """Test get_auth_headers prioritizes token over basic auth when both are provided."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = SecretStr("priority-gitea-token")
        mock_settings.scm_auth_username = "giteauser"
        mock_settings.scm_auth_password = SecretStr("giteapass")

        headers = gitea_provider.get_auth_headers()

        assert headers == {"Authorization": "token priority-gitea-token"}


def test_get_auth_headers_raises_when_no_auth(gitea_provider):
    """Test get_auth_headers raises AuthenticationConfigError when no auth is configured."""
    from soliplex.agents.scm import AuthenticationConfigError

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = None
        mock_settings.scm_auth_username = None
        mock_settings.scm_auth_password = None

        with pytest.raises(AuthenticationConfigError):
            gitea_provider.get_auth_headers()


def test_get_auth_headers_raises_when_only_username(gitea_provider):
    """Test get_auth_headers raises when only username is provided."""
    from soliplex.agents.scm import AuthenticationConfigError

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = None
        mock_settings.scm_auth_username = "giteauser"
        mock_settings.scm_auth_password = None

        with pytest.raises(AuthenticationConfigError):
            gitea_provider.get_auth_headers()


def test_get_auth_headers_raises_when_only_password(gitea_provider):
    """Test get_auth_headers raises when only password is provided."""
    from soliplex.agents.scm import AuthenticationConfigError

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = None
        mock_settings.scm_auth_username = None
        mock_settings.scm_auth_password = "giteapass"

        with pytest.raises(AuthenticationConfigError):
            gitea_provider.get_auth_headers()
