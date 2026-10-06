"""Tests for soliplex.agents.scm.github module."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import aiohttp
import pytest
from pydantic import SecretStr

from soliplex.agents.scm import GitHubAPIError
from soliplex.agents.scm import SCMException
from soliplex.agents.scm.github import GitHubProvider
from tests.unit.conftest import create_async_context_manager


@pytest.fixture
def github_provider():
    """Create GitHub provider instance."""
    return GitHubProvider()


@pytest.fixture
def github_provider_with_owner():
    """Create GitHub provider instance with custom owner."""
    return GitHubProvider(owner="custom_owner")


# Basic provider methods tests


def test_init_no_owner():
    """Test provider initialization without owner sets None."""
    provider = GitHubProvider()
    assert provider.owner is None


def test_get_base_url(github_provider):
    """Test get_base_url returns GitHub API URL."""
    assert github_provider.get_base_url() == "https://api.github.com"


def test_get_auth_token(github_provider):
    """Test get_auth_token returns settings value."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = "test-token"
        assert github_provider.get_auth_token() == "test-token"


def test_get_last_updated(github_provider):
    """Test get_last_updated returns None."""
    rec = {"name": "test.md", "sha": "abc123"}
    assert github_provider.get_last_updated(rec) is None


# Validate response tests


@pytest.mark.asyncio
async def test_validate_response_success(github_provider):
    """Test validate_response with successful 200 response."""
    response = AsyncMock()
    response.status = 200
    resp = {"data": "success"}

    # Should not raise
    await github_provider.validate_response(response, resp)


@pytest.mark.asyncio
async def test_validate_response_success_list(github_provider):
    """Test validate_response with successful 200 response as list."""
    response = AsyncMock()
    response.status = 200
    resp = [{"id": 1}, {"id": 2}]

    # Should not raise
    await github_provider.validate_response(response, resp)


@pytest.mark.asyncio
async def test_validate_response_non_200_with_message(github_provider):
    """Test validate_response raises SCMException on non-200 with message."""
    response = AsyncMock()
    response.status = 404
    resp = {"message": "Not found"}

    with pytest.raises(SCMException, match="Not found"):
        await github_provider.validate_response(response, resp)


@pytest.mark.asyncio
async def test_validate_response_non_200_without_message(github_provider):
    """Test validate_response raises GitHubAPIError on non-200 without message."""
    response = AsyncMock()
    response.status = 500
    resp = {"error": "Internal error"}

    with pytest.raises(GitHubAPIError):
        await github_provider.validate_response(response, resp)


@pytest.mark.asyncio
async def test_validate_response_non_200_list(github_provider):
    """Test validate_response raises GitHubAPIError on non-200 with list response."""
    response = AsyncMock()
    response.status = 500
    resp = [{"error": "Error 1"}]

    with pytest.raises(GitHubAPIError):
        await github_provider.validate_response(response, resp)


@pytest.mark.asyncio
async def test_validate_response_with_errors_field(github_provider):
    """Test validate_response raises SCMException when response has errors field."""
    response = AsyncMock()
    response.status = 200
    resp = {"errors": ["Error 1", "Error 2"]}

    with pytest.raises(SCMException):
        await github_provider.validate_response(response, resp)


# Get file content tests


@pytest.mark.asyncio
async def test_get_file_content_with_existing_content(github_provider):
    """Test get_file_content when content already exists."""
    rec = {"name": "test.md", "content": "VGVzdA==", "sha": "abc123"}
    session = AsyncMock()

    result = await github_provider.get_file_content(rec, session, "owner", "repo")

    assert result["content"] == "VGVzdA=="
    # Should not call get_blob


@pytest.mark.asyncio
async def test_get_file_content_without_content(github_provider, mock_response):
    """Test get_file_content when content is missing."""
    rec = {"name": "test.md", "sha": "abc123"}
    session = MagicMock()

    # Mock the blob response
    blob_response = mock_response(200, text_data="blob content")
    session.get.return_value = create_async_context_manager(blob_response)

    with patch.object(github_provider, "get_blob", return_value=b"blob content"):
        result = await github_provider.get_file_content(rec, session, "owner", "repo")

        assert result["content"] == b"blob content"


@pytest.mark.asyncio
async def test_get_file_content_with_none_content(github_provider):
    """Test get_file_content when content is None."""
    rec = {"name": "test.md", "content": None, "sha": "abc123"}
    session = AsyncMock()

    with patch.object(github_provider, "get_blob", return_value=b"blob content"):
        result = await github_provider.get_file_content(rec, session, "owner", "repo")

        assert result["content"] == b"blob content"


@pytest.mark.asyncio
async def test_get_file_content_with_empty_content(github_provider):
    """Test get_file_content when content is empty string."""
    rec = {"name": "test.md", "content": "", "sha": "abc123"}
    session = AsyncMock()

    with patch.object(github_provider, "get_blob", return_value=b"blob content"):
        result = await github_provider.get_file_content(rec, session, "owner", "repo")

        assert result["content"] == b"blob content"


# Get blob tests


@pytest.mark.asyncio
async def test_get_blob_success(github_provider, mock_response):
    """Test get_blob successfully fetches blob content."""
    rec = {"sha": "abc123"}
    session = MagicMock()

    # Mock the blob API response
    blob_content = b"This is the blob content"
    blob_response = mock_response(200)
    blob_response.read = AsyncMock(return_value=blob_content)
    session.get = AsyncMock(return_value=blob_response)

    result = await github_provider.get_blob("test-repo", "test-owner", rec, session)

    assert result == blob_content
    session.get.assert_called_once()


@pytest.mark.asyncio
async def test_get_blob_raises_on_error(github_provider, mock_response):
    """Test get_blob raises exception on HTTP error."""
    rec = {"sha": "abc123"}
    session = MagicMock()

    # Mock error response - 404 is not retryable, so _request_with_retry
    # returns the response; raise_for_status then raises.
    blob_response = mock_response(404)
    blob_response.raise_for_status = MagicMock(
        side_effect=aiohttp.ClientResponseError(request_info=MagicMock(), history=(), status=404, message="Not found")
    )
    session.get = AsyncMock(return_value=blob_response)

    with pytest.raises(aiohttp.ClientResponseError):
        await github_provider.get_blob("test-repo", "test-owner", rec, session)


@pytest.mark.asyncio
async def test_get_blob_retries_on_429(github_provider, mock_response):
    """Test get_blob retries on 429 rate limit then succeeds."""
    rec = {"sha": "abc123"}
    session = MagicMock()

    # First call returns 429, second returns success
    resp_429 = mock_response(429)
    resp_429.headers = {"Retry-After": "0"}
    resp_429.text = AsyncMock(return_value="rate limited")

    blob_content = b"blob data"
    resp_200 = mock_response(200)
    resp_200.read = AsyncMock(return_value=blob_content)

    session.get = AsyncMock(side_effect=[resp_429, resp_200])

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_retry_attempts = 3
        mock_settings.scm_retry_backoff_max = 0.02

        result = await github_provider.get_blob("test-repo", "test-owner", rec, session)

        assert result == blob_content


@pytest.mark.asyncio
async def test_get_blob_retries_on_network_error(github_provider, mock_response):
    """Test get_blob retries on network error and raises after max attempts."""
    rec = {"sha": "abc123"}
    session = MagicMock()

    session.get = AsyncMock(
        side_effect=aiohttp.ClientConnectorError(connection_key=MagicMock(), os_error=OSError("Connection refused"))
    )

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_retry_attempts = 2
        mock_settings.scm_retry_backoff_max = 0.02

        with pytest.raises(aiohttp.ClientConnectorError):
            await github_provider.get_blob("test-repo", "test-owner", rec, session)


# Integration tests


def test_initialization_without_owner():
    """Test GitHub provider has None owner when not provided."""
    provider = GitHubProvider()
    assert provider.owner is None


def test_initialization_with_custom_owner(github_provider_with_owner):
    """Test GitHub provider uses custom owner when provided."""
    assert github_provider_with_owner.owner == "custom_owner"


def test_build_url(github_provider):
    """Test build_url constructs correct GitHub API URL."""
    url = github_provider.build_url("/repos/owner/repo")
    assert url == "https://api.github.com/repos/owner/repo"


# Authentication method tests


def test_get_auth_headers_with_token(github_provider):
    """Test get_auth_headers returns token auth when token is provided."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = SecretStr("test-token-123")
        mock_settings.scm_auth_username = None
        mock_settings.scm_auth_password = None

        headers = github_provider.get_auth_headers()

        assert headers == {"Authorization": "token test-token-123"}


def test_get_auth_headers_with_basic_auth(github_provider):
    """Test get_auth_headers returns basic auth when username and password are provided."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = None
        mock_settings.scm_auth_username = "testuser"
        mock_settings.scm_auth_password = SecretStr("testpass")

        headers = github_provider.get_auth_headers()

        # base64("testuser:testpass") = "dGVzdHVzZXI6dGVzdHBhc3M="
        assert headers == {"Authorization": "Basic dGVzdHVzZXI6dGVzdHBhc3M="}


def test_get_auth_headers_token_priority(github_provider):
    """Test get_auth_headers prioritizes token over basic auth when both are provided."""
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = SecretStr("priority-token")
        mock_settings.scm_auth_username = "testuser"
        mock_settings.scm_auth_password = SecretStr("testpass")

        headers = github_provider.get_auth_headers()

        assert headers == {"Authorization": "token priority-token"}


def test_get_auth_headers_raises_when_no_auth(github_provider):
    """Test get_auth_headers raises AuthenticationConfigError when no auth is configured."""
    from soliplex.agents.scm import AuthenticationConfigError

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = None
        mock_settings.scm_auth_username = None
        mock_settings.scm_auth_password = None

        with pytest.raises(AuthenticationConfigError):
            github_provider.get_auth_headers()


def test_get_auth_headers_raises_when_only_username(github_provider):
    """Test get_auth_headers raises when only username is provided."""
    from soliplex.agents.scm import AuthenticationConfigError

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = None
        mock_settings.scm_auth_username = "testuser"
        mock_settings.scm_auth_password = None

        with pytest.raises(AuthenticationConfigError):
            github_provider.get_auth_headers()


def test_get_auth_headers_raises_when_only_password(github_provider):
    """Test get_auth_headers raises when only password is provided."""
    from soliplex.agents.scm import AuthenticationConfigError

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = None
        mock_settings.scm_auth_username = None
        mock_settings.scm_auth_password = "testpass"

        with pytest.raises(AuthenticationConfigError):
            github_provider.get_auth_headers()


# Contents 404, commit paging, issue lookups


@pytest.fixture
def fast_settings():
    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_max_concurrent_requests = 5
        mock_settings.scm_retry_attempts = 1
        mock_settings.scm_retry_backoff_max = 0.1
        mock_settings.extensions = ["md"]
        yield mock_settings


def _session(*responses):
    session = MagicMock()
    session.get = AsyncMock(side_effect=list(responses))
    return create_async_context_manager(session), session


@pytest.mark.asyncio
async def test_list_repo_files_missing_branch_raises(github_provider, mock_response, fast_settings):
    """A missing branch says so, rather than GitHub's 'No commit found for the ref'."""
    ctx, session = _session(
        mock_response(404, {"message": "No commit found for the ref dev"}),
        mock_response(404, {"message": "Branch not found"}),
    )
    with patch.object(github_provider, "get_session", return_value=ctx):
        with pytest.raises(SCMException, match="branch 'dev' not found in owner/repo"):
            await github_provider.list_repo_files("repo", owner="owner", branch="dev")
    assert session.get.call_args_list[1][0][0] == "https://api.github.com/repos/owner/repo/branches/dev"


@pytest.mark.asyncio
async def test_list_repo_files_404_on_existing_branch_raises(github_provider, mock_response, fast_settings):
    """GitHub never lists a 404 as empty (an existing empty branch is a 200 with [])."""
    ctx, _session_mock = _session(
        mock_response(404, {"message": "This repository is empty."}),
        mock_response(200, {"name": "main"}),
    )
    with patch.object(github_provider, "get_session", return_value=ctx):
        with pytest.raises(SCMException, match="This repository is empty"):
            await github_provider.list_repo_files("repo", owner="owner", branch="main")


@pytest.mark.asyncio
async def test_list_repo_files_empty_branch_returns_empty(github_provider, mock_response, fast_settings):
    ctx, _session_mock = _session(mock_response(200, []))
    with patch.object(github_provider, "get_session", return_value=ctx):
        assert await github_provider.list_repo_files("repo", owner="owner", branch="main") == []


@pytest.mark.asyncio
async def test_list_repo_files_5xx_root_raises(github_provider, mock_response, fast_settings):
    from soliplex.agents.retry import RetryableHTTPError

    ctx, _session_mock = _session(mock_response(503, {}, text_data="unavailable"))
    with patch.object(github_provider, "get_session", return_value=ctx):
        with pytest.raises(RetryableHTTPError):
            await github_provider.list_repo_files("repo", owner="owner", branch="main")


@pytest.mark.asyncio
async def test_list_commits_since_uses_per_page(github_provider, mock_response, fast_settings):
    """GitHub ignores 'limit' (and pages by 30), so it is asked for per_page."""
    ctx, session = _session(mock_response(200, []))
    with patch.object(github_provider, "get_session", return_value=ctx):
        await github_provider.list_commits_since("repo", "owner", branch="main", limit=100)
    url = session.get.call_args[0][0]
    assert "&per_page=100" in url
    assert "limit=" not in url


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_get_issue_state_deleted(github_provider, mock_response, fast_settings, status):
    resp = mock_response(status, {"message": "gone"})
    resp.history = ()
    ctx, _session_mock = _session(resp)
    with patch.object(github_provider, "get_session", return_value=ctx):
        assert await github_provider.get_issue_state("repo", "owner", 3) == "gone"


@pytest.mark.asyncio
async def test_get_issue_state_transferred(github_provider, mock_response, fast_settings):
    """A transferred issue redirects to its new repository: gone from this one."""
    resp = mock_response(200, {"number": 1, "state": "open"})
    resp.history = (MagicMock(status=301),)
    ctx, _session_mock = _session(resp)
    with patch.object(github_provider, "get_session", return_value=ctx):
        assert await github_provider.get_issue_state("repo", "owner", 3) == "gone"


@pytest.mark.asyncio
async def test_get_issue_state_open(github_provider, mock_response, fast_settings):
    resp = mock_response(200, {"number": 3, "state": "open"})
    resp.history = ()
    ctx, _session_mock = _session(resp)
    with patch.object(github_provider, "get_session", return_value=ctx):
        assert await github_provider.get_issue_state("repo", "owner", 3) == "open"
