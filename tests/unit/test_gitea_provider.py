"""Tests for soliplex.agents.scm.gitea module."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from soliplex.agents.scm import SCMException
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


def test_init_no_owner():
    """Test provider initialization without owner sets None."""
    provider = GiteaProvider()
    assert provider.owner is None


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


def test_initialization_without_owner():
    """Test Gitea provider has None owner when not provided."""
    provider = GiteaProvider()
    assert provider.owner is None


def test_initialization_with_custom_owner(gitea_provider_with_owner):
    """Test Gitea provider uses custom owner when provided."""
    assert gitea_provider_with_owner.owner == "custom_owner"


def test_build_url(gitea_provider):
    """Test build_url constructs correct Gitea API URL."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_base_url = "https://gitea.example.com/api/v1"
        mock_settings.scm_auth_token = "test-token"
        provider = GiteaProvider(owner="test-owner")
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


# Contents 404: empty repository vs missing branch


@pytest.fixture
def fast_settings():
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_base_url = "https://gitea.example.com/api/v1"
        mock_settings.scm_max_concurrent_requests = 5
        mock_settings.scm_retry_attempts = 1
        mock_settings.scm_retry_backoff_max = 0.1
        mock_settings.extensions = ["md"]
        yield mock_settings


def _session(*responses):
    from tests.unit.conftest import create_async_context_manager

    session = MagicMock()
    session.get = AsyncMock(side_effect=list(responses))
    return create_async_context_manager(session), session


EMPTY_TREE = {"errors": ["object does not exist [id: refs/heads/main, rel_path: ]"]}


@pytest.mark.asyncio
async def test_list_repo_files_existing_empty_branch_returns_empty(gitea_provider, mock_response, fast_settings):
    """'object does not exist' on a branch that exists: an empty repository."""
    ctx, session = _session(mock_response(404, EMPTY_TREE), mock_response(200, {"name": "main"}))
    with patch.object(gitea_provider, "get_session", return_value=ctx):
        assert await gitea_provider.list_repo_files("repo", owner="owner", branch="main") == []
    assert session.get.call_args_list[1][0][0] == "https://gitea.example.com/api/v1/repos/owner/repo/branches/main"


@pytest.mark.asyncio
async def test_list_repo_files_missing_branch_raises(gitea_provider, mock_response, fast_settings):
    """A branch missing from a repository with commits is an error, never an empty listing."""
    ctx, _session_mock = _session(mock_response(404, EMPTY_TREE), mock_response(404, {"message": "not found"}))
    with (
        patch.object(gitea_provider, "get_session", return_value=ctx),
        patch.object(gitea_provider, "get_repo", AsyncMock(return_value={"empty": False})),
    ):
        with pytest.raises(SCMException, match="branch 'dev' not found in owner/repo"):
            await gitea_provider.list_repo_files("repo", owner="owner", branch="dev")


@pytest.mark.asyncio
async def test_list_repo_files_repo_without_commits_returns_empty(gitea_provider, mock_response, fast_settings):
    """A repository with no commits has no branches either; it lists as empty."""
    ctx, _session_mock = _session(mock_response(404, EMPTY_TREE), mock_response(404, {"message": "not found"}))
    with (
        patch.object(gitea_provider, "get_session", return_value=ctx),
        patch.object(gitea_provider, "get_repo", AsyncMock(return_value={"empty": True})),
    ):
        assert await gitea_provider.list_repo_files("repo", owner="owner", branch="main") == []


@pytest.mark.asyncio
async def test_list_repo_files_existing_branch_other_404_raises(gitea_provider, mock_response, fast_settings):
    """A 404 that is not 'object does not exist' is an error even on an existing branch."""
    ctx, _session_mock = _session(mock_response(404, {"errors": ["some other error"]}), mock_response(200, {}))
    with patch.object(gitea_provider, "get_session", return_value=ctx):
        with pytest.raises(SCMException, match="some other error"):
            await gitea_provider.list_repo_files("repo", owner="owner", branch="main")


@pytest.mark.asyncio
async def test_is_empty_branch_non_dict_response(gitea_provider, mock_response, fast_settings):
    session = MagicMock()
    session.get = AsyncMock(return_value=mock_response(200, {}))
    assert await gitea_provider.is_empty_branch(session, "owner", "repo", "main", ["not a dict"]) is False


@pytest.mark.asyncio
async def test_is_empty_branch_repo_record_not_dict_raises(gitea_provider, mock_response, fast_settings):
    session = MagicMock()
    session.get = AsyncMock(return_value=mock_response(404, {}))
    with patch.object(gitea_provider, "get_repo", AsyncMock(return_value=["odd"])):
        with pytest.raises(SCMException, match="not found"):
            await gitea_provider.is_empty_branch(session, "owner", "repo", "main", EMPTY_TREE)


@pytest.mark.asyncio
async def test_list_commits_since_uses_limit(gitea_provider, mock_response, fast_settings):
    ctx, session = _session(mock_response(200, []))
    with patch.object(gitea_provider, "get_session", return_value=ctx):
        await gitea_provider.list_commits_since("repo", "owner", branch="main", limit=100)
    assert "&limit=100" in session.get.call_args[0][0]
