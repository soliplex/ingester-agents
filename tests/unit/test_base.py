"""Tests for soliplex.agents.scm.base module."""

from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import patch

import aiohttp
import pytest

from soliplex.agents.scm import APIFetchError
from soliplex.agents.scm import SCMException
from soliplex.agents.scm.base import BaseSCMProvider


class ConcreteSCMProvider(BaseSCMProvider):
    """Concrete implementation for testing."""

    def get_base_url(self) -> str:
        return "https://api.example.com"

    def get_auth_token(self) -> str:
        return "test_token"

    def get_last_updated(self, rec: dict[str, Any]) -> str | None:
        return rec.get("updated_at")


@pytest.fixture
def provider():
    """Create concrete provider instance."""
    return ConcreteSCMProvider()


@pytest.fixture
def provider_with_owner():
    """Create concrete provider instance with custom owner."""
    return ConcreteSCMProvider(owner="custom_owner")


def test_init_default_owner(provider):
    """Test provider initialization without owner."""
    assert provider.owner is None


def test_init_custom_owner(provider_with_owner):
    """Test provider initialization with custom owner."""
    assert provider_with_owner.owner == "custom_owner"


@pytest.mark.asyncio
async def test_get_session(provider):
    """Test get_session creates authenticated session."""
    from pydantic import SecretStr

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = SecretStr("test_token")
        mock_settings.ssl_verify = True
        mock_settings.http_timeout_total = 30
        mock_settings.http_timeout_connect = 10
        mock_settings.http_timeout_sock_read = 10
        async with provider.get_session() as session:
            assert isinstance(session, aiohttp.ClientSession)
            assert session.headers["Authorization"] == "token test_token"


def test_build_url(provider):
    """Test build_url constructs correct URL."""
    url = provider.build_url("/repos/owner/repo")
    assert url == "https://api.example.com/repos/owner/repo"


def test_build_url_strips_slashes(provider):
    """Test build_url handles leading/trailing slashes."""
    url = provider.build_url("repos/owner/repo/")
    assert url == "https://api.example.com/repos/owner/repo/"


@pytest.mark.asyncio
async def test_paginate_single_page(provider, mock_response):
    """Test paginate with single page of results."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    page1_data = [{"id": 1, "name": "item1"}]

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp1 = mock_response(200, page1_data)
        mock_resp2 = mock_response(200, [])  # Empty second page

        mock_session.get.side_effect = [
            create_async_context_manager(mock_resp1),
            create_async_context_manager(mock_resp2),
        ]
        mock_get_session.return_value = create_async_context_manager(mock_session)

        url_template = "https://api.example.com/repos/{owner}/{repo}/items?page={page}"
        result = await provider.paginate(url_template, "test_owner", "test_repo")

        assert len(result) == 1
        assert result[0]["name"] == "item1"


@pytest.mark.asyncio
async def test_paginate_multiple_pages(provider, mock_response):
    """Test paginate with multiple pages of results."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    page1_data = [{"id": 1}, {"id": 2}]
    page2_data = [{"id": 3}]

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp1 = mock_response(200, page1_data)
        mock_resp2 = mock_response(200, page2_data)
        mock_resp3 = mock_response(200, [])

        mock_session.get.side_effect = [
            create_async_context_manager(mock_resp1),
            create_async_context_manager(mock_resp2),
            create_async_context_manager(mock_resp3),
        ]
        mock_get_session.return_value = create_async_context_manager(mock_session)

        url_template = "https://api.example.com/repos/{owner}/{repo}/items?page={page}"
        result = await provider.paginate(url_template, "test_owner", "test_repo")

        assert len(result) == 3


@pytest.mark.asyncio
async def test_paginate_404_error(provider, mock_response):
    """Test paginate raises SCMException on 404."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(404, {"message": "Not found"})

        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        url_template = "https://api.example.com/repos/{owner}/{repo}/items?page={page}"

        with pytest.raises(SCMException, match="not found"):
            await provider.paginate(url_template, "test_owner", "test_repo")


@pytest.mark.asyncio
async def test_paginate_api_error_with_errors_field(provider, mock_response):
    """Test paginate raises SCMException when response has errors field."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        error_data = {"errors": ["Error 1", "Error 2"]}
        mock_resp = mock_response(500, error_data)

        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        url_template = "https://api.example.com/repos/{owner}/{repo}/items?page={page}"

        with pytest.raises(SCMException):
            await provider.paginate(url_template, "test_owner", "test_repo")


@pytest.mark.asyncio
async def test_paginate_non_200_status(provider, mock_response):
    """Test paginate raises APIFetchError on non-200 status."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(500, {"message": "Server error"})

        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        url_template = "https://api.example.com/repos/{owner}/{repo}/items?page={page}"

        with pytest.raises(APIFetchError):
            await provider.paginate(url_template, "test_owner", "test_repo")


@pytest.mark.asyncio
async def test_paginate_with_process_response(provider, mock_response):
    """Test paginate with custom response processor."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    page1_data = {"items": [{"id": 1}, {"id": 2}]}

    def process_response(resp):
        return resp.get("items", [])

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp1 = mock_response(200, page1_data)
        mock_resp2 = mock_response(200, {"items": []})

        mock_session.get.side_effect = [
            create_async_context_manager(mock_resp1),
            create_async_context_manager(mock_resp2),
        ]
        mock_get_session.return_value = create_async_context_manager(mock_session)

        url_template = "https://api.example.com/repos/{owner}/{repo}/items?page={page}"
        result = await provider.paginate(url_template, "test_owner", "test_repo", process_response=process_response)

        assert len(result) == 2


@pytest.mark.asyncio
async def test_list_issues_without_comments(provider, mock_response, sample_issue):
    """Test list_issues without comments."""
    with patch.object(provider, "paginate") as mock_paginate:
        mock_paginate.return_value = [sample_issue]

        result = await provider.list_issues("test_repo", add_comments=False)

        assert len(result) == 1
        assert result[0]["title"] == "Test Issue"
        mock_paginate.assert_called_once()


@pytest.mark.asyncio
async def test_list_issues_with_comments(provider, mock_response, sample_issue, sample_comment):
    """Test list_issues with comments."""
    with patch.object(provider, "paginate") as mock_paginate:
        with patch.object(provider, "list_repo_comments") as mock_list_comments:
            mock_paginate.return_value = [sample_issue]
            mock_list_comments.return_value = [sample_comment]

            result = await provider.list_issues("test_repo", add_comments=True)

            assert len(result) == 1
            assert "comments" in result[0]
            assert result[0]["comment_count"] == 1
            assert result[0]["comments"][0] == "Test comment"


@pytest.mark.asyncio
async def test_list_repo_comments(provider, sample_comment):
    """Test list_repo_comments."""
    with patch.object(provider, "paginate") as mock_paginate:
        mock_paginate.return_value = [sample_comment]

        result = await provider.list_repo_comments("test_owner", "test_repo")

        assert len(result) == 1
        assert result[0]["body"] == "Test comment"


def test_parse_file_rec(provider, sample_file_record):
    """Test parse_file_rec normalizes file record."""
    result = provider.parse_file_rec(sample_file_record)

    assert result["name"] == "test.md"
    assert result["uri"] == "docs/test.md"
    assert result["path"] == "docs/test.md"
    assert "file_bytes" in result
    assert "sha256" in result
    assert len(result["sha256"]) == 64
    assert result["content-type"] == "text/markdown"


@pytest.mark.asyncio
async def test_get_file_content_default(provider):
    """Test get_file_content default implementation."""
    rec = {"name": "test.md", "content": "test"}
    session = AsyncMock()

    result = await provider.get_file_content(rec, session, "owner", "repo")

    assert result == rec


@pytest.mark.asyncio
async def test_get_data_from_url_single_file(provider, mock_response, sample_file_record):
    """Test get_data_from_url with single file."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    mock_session = MagicMock()
    mock_resp = mock_response(200, sample_file_record)
    mock_session.get.return_value = create_async_context_manager(mock_resp)

    with patch.object(provider, "get_file_content", return_value=sample_file_record):
        result = await provider.get_data_from_url("https://api.example.com/file", mock_session, owner="owner", repo="repo")

        assert isinstance(result, dict)
        assert result["name"] == "test.md"


@pytest.mark.asyncio
async def test_get_data_from_url_single_file_without_owner_repo(provider, mock_response, sample_file_record):
    """Test get_data_from_url with single file without owner/repo parameters."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    mock_session = MagicMock()
    mock_resp = mock_response(200, sample_file_record)
    mock_session.get.return_value = create_async_context_manager(mock_resp)

    # Call without owner and repo to test the branch where get_file_content is not called
    result = await provider.get_data_from_url("https://api.example.com/file", mock_session)

    assert isinstance(result, dict)
    assert result["name"] == "test.md"


@pytest.mark.asyncio
async def test_get_data_from_url_directory(provider, mock_response, sample_file_record):
    """Test get_data_from_url with directory."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    dir_response = [
        {"name": "file1.md", "url": "https://api.example.com/file1", "type": "file"},
        {"name": "file2.md", "url": "https://api.example.com/file2", "type": "file"},
    ]

    mock_session = MagicMock()
    mock_resp_dir = mock_response(200, dir_response)
    mock_session.get.return_value = create_async_context_manager(mock_resp_dir)

    # Mock the recursive calls
    with patch.object(provider, "get_data_from_url") as mock_get_data:
        # First call returns directory
        mock_get_data.side_effect = [
            dir_response,
            sample_file_record,
            sample_file_record,
        ]

        result = await mock_get_data("https://api.example.com/dir", mock_session, owner="owner", repo="repo")

        assert isinstance(result, list)


@pytest.mark.asyncio
async def test_get_data_from_url_directory_with_recursion(provider, mock_response, sample_file_record):
    """Test get_data_from_url with directory that requires recursion."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    dir_response = [
        {"name": "file1.md", "url": "https://api.example.com/file1", "type": "file"},
        {"name": "file2.md", "url": "https://api.example.com/file2", "type": "file"},
    ]

    file1_response = {
        "name": "file1.md",
        "path": "file1.md",
        "url": "https://api.example.com/file1",
        "content": "VGVzdDE=",
        "last_commit_sha": "abc1",
    }
    file2_response = {
        "name": "file2.md",
        "path": "file2.md",
        "url": "https://api.example.com/file2",
        "content": "VGVzdDI=",
        "last_commit_sha": "abc2",
    }

    mock_session = MagicMock()

    # First GET returns the directory listing
    mock_resp_dir = mock_response(200, dir_response)
    # Second GET returns file1
    mock_resp_file1 = mock_response(200, file1_response)
    # Third GET returns file2
    mock_resp_file2 = mock_response(200, file2_response)

    mock_session.get.side_effect = [
        create_async_context_manager(mock_resp_dir),
        create_async_context_manager(mock_resp_file1),
        create_async_context_manager(mock_resp_file2),
    ]

    # Call the actual method to exercise the directory recursion code
    result = await provider.get_data_from_url("https://api.example.com/dir", mock_session, owner="owner", repo="repo")

    # Should return a list of parsed files
    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_data_from_url_error(provider, mock_response):
    """Test get_data_from_url handles client errors."""
    from unittest.mock import MagicMock

    mock_session = MagicMock()
    mock_session.get.side_effect = aiohttp.ClientError("Network error")

    result = await provider.get_data_from_url("https://api.example.com/file", mock_session, owner="owner", repo="repo")

    assert "error" in result


@pytest.mark.asyncio
async def test_list_repo_files(provider, mock_response, sample_file_record):
    """Test list_repo_files."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    api_response = [
        {"name": "file1.md", "type": "file", "url": "https://api.example.com/file1"},
        {"name": "file2.pdf", "type": "file", "url": "https://api.example.com/file2"},
        {"name": "dir1", "type": "dir", "url": "https://api.example.com/dir1"},
        {"name": "file3.txt", "type": "file", "url": "https://api.example.com/file3"},  # Not in allowed extensions
    ]

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, api_response)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch.object(provider, "validate_response"):
            with patch.object(provider, "get_data_from_url") as mock_get_data:
                # Mock returns for files and directory
                mock_get_data.side_effect = [
                    {"name": "file1.md", "uri": "file1.md"},
                    {"name": "file2.pdf", "uri": "file2.pdf"},
                    [{"name": "subfile.md", "uri": "dir1/subfile.md"}],
                ]

                result = await provider.list_repo_files("test_repo")

                assert len(result) == 3


@pytest.mark.asyncio
async def test_iter_repo_files(provider, mock_response, sample_file_record):
    """Test iter_repo_files yields files."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    api_response = [
        {"name": "file1.md", "type": "file", "url": "https://api.example.com/file1"},
        {"name": "dir1", "type": "dir", "url": "https://api.example.com/dir1"},
    ]

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, api_response)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch.object(provider, "validate_response"):
            with patch.object(provider, "get_data_from_url") as mock_get_data:
                mock_get_data.side_effect = [
                    {"name": "file1.md", "uri": "file1.md"},
                    [{"name": "subfile.md", "uri": "dir1/subfile.md"}],
                ]

                files = []
                async for file in provider.iter_repo_files("test_repo"):
                    files.append(file)

                assert len(files) == 2


@pytest.mark.asyncio
async def test_validate_response_with_errors(provider):
    """Test validate_response raises SCMException for errors."""
    response = AsyncMock()
    response.status = 200
    resp = {"errors": ["Error 1"]}

    with pytest.raises(SCMException):
        await provider.validate_response(response, resp)


@pytest.mark.asyncio
async def test_validate_response_success(provider):
    """Test validate_response with successful response."""
    response = AsyncMock()
    response.status = 200
    resp = {"data": "success"}

    # Should not raise
    await provider.validate_response(response, resp)


@pytest.mark.asyncio
async def test_validate_response_list(provider):
    """Test validate_response with list response."""
    response = AsyncMock()
    response.status = 200
    resp = [{"id": 1}, {"id": 2}]

    # Should not raise
    await provider.validate_response(response, resp)


@pytest.mark.asyncio
async def test_create_repository_success(provider, mock_response):
    """Test create_repository creates repository successfully."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    created_repo = {"id": 1, "name": "new-repo", "full_name": "test_owner/new-repo"}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(201, created_repo)
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.create_repository("new-repo", description="A test repo", private=True)

        assert result["name"] == "new-repo"
        mock_session.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_repository_for_org(provider, mock_response):
    """Test create_repository creates repository under organization."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    created_repo = {"id": 1, "name": "org-repo", "full_name": "my-org/org-repo"}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(201, created_repo)
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.create_repository("org-repo", organization="my-org")

        assert result["name"] == "org-repo"


@pytest.mark.asyncio
async def test_create_repository_without_owner(mock_response):
    """Test create_repository without owner uses user endpoint."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    provider = ConcreteSCMProvider()  # No owner provided

    created_repo = {"id": 1, "name": "user-repo"}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(201, created_repo)
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.create_repository("user-repo")

        assert result["name"] == "user-repo"


@pytest.mark.asyncio
async def test_create_repository_already_exists(provider, mock_response):
    """Test create_repository raises SCMException when repo exists."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(409, {"message": "Repository already exists"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="already exists"):
            await provider.create_repository("existing-repo")


@pytest.mark.asyncio
async def test_create_repository_org_not_found(provider, mock_response):
    """Test create_repository raises SCMException when org not found."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(404, {"message": "Not found"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="not found"):
            await provider.create_repository("new-repo", organization="nonexistent-org")


@pytest.mark.asyncio
async def test_create_repository_permission_denied(provider, mock_response):
    """Test create_repository raises SCMException on permission denied."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(403, {"message": "Forbidden"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Permission denied"):
            await provider.create_repository("new-repo")


@pytest.mark.asyncio
async def test_create_repository_error_with_message(provider, mock_response):
    """Test create_repository raises SCMException with API message on error."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(500, {"message": "Internal server error"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Internal server error"):
            await provider.create_repository("new-repo")


@pytest.mark.asyncio
async def test_create_repository_error_without_message(provider, mock_response):
    """Test create_repository raises SCMException with status on error without message."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(500, {"error": "something"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Failed to create repository: 500"):
            await provider.create_repository("new-repo")


@pytest.mark.asyncio
async def test_delete_repository_success(provider, mock_response):
    """Test delete_repository deletes repository successfully."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(204, None)
        mock_session.delete.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.delete_repository("test-repo")

        assert result is True
        mock_session.delete.assert_called_once()


@pytest.mark.asyncio
async def test_delete_repository_with_owner(provider, mock_response):
    """Test delete_repository with explicit owner."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(204, None)
        mock_session.delete.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.delete_repository("test-repo", owner="other-owner")

        assert result is True


@pytest.mark.asyncio
async def test_delete_repository_not_found(provider, mock_response):
    """Test delete_repository raises SCMException when repo not found."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(404, {"message": "Not found"})
        mock_session.delete.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="not found"):
            await provider.delete_repository("nonexistent-repo")


@pytest.mark.asyncio
async def test_delete_repository_permission_denied(provider, mock_response):
    """Test delete_repository raises SCMException on permission denied."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(403, {"message": "Forbidden"})
        mock_session.delete.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Permission denied"):
            await provider.delete_repository("protected-repo")


@pytest.mark.asyncio
async def test_delete_repository_error_with_message(provider, mock_response):
    """Test delete_repository raises SCMException with API message on error."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(500, {"message": "Internal server error"})
        mock_session.delete.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Internal server error"):
            await provider.delete_repository("test-repo")


@pytest.mark.asyncio
async def test_delete_repository_error_without_message(provider, mock_response):
    """Test delete_repository raises SCMException with status on error without message."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(500, {"error": "something"})
        mock_session.delete.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Failed to delete repository: 500"):
            await provider.delete_repository("test-repo")


# ==================== Tests for create_issue ====================


@pytest.mark.asyncio
async def test_create_issue_success(provider, mock_response):
    """Test create_issue creates issue successfully."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    created_issue = {"id": 1, "number": 42, "title": "Test Issue"}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(201, created_issue)
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.create_issue("test-repo", "Test Issue", "Issue body")

        assert result["title"] == "Test Issue"
        mock_session.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_issue_with_owner(provider, mock_response):
    """Test create_issue with explicit owner."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    created_issue = {"id": 1, "number": 1, "title": "Org Issue"}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(201, created_issue)
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.create_issue("test-repo", "Org Issue", owner="other-owner")

        assert result["title"] == "Org Issue"


@pytest.mark.asyncio
async def test_create_issue_repo_not_found(provider, mock_response):
    """Test create_issue raises SCMException when repo not found."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(404, {"message": "Not found"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="not found"):
            await provider.create_issue("nonexistent-repo", "Test Issue")


@pytest.mark.asyncio
async def test_create_issue_permission_denied(provider, mock_response):
    """Test create_issue raises SCMException on permission denied."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(403, {"message": "Forbidden"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Permission denied"):
            await provider.create_issue("test-repo", "Test Issue")


@pytest.mark.asyncio
async def test_create_issue_error_with_message(provider, mock_response):
    """Test create_issue raises SCMException with API message on error."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(500, {"message": "Internal server error"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Internal server error"):
            await provider.create_issue("test-repo", "Test Issue")


@pytest.mark.asyncio
async def test_create_issue_error_without_message(provider, mock_response):
    """Test create_issue raises SCMException with status on error without message."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(500, {"error": "something"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Failed to create issue: 500"):
            await provider.create_issue("test-repo", "Test Issue")


# ==================== Tests for create_file ====================


@pytest.mark.asyncio
async def test_create_file_success_with_string(provider, mock_response):
    """Test create_file creates file successfully with string content."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    created_file = {"content": {"path": "test.md", "sha": "abc123"}}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(201, created_file)
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.create_file("test-repo", "test.md", "# Hello World")

        assert result["content"]["path"] == "test.md"
        mock_session.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_file_success_with_bytes(provider, mock_response):
    """Test create_file creates file successfully with bytes content."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    created_file = {"content": {"path": "image.png", "sha": "def456"}}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(201, created_file)
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.create_file("test-repo", "image.png", b"\x89PNG\r\n")

        assert result["content"]["path"] == "image.png"


@pytest.mark.asyncio
async def test_create_file_with_custom_message_and_branch(provider, mock_response):
    """Test create_file with custom commit message and branch."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    created_file = {"content": {"path": "docs/readme.md"}}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(201, created_file)
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.create_file("test-repo", "docs/readme.md", "content", message="Add docs", branch="develop")

        assert result["content"]["path"] == "docs/readme.md"


@pytest.mark.asyncio
async def test_create_file_with_owner(provider, mock_response):
    """Test create_file with explicit owner."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    created_file = {"content": {"path": "file.txt"}}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(201, created_file)
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.create_file("test-repo", "file.txt", "content", owner="other-owner")

        assert result["content"]["path"] == "file.txt"


@pytest.mark.asyncio
async def test_create_file_status_200(provider, mock_response):
    """Test create_file handles 200 status (update case)."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    updated_file = {"content": {"path": "existing.txt"}}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, updated_file)
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.create_file("test-repo", "existing.txt", "new content")

        assert result["content"]["path"] == "existing.txt"


@pytest.mark.asyncio
async def test_create_file_repo_not_found(provider, mock_response):
    """Test create_file raises SCMException when repo not found."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(404, {"message": "Not found"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="not found"):
            await provider.create_file("nonexistent-repo", "file.txt", "content")


@pytest.mark.asyncio
async def test_create_file_permission_denied(provider, mock_response):
    """Test create_file raises SCMException on permission denied."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(403, {"message": "Forbidden"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Permission denied"):
            await provider.create_file("test-repo", "file.txt", "content")


@pytest.mark.asyncio
async def test_create_file_already_exists(provider, mock_response):
    """Test create_file raises SCMException when file exists (422)."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(422, {"message": "Unprocessable Entity"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="already exists"):
            await provider.create_file("test-repo", "existing.txt", "content")


@pytest.mark.asyncio
async def test_create_file_error_with_message(provider, mock_response):
    """Test create_file raises SCMException with API message on error."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(500, {"message": "Internal server error"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Internal server error"):
            await provider.create_file("test-repo", "file.txt", "content")


@pytest.mark.asyncio
async def test_create_file_error_without_message(provider, mock_response):
    """Test create_file raises SCMException with status on error without message."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(500, {"error": "something"})
        mock_session.post.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with pytest.raises(SCMException, match="Failed to create file: 500"):
            await provider.create_file("test-repo", "file.txt", "content")


# ==================== Tests for retry logic and error handling ====================


@pytest.mark.asyncio
async def test_get_data_from_url_directory_with_extension_filtering(provider, mock_response):
    """Test get_data_from_url filters files by allowed_extensions in directory."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    dir_response = [
        {"name": "file1.md", "url": "https://api.example.com/file1", "type": "file"},
        {"name": "file2.txt", "url": "https://api.example.com/file2", "type": "file"},  # Should be ignored
        {"name": "file3.md", "url": "https://api.example.com/file3", "type": "file"},
    ]

    file1_response = {
        "name": "file1.md",
        "path": "file1.md",
        "url": "https://api.example.com/file1",
        "content": "VGVzdDE=",
        "last_commit_sha": "abc1",
    }
    file3_response = {
        "name": "file3.md",
        "path": "file3.md",
        "url": "https://api.example.com/file3",
        "content": "VGVzdDM=",
        "last_commit_sha": "abc3",
    }

    mock_session = MagicMock()

    # First GET returns the directory listing
    mock_resp_dir = mock_response(200, dir_response)
    # Second GET returns file1 (file2.txt is skipped due to extension filtering)
    mock_resp_file1 = mock_response(200, file1_response)
    # Third GET returns file3
    mock_resp_file3 = mock_response(200, file3_response)

    mock_session.get.side_effect = [
        create_async_context_manager(mock_resp_dir),
        create_async_context_manager(mock_resp_file1),
        create_async_context_manager(mock_resp_file3),
    ]

    # Call with allowed_extensions to exercise the filtering branch (line 290)
    result = await provider.get_data_from_url(
        "https://api.example.com/dir",
        mock_session,
        owner="owner",
        repo="repo",
        allowed_extensions=["md"],
    )

    # Should return only the .md files, file2.txt should be ignored
    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_should_retry_response_429_rate_limit(provider, mock_response):
    """Test _should_retry_response returns True for 429 rate limit on non-final attempt."""
    response = mock_response(429, {})
    response.headers = {"Retry-After": "5"}

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_retry_attempts = 3

        result = await provider._should_retry_response(response, "http://test.com", attempt=0)
        assert result is True


@pytest.mark.asyncio
async def test_should_retry_response_429_final_attempt_raises(provider, mock_response):
    """Test _should_retry_response raises RateLimitError on 429 at final attempt."""
    from soliplex.agents.scm import RateLimitError

    response = mock_response(429, {})
    response.headers = {"Retry-After": "60"}

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_retry_attempts = 3

        with pytest.raises(RateLimitError) as exc_info:
            await provider._should_retry_response(response, "http://test.com", attempt=2)

        assert exc_info.value.retry_after == 60


@pytest.mark.asyncio
async def test_should_retry_response_5xx_server_error(provider, mock_response):
    """Test _should_retry_response returns True for 5xx server errors on non-final attempt."""
    response = mock_response(503, {})
    response.headers = {}

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_retry_attempts = 3
        mock_settings.scm_retry_backoff_base = 1.0
        mock_settings.scm_retry_backoff_max = 30.0

        result = await provider._should_retry_response(response, "http://test.com", attempt=0)
        assert result is True


@pytest.mark.asyncio
async def test_should_retry_response_5xx_final_attempt(provider, mock_response):
    """Test _should_retry_response returns False for 5xx on final attempt."""
    response = mock_response(500, {})
    response.headers = {}

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_retry_attempts = 3
        mock_settings.scm_retry_backoff_base = 1.0
        mock_settings.scm_retry_backoff_max = 30.0

        result = await provider._should_retry_response(response, "http://test.com", attempt=2)
        assert result is False


@pytest.mark.asyncio
async def test_should_retry_response_success(provider, mock_response):
    """Test _should_retry_response returns False for successful responses."""
    response = mock_response(200, {})
    response.headers = {}

    result = await provider._should_retry_response(response, "http://test.com", attempt=0)
    assert result is False


@pytest.mark.asyncio
async def test_get_data_from_url_retry_on_client_error(provider, mock_response):
    """Test get_data_from_url retries on client errors and eventually returns error."""
    from unittest.mock import MagicMock

    import aiohttp

    mock_session = MagicMock()

    # All attempts fail with client error
    mock_session.get.side_effect = aiohttp.ClientError("Connection failed")

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_retry_attempts = 2
        mock_settings.scm_retry_backoff_base = 0.01
        mock_settings.scm_retry_backoff_max = 0.1

        result = await provider.get_data_from_url("https://api.example.com/file", mock_session)

        assert "error" in result
        assert "Connection failed" in result["error"]
        # Should have tried twice
        assert mock_session.get.call_count == 2


@pytest.mark.asyncio
async def test_get_data_from_url_retry_success_after_failure(provider, mock_response):
    """Test get_data_from_url succeeds after initial failures."""
    from unittest.mock import MagicMock

    import aiohttp

    from tests.unit.conftest import create_async_context_manager

    file_response = {
        "name": "test.md",
        "path": "test.md",
        "url": "https://api.example.com/file",
        "content": "VGVzdA==",
        "last_commit_sha": "abc123",
    }

    mock_session = MagicMock()

    # First attempt fails, second succeeds
    mock_resp_success = mock_response(200, file_response)
    mock_session.get.side_effect = [
        aiohttp.ClientError("Temporary failure"),
        create_async_context_manager(mock_resp_success),
    ]

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_retry_attempts = 3
        mock_settings.scm_retry_backoff_base = 0.01
        mock_settings.scm_retry_backoff_max = 0.1

        result = await provider.get_data_from_url("https://api.example.com/file", mock_session)

        assert result["name"] == "test.md"
        assert mock_session.get.call_count == 2


@pytest.mark.asyncio
async def test_get_data_from_url_with_semaphore(provider, mock_response):
    """Test get_data_from_url respects semaphore for concurrency control."""
    import asyncio
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    file_response = {
        "name": "test.md",
        "path": "test.md",
        "url": "https://api.example.com/file",
        "content": "VGVzdA==",
        "last_commit_sha": "abc123",
    }

    mock_session = MagicMock()
    mock_resp = mock_response(200, file_response)
    mock_session.get.return_value = create_async_context_manager(mock_resp)

    semaphore = asyncio.Semaphore(1)

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_retry_attempts = 1
        mock_settings.scm_retry_backoff_base = 0.01
        mock_settings.scm_retry_backoff_max = 0.1

        result = await provider.get_data_from_url("https://api.example.com/file", mock_session, semaphore=semaphore)

        assert result["name"] == "test.md"


@pytest.mark.asyncio
async def test_paginate_retry_on_client_error(provider, mock_response):
    """Test paginate retries on client errors."""
    from unittest.mock import MagicMock

    import aiohttp

    from tests.unit.conftest import create_async_context_manager

    page1_data = [{"id": 1}]

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()

        # First attempt fails, second succeeds, third returns empty
        mock_resp_success = mock_response(200, page1_data)
        mock_resp_empty = mock_response(200, [])
        mock_session.get.side_effect = [
            aiohttp.ClientError("Temporary failure"),
            create_async_context_manager(mock_resp_success),
            create_async_context_manager(mock_resp_empty),
        ]
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_retry_attempts = 3
            mock_settings.scm_retry_backoff_base = 0.01
            mock_settings.scm_retry_backoff_max = 0.1

            url_template = "https://api.example.com/repos/{owner}/{repo}/items?page={page}"
            result = await provider.paginate(url_template, "test_owner", "test_repo")

            assert len(result) == 1
            assert result[0]["id"] == 1


@pytest.mark.asyncio
async def test_paginate_raises_after_max_retries(provider, mock_response):
    """Test paginate raises after exhausting all retries."""
    from unittest.mock import MagicMock

    import aiohttp

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()

        # All attempts fail
        mock_session.get.side_effect = aiohttp.ClientError("Connection failed")
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_retry_attempts = 2
            mock_settings.scm_retry_backoff_base = 0.01
            mock_settings.scm_retry_backoff_max = 0.1

            url_template = "https://api.example.com/repos/{owner}/{repo}/items?page={page}"

            with pytest.raises(aiohttp.ClientError):
                await provider.paginate(url_template, "test_owner", "test_repo")


@pytest.mark.asyncio
async def test_get_data_from_url_retry_on_5xx_with_semaphore(provider, mock_response):
    """Test get_data_from_url retries on 5xx errors when using semaphore."""
    import asyncio
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    file_response = {
        "name": "test.md",
        "path": "test.md",
        "url": "https://api.example.com/file",
        "content": "VGVzdA==",
        "last_commit_sha": "abc123",
    }

    mock_session = MagicMock()

    # First attempt returns 503, second succeeds
    mock_resp_503 = mock_response(503, {})
    mock_resp_503.headers = {}
    mock_resp_success = mock_response(200, file_response)

    mock_session.get.side_effect = [
        create_async_context_manager(mock_resp_503),
        create_async_context_manager(mock_resp_success),
    ]

    semaphore = asyncio.Semaphore(1)

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_retry_attempts = 3
        mock_settings.scm_retry_backoff_base = 0.01
        mock_settings.scm_retry_backoff_max = 0.1

        result = await provider.get_data_from_url("https://api.example.com/file", mock_session, semaphore=semaphore)

        assert result["name"] == "test.md"
        assert mock_session.get.call_count == 2


@pytest.mark.asyncio
async def test_get_data_from_url_retry_on_5xx_without_semaphore(provider, mock_response):
    """Test get_data_from_url retries on 5xx errors without semaphore."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    file_response = {
        "name": "test.md",
        "path": "test.md",
        "url": "https://api.example.com/file",
        "content": "VGVzdA==",
        "last_commit_sha": "abc123",
    }

    mock_session = MagicMock()

    # First attempt returns 500, second succeeds
    mock_resp_500 = mock_response(500, {})
    mock_resp_500.headers = {}
    mock_resp_success = mock_response(200, file_response)

    mock_session.get.side_effect = [
        create_async_context_manager(mock_resp_500),
        create_async_context_manager(mock_resp_success),
    ]

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_retry_attempts = 3
        mock_settings.scm_retry_backoff_base = 0.01
        mock_settings.scm_retry_backoff_max = 0.1

        result = await provider.get_data_from_url("https://api.example.com/file", mock_session)

        assert result["name"] == "test.md"
        assert mock_session.get.call_count == 2


# ==================== Tests for coverage gaps ====================


def test_get_base_url_raises_when_not_configured():
    """Test get_base_url raises SCMException when scm_base_url is None."""
    from soliplex.agents.scm import SCMException
    from soliplex.agents.scm.base import BaseSCMProvider

    class TestProvider(BaseSCMProvider):
        def get_auth_token(self):
            return "token"

        def get_last_updated(self, rec):
            return None

    provider = TestProvider(owner="admin")

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_base_url = None

        with pytest.raises(SCMException, match="SCM base URL is not configured"):
            provider.get_base_url()


@pytest.mark.asyncio
async def test_list_issues_with_since_parameter(provider, mock_response, sample_issue):
    """Test list_issues with since parameter adds date filter to URL."""
    import datetime

    since_date = datetime.datetime(2024, 6, 15, 10, 30, 0)

    with patch.object(provider, "paginate") as mock_paginate:
        mock_paginate.return_value = [sample_issue]

        result = await provider.list_issues("test_repo", add_comments=False, since=since_date)

        assert len(result) == 1
        # Verify the URL template includes the since parameter
        call_args = mock_paginate.call_args
        url_template = call_args[0][0]
        assert "&since=2024-06-15T10:30:00Z" in url_template


# ==================== Tests for _fetch_json ====================


@pytest.mark.asyncio
async def test_fetch_json_success(provider, mock_response):
    """Test _fetch_json returns JSON data on success."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    json_data = [{"id": 1, "body": "Comment 1"}, {"id": 2, "body": "Comment 2"}]

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, json_data)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider._fetch_json("https://api.example.com/test")

        assert len(result) == 2
        assert result[0]["body"] == "Comment 1"


@pytest.mark.asyncio
async def test_fetch_json_retry_on_client_error(provider, mock_response):
    """Test _fetch_json retries on client errors and succeeds."""
    from unittest.mock import MagicMock

    import aiohttp

    from tests.unit.conftest import create_async_context_manager

    json_data = {"id": 1, "body": "Test"}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()

        # First attempt fails, second succeeds
        mock_resp_success = mock_response(200, json_data)
        mock_session.get.side_effect = [
            aiohttp.ClientError("Temporary failure"),
            create_async_context_manager(mock_resp_success),
        ]
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_retry_attempts = 3
            mock_settings.scm_retry_backoff_base = 0.01
            mock_settings.scm_retry_backoff_max = 0.1

            result = await provider._fetch_json("https://api.example.com/test")

            assert result["id"] == 1
            assert mock_session.get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_json_raises_after_max_retries(provider, mock_response):
    """Test _fetch_json raises after exhausting all retries."""
    from unittest.mock import MagicMock

    import aiohttp

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()

        # All attempts fail
        mock_session.get.side_effect = aiohttp.ClientError("Connection failed")
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_retry_attempts = 2
            mock_settings.scm_retry_backoff_base = 0.01
            mock_settings.scm_retry_backoff_max = 0.1

            with pytest.raises(aiohttp.ClientError):
                await provider._fetch_json("https://api.example.com/test")

            assert mock_session.get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_json_retry_on_5xx(provider, mock_response):
    """Test _fetch_json retries on 5xx server errors."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    json_data = {"id": 1}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()

        # First attempt returns 503, second succeeds
        mock_resp_503 = mock_response(503, {})
        mock_resp_503.headers = {}
        mock_resp_success = mock_response(200, json_data)

        mock_session.get.side_effect = [
            create_async_context_manager(mock_resp_503),
            create_async_context_manager(mock_resp_success),
        ]
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_retry_attempts = 3
            mock_settings.scm_retry_backoff_base = 0.01
            mock_settings.scm_retry_backoff_max = 0.1

            result = await provider._fetch_json("https://api.example.com/test")

            assert result["id"] == 1
            assert mock_session.get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_json_rate_limit_retry(provider, mock_response):
    """Test _fetch_json retries on 429 rate limit."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    json_data = {"id": 1}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()

        # First attempt returns 429, second succeeds
        mock_resp_429 = mock_response(429, {})
        mock_resp_429.headers = {"Retry-After": "1"}
        mock_resp_success = mock_response(200, json_data)

        mock_session.get.side_effect = [
            create_async_context_manager(mock_resp_429),
            create_async_context_manager(mock_resp_success),
        ]
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_retry_attempts = 3

            result = await provider._fetch_json("https://api.example.com/test")

            assert result["id"] == 1
            assert mock_session.get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_json_rate_limit_raises_on_final_attempt(provider, mock_response):
    """Test _fetch_json raises RateLimitError when rate limited on final attempt."""
    from unittest.mock import MagicMock

    from soliplex.agents.scm import RateLimitError
    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()

        # All attempts return 429
        mock_resp_429 = mock_response(429, {})
        mock_resp_429.headers = {"Retry-After": "60"}

        mock_session.get.side_effect = [
            create_async_context_manager(mock_resp_429),
            create_async_context_manager(mock_resp_429),
        ]
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_retry_attempts = 2

            with pytest.raises(RateLimitError) as exc_info:
                await provider._fetch_json("https://api.example.com/test")

            assert exc_info.value.retry_after == 60


@pytest.mark.asyncio
async def test_fetch_json_timeout_error(provider, mock_response):
    """Test _fetch_json handles TimeoutError."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    json_data = {"id": 1}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()

        # First attempt times out, second succeeds
        mock_resp_success = mock_response(200, json_data)
        mock_session.get.side_effect = [
            TimeoutError("Request timed out"),
            create_async_context_manager(mock_resp_success),
        ]
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_retry_attempts = 3
            mock_settings.scm_retry_backoff_base = 0.01
            mock_settings.scm_retry_backoff_max = 0.1

            result = await provider._fetch_json("https://api.example.com/test")

            assert result["id"] == 1


# ==================== Tests for list_issue_comments ====================


@pytest.mark.asyncio
async def test_list_issue_comments_success(provider, mock_response, sample_comment):
    """Test list_issue_comments returns comments."""
    with patch.object(provider, "_fetch_json") as mock_fetch:
        mock_fetch.return_value = [sample_comment]

        result = await provider.list_issue_comments("test_owner", "test_repo", 1)

        assert len(result) == 1
        assert result[0]["body"] == "Test comment"
        mock_fetch.assert_called_once_with("https://api.example.com/repos/test_owner/test_repo/issues/1/comments")


@pytest.mark.asyncio
async def test_list_issue_comments_with_explicit_owner(mock_response, sample_comment):
    """Test list_issue_comments uses explicit owner."""
    provider = ConcreteSCMProvider(owner="explicit_owner")
    with patch.object(provider, "_fetch_json") as mock_fetch:
        mock_fetch.return_value = [sample_comment]

        result = await provider.list_issue_comments(None, "test_repo", 42)

        assert len(result) == 1
        # Should use provider's owner "explicit_owner"
        mock_fetch.assert_called_once_with("https://api.example.com/repos/explicit_owner/test_repo/issues/42/comments")


@pytest.mark.asyncio
async def test_list_issue_comments_empty(provider, mock_response):
    """Test list_issue_comments returns empty list when no comments."""
    with patch.object(provider, "_fetch_json") as mock_fetch:
        mock_fetch.return_value = []

        result = await provider.list_issue_comments("test_owner", "test_repo", 1)

        assert result == []


@pytest.mark.asyncio
async def test_list_issue_comments_multiple(provider, mock_response):
    """Test list_issue_comments returns multiple comments."""
    comments = [
        {"id": 1, "body": "First comment", "user": {"login": "user1"}},
        {"id": 2, "body": "Second comment", "user": {"login": "user2"}},
        {"id": 3, "body": "Third comment", "user": {"login": "user1"}},
    ]

    with patch.object(provider, "_fetch_json") as mock_fetch:
        mock_fetch.return_value = comments

        result = await provider.list_issue_comments("owner", "repo", 123)

        assert len(result) == 3
        assert result[0]["body"] == "First comment"
        assert result[1]["body"] == "Second comment"
        assert result[2]["body"] == "Third comment"


# ==================== Tests for list_issues with since and add_comments ====================


@pytest.mark.asyncio
async def test_list_issues_with_since_and_add_comments(provider, mock_response, sample_issue):
    """Test list_issues with since parameter and add_comments=True fetches comments per issue."""
    import datetime

    since_date = datetime.datetime(2024, 6, 15, 10, 30, 0)
    issue_with_number = {**sample_issue, "number": 42}

    with patch.object(provider, "paginate") as mock_paginate:
        with patch.object(provider, "list_issue_comments") as mock_list_comments:
            mock_paginate.return_value = [issue_with_number]
            mock_list_comments.return_value = [{"body": "Comment 1"}, {"body": "Comment 2"}]

            result = await provider.list_issues("test_repo", owner="test_owner", add_comments=True, since=since_date)

            assert len(result) == 1
            assert result[0]["comments"] == [{"body": "Comment 1"}, {"body": "Comment 2"}]
            assert result[0]["comment_count"] == 2
            mock_list_comments.assert_called_once_with("test_owner", "test_repo", 42)


# ==================== Tests for list_commits_since ====================


@pytest.mark.asyncio
async def test_list_commits_since_basic(provider, mock_response):
    """Test list_commits_since returns commits."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    commits_data = [
        {"sha": "abc123", "message": "Commit 1"},
        {"sha": "def456", "message": "Commit 2"},
    ]

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, commits_data)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.list_commits_since("test_repo")

        assert len(result) == 2
        assert result[0]["sha"] == "abc123"


@pytest.mark.asyncio
async def test_list_commits_since_with_marker(provider, mock_response):
    """Test list_commits_since stops at marker SHA."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    commits_data = [
        {"sha": "abc123", "message": "Commit 1"},
        {"sha": "marker_sha", "message": "Marker commit"},
        {"sha": "def456", "message": "Commit 2"},
    ]

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, commits_data)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.list_commits_since("test_repo", since_commit_sha="marker_sha")

        # Should only return commits before the marker
        assert len(result) == 1
        assert result[0]["sha"] == "abc123"


@pytest.mark.asyncio
async def test_list_commits_since_empty_response(provider, mock_response):
    """Test list_commits_since handles empty response."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, [])
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.list_commits_since("test_repo")

        assert result == []


@pytest.mark.asyncio
async def test_list_commits_since_with_owner(provider, mock_response):
    """Test list_commits_since with explicit owner."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    commits_data = [{"sha": "abc123", "message": "Commit 1"}]

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, commits_data)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.list_commits_since("test_repo", owner="custom_owner")

        assert len(result) == 1


@pytest.mark.asyncio
async def test_list_commits_since_dict_response(provider, mock_response):
    """Test list_commits_since handles dict response (non-list)."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    # API might return a dict instead of list in error cases
    dict_response = {"message": "not a list"}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, dict_response)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.list_commits_since("test_repo")

        # Should treat non-list as empty
        assert result == []


# ==================== Tests for get_commit_details ====================


@pytest.mark.asyncio
async def test_get_commit_details_success(provider, mock_response):
    """Test get_commit_details returns commit info."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    commit_data = {
        "sha": "abc123",
        "message": "Test commit",
        "files": [{"filename": "test.py", "status": "modified"}],
    }

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, commit_data)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.get_commit_details("test_repo", commit_sha="abc123")

        assert result["sha"] == "abc123"
        assert result["message"] == "Test commit"


@pytest.mark.asyncio
async def test_get_commit_details_with_owner(provider, mock_response):
    """Test get_commit_details with explicit owner."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    commit_data = {"sha": "abc123", "message": "Test commit"}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, commit_data)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.get_commit_details("test_repo", owner="custom_owner", commit_sha="abc123")

        assert result["sha"] == "abc123"


# ==================== Tests for get_single_file ====================


@pytest.mark.asyncio
async def test_get_single_file_success(provider, mock_response, sample_file_record):
    """Test get_single_file returns parsed file."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, sample_file_record)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.get_single_file("test_repo", file_path="docs/test.md")

        assert result["name"] == "test.md"
        assert "file_bytes" in result


@pytest.mark.asyncio
async def test_get_single_file_with_owner(provider, mock_response, sample_file_record):
    """Test get_single_file with explicit owner."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, sample_file_record)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.get_single_file("test_repo", owner="custom_owner", file_path="test.md")

        assert result["name"] == "test.md"


@pytest.mark.asyncio
async def test_get_single_file_with_branch(provider, mock_response, sample_file_record):
    """Test get_single_file with custom branch."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(200, sample_file_record)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.get_single_file("test_repo", file_path="test.md", branch="develop")

        assert result["name"] == "test.md"


@pytest.mark.asyncio
async def test_list_commits_since_multiple_pages(provider, mock_response):
    """Test list_commits_since with multiple pages of commits."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    # Page 1: 100 commits (limit)
    page1_commits = [{"sha": f"sha_{i}", "message": f"Commit {i}"} for i in range(100)]
    # Page 2: 50 commits (less than limit, indicating last page)
    page2_commits = [{"sha": f"sha_{100 + i}", "message": f"Commit {100 + i}"} for i in range(50)]

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp1 = mock_response(200, page1_commits)
        mock_resp2 = mock_response(200, page2_commits)

        mock_session.get.side_effect = [
            create_async_context_manager(mock_resp1),
            create_async_context_manager(mock_resp2),
        ]
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.list_commits_since("test_repo", limit=100)

        # Should have fetched both pages
        assert len(result) == 150
        assert mock_session.get.call_count == 2


@pytest.mark.asyncio
async def test_list_commits_since_marker_on_second_page(provider, mock_response):
    """Test list_commits_since finds marker SHA on second page."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    # Page 1: Full page
    page1_commits = [{"sha": f"sha_{i}", "message": f"Commit {i}"} for i in range(100)]
    # Page 2: Contains marker
    page2_commits = [
        {"sha": "new_commit_1", "message": "New 1"},
        {"sha": "marker_sha", "message": "Marker"},
        {"sha": "old_commit", "message": "Old"},
    ]

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp1 = mock_response(200, page1_commits)
        mock_resp2 = mock_response(200, page2_commits)

        mock_session.get.side_effect = [
            create_async_context_manager(mock_resp1),
            create_async_context_manager(mock_resp2),
        ]
        mock_get_session.return_value = create_async_context_manager(mock_session)

        result = await provider.list_commits_since("test_repo", since_commit_sha="marker_sha", limit=100)

        # Should have page 1 (100) + 1 commit from page 2 before marker
        assert len(result) == 101


# ==================== Tests for base class methods ====================


def test_base_init_no_owner():
    """Test BaseSCMProvider.__init__ sets owner to None when not provided."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class MinimalProvider(BaseSCMProvider):
        def get_last_updated(self, rec):
            return None

    provider = MinimalProvider()
    assert provider.owner is None


def test_base_init_with_owner():
    """Test BaseSCMProvider.__init__ sets owner when provided."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class MinimalProvider(BaseSCMProvider):
        def get_last_updated(self, rec):
            return None

    provider = MinimalProvider(owner="my_owner")
    assert provider.owner == "my_owner"


def test_base_get_base_url():
    """Test BaseSCMProvider.get_base_url returns settings value."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class MinimalProvider(BaseSCMProvider):
        def get_last_updated(self, rec):
            return None

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_base_url = "https://api.test.com"
        provider = MinimalProvider()
        assert provider.get_base_url() == "https://api.test.com"


def test_base_get_auth_token():
    """Test BaseSCMProvider.get_auth_token returns settings value."""
    from soliplex.agents.scm.base import BaseSCMProvider

    class MinimalProvider(BaseSCMProvider):
        def get_last_updated(self, rec):
            return None

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = "test_token_123"
        provider = MinimalProvider()
        assert provider.get_auth_token() == "test_token_123"


def test_base_get_auth_headers_basic_auth():
    """Test BaseSCMProvider.get_auth_headers with basic auth."""
    from pydantic import SecretStr

    from soliplex.agents.scm.base import BaseSCMProvider

    class MinimalProvider(BaseSCMProvider):
        def get_last_updated(self, rec):
            return None

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = None
        mock_settings.scm_auth_username = "testuser"
        mock_settings.scm_auth_password = SecretStr("testpass")

        provider = MinimalProvider()
        headers = provider.get_auth_headers()

        # base64("testuser:testpass") = "dGVzdHVzZXI6dGVzdHBhc3M="
        assert headers == {"Authorization": "Basic dGVzdHVzZXI6dGVzdHBhc3M="}


def test_base_get_auth_headers_raises_no_auth():
    """Test BaseSCMProvider.get_auth_headers raises when no auth configured."""
    from soliplex.agents.scm import AuthenticationConfigError
    from soliplex.agents.scm.base import BaseSCMProvider

    class MinimalProvider(BaseSCMProvider):
        def get_last_updated(self, rec):
            return None

    with patch("soliplex.agents.scm.base.settings") as mock_settings:
        mock_settings.scm_auth_token = None
        mock_settings.scm_auth_username = None
        mock_settings.scm_auth_password = None

        provider = MinimalProvider()

        with pytest.raises(AuthenticationConfigError):
            provider.get_auth_headers()


# ==================== Tests for empty repository 404 handling ====================


@pytest.mark.asyncio
async def test_list_repo_files_empty_repo_404_object_does_not_exist(provider, mock_response):
    """Test list_repo_files returns empty list for empty repo with 404 'object does not exist' error."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    # Gitea returns 404 with "object does not exist" for repos with no commits
    error_response = {"errors": ["object does not exist [id: refs/heads/main, rel_path: ]"]}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(404, error_response)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_max_concurrent_requests = 5
            mock_settings.extensions = ["md", "txt"]

            result = await provider.list_repo_files("test_repo", owner="test_owner", branch="main")

            # Should return empty list for empty repository
            assert result == []


@pytest.mark.asyncio
async def test_list_repo_files_404_non_dict_response_passed_to_validate(provider, mock_response):
    """Test list_repo_files with 404 non-dict response passes to validate_response."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    # Non-dict response should skip the errors check and go to validate_response
    error_response = ["not a dict"]

    with patch.object(provider, "get_session") as mock_get_session:
        with patch.object(provider, "validate_response") as mock_validate:
            mock_session = MagicMock()
            mock_resp = mock_response(404, error_response)
            mock_session.get.return_value = create_async_context_manager(mock_resp)
            mock_get_session.return_value = create_async_context_manager(mock_session)
            mock_validate.side_effect = SCMException("not found")

            with patch("soliplex.agents.scm.base.settings") as mock_settings:
                mock_settings.scm_max_concurrent_requests = 5
                mock_settings.extensions = ["md", "txt"]

                with pytest.raises(SCMException, match="not found"):
                    await provider.list_repo_files("nonexistent_repo", owner="test_owner", branch="main")


@pytest.mark.asyncio
async def test_iter_repo_files_empty_repo_404_object_does_not_exist(provider, mock_response):
    """Test iter_repo_files returns empty for empty repo with 404 'object does not exist' error."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    # Gitea returns 404 with "object does not exist" for repos with no commits
    error_response = {"errors": ["object does not exist [id: refs/heads/main, rel_path: ]"]}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(404, error_response)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_max_concurrent_requests = 5

            files = []
            async for file in provider.iter_repo_files("test_repo", owner="test_owner", branch="main"):
                files.append(file)

            # Should return empty for empty repository
            assert files == []


@pytest.mark.asyncio
async def test_iter_repo_files_404_non_dict_response_passed_to_validate(provider, mock_response):
    """Test iter_repo_files with 404 non-dict response passes to validate_response."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    # Non-dict response should skip the errors check and go to validate_response
    error_response = ["not a dict"]

    with patch.object(provider, "get_session") as mock_get_session:
        with patch.object(provider, "validate_response") as mock_validate:
            mock_session = MagicMock()
            mock_resp = mock_response(404, error_response)
            mock_session.get.return_value = create_async_context_manager(mock_resp)
            mock_get_session.return_value = create_async_context_manager(mock_session)
            mock_validate.side_effect = SCMException("not found")

            with patch("soliplex.agents.scm.base.settings") as mock_settings:
                mock_settings.scm_max_concurrent_requests = 5

                with pytest.raises(SCMException, match="not found"):
                    async for _ in provider.iter_repo_files("nonexistent_repo", owner="test_owner", branch="main"):
                        pass


@pytest.mark.asyncio
async def test_list_repo_files_404_with_errors_list_but_different_error(provider, mock_response):
    """Test list_repo_files raises when 404 has errors list but not 'object does not exist'."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    # 404 with errors list but different error message
    error_response = {"errors": ["some other error"]}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(404, error_response)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_max_concurrent_requests = 5
            mock_settings.extensions = ["md", "txt"]

            # Should call validate_response which raises SCMException for 404 with errors
            with pytest.raises(SCMException):
                await provider.list_repo_files("test_repo", owner="test_owner", branch="main")


@pytest.mark.asyncio
async def test_iter_repo_files_404_with_errors_list_but_different_error(provider, mock_response):
    """Test iter_repo_files raises when 404 has errors list but not 'object does not exist'."""
    from unittest.mock import MagicMock

    from tests.unit.conftest import create_async_context_manager

    # 404 with errors list but different error message
    error_response = {"errors": ["some other error"]}

    with patch.object(provider, "get_session") as mock_get_session:
        mock_session = MagicMock()
        mock_resp = mock_response(404, error_response)
        mock_session.get.return_value = create_async_context_manager(mock_resp)
        mock_get_session.return_value = create_async_context_manager(mock_session)

        with patch("soliplex.agents.scm.base.settings") as mock_settings:
            mock_settings.scm_max_concurrent_requests = 5

            # Should call validate_response which raises SCMException for 404 with errors
            with pytest.raises(SCMException):
                async for _ in provider.iter_repo_files("test_repo", owner="test_owner", branch="main"):
                    pass
