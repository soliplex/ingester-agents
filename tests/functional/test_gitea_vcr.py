"""VCR-based functional tests for Gitea SCM provider.

These tests use pytest-vcr to record and replay HTTP interactions.
Environment variables are configured in pyproject.toml [tool.pytest_env]:
- scm_auth_token: Gitea API token
- scm_base_url: Gitea API base URL

To record cassettes:
1. Start a local Gitea instance at http://localhost:3000
2. Start a local Ingester API at http://localhost:8000
3. Delete existing cassettes in tests/functional/cassettes/
4. Run: uv run pytest tests/functional/test_gitea_vcr.py -v

To replay (CI mode):
    uv run pytest tests/functional/test_gitea_vcr.py -v --vcr-record=none
"""

from pathlib import Path

import pytest

from soliplex.agents.config import SCM
from soliplex.agents.scm import SCMException
from soliplex.agents.scm import app as scm_app
from soliplex.agents.scm.gitea import GiteaProvider

# Fixed repo names for deterministic cassette matching
REPO_CREATE = "vcr-test-create-repo"
REPO_FILE = "vcr-test-repo-with-file"
REPO_ISSUE = "vcr-test-repo-with-issue"
REPO_INVENTORY_EMPTY = "vcr-test-inventory-empty"
REPO_SYNC_EMPTY = "vcr-test-sync-empty"
REPO_INVENTORY_FILE = "vcr-test-inventory-file"
REPO_SYNC_ISSUE = "vcr-test-sync-issue"


@pytest.fixture(scope="module")
def vcr_config():
    """Configure VCR for Gitea API testing.

    - filter_headers: Remove authorization tokens from recorded cassettes
    - record_mode: 'once' records new cassettes, replays existing ones
    - match_on: Match requests by method, scheme, host, port, path, query
    """
    return {
        "filter_headers": ["authorization"],
        "record_mode": "once",
        "match_on": ["method", "scheme", "host", "port", "path", "query"],
    }


@pytest.fixture(scope="module")
def vcr_cassette_dir():
    """Store cassettes in tests/functional/cassettes/ directory."""
    return str(Path(__file__).parent / "cassettes")


@pytest.fixture
def provider():
    """Create GiteaProvider instance using environment variables from pytest_env."""
    return GiteaProvider()


# ==================== Test: Create Repository ====================


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_create_repository(provider):
    """Test creating a new Gitea repository."""
    try:
        result = await provider.create_repository(
            name=REPO_CREATE,
            description="Test repository created by VCR test",
            private=False,
        )

        assert result["name"] == REPO_CREATE
        assert "id" in result
        assert result["description"] == "Test repository created by VCR test"

    finally:
        try:
            await provider.delete_repository(REPO_CREATE)
        except SCMException:
            pass


# ==================== Test: Create Repository and Add File ====================


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_create_repository_and_add_file(provider):
    """Test creating a repository and adding a sample text file."""
    try:
        repo_result = await provider.create_repository(
            name=REPO_FILE,
            description="Repository with sample file",
            private=False,
        )
        assert repo_result["name"] == REPO_FILE

        file_content = "# Sample File\n\nThis is a test file created by VCR functional test.\n"
        file_result = await provider.create_file(
            repo=REPO_FILE,
            file_path="README.md",
            content=file_content,
            message="Add README file",
            branch="main",
        )

        assert "content" in file_result
        assert file_result["content"]["path"] == "README.md"

    finally:
        try:
            await provider.delete_repository(REPO_FILE)
        except SCMException:
            pass


# ==================== Test: Create Repository and Add Issue ====================


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_create_repository_and_add_issue(provider):
    """Test creating a repository and adding an issue."""
    try:
        repo_result = await provider.create_repository(
            name=REPO_ISSUE,
            description="Repository for issue testing",
            private=False,
        )
        assert repo_result["name"] == REPO_ISSUE

        issue_result = await provider.create_issue(
            repo=REPO_ISSUE,
            title="Test Issue from VCR",
            body="This is a test issue created by the VCR functional test suite.\n\n- Item 1\n- Item 2",
        )

        assert "id" in issue_result
        assert issue_result["title"] == "Test Issue from VCR"
        assert "number" in issue_result

    finally:
        try:
            await provider.delete_repository(REPO_ISSUE)
        except SCMException:
            pass


# ==================== Test: Empty Repo with load_inventory ====================


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_load_inventory_empty_repo(provider):
    """Test load_inventory on an empty repository returns ingested=0.

    Verifies:
    - Repository is created successfully
    - load_inventory runs without errors
    - No documents are ingested (ingested count = 0)
    """
    owner = provider.owner

    try:
        # Create empty repository
        repo_result = await provider.create_repository(
            name=REPO_INVENTORY_EMPTY,
            description="Empty repository for load_inventory test",
            private=False,
        )
        assert repo_result["name"] == REPO_INVENTORY_EMPTY

        # Run load_inventory on empty repo
        result = await scm_app.load_inventory(
            scm=SCM.GITEA,
            repo_name=REPO_INVENTORY_EMPTY,
            owner=owner,
        )

        # Verify no errors and nothing ingested
        assert "errors" not in result or len(result.get("errors", [])) == 0
        assert len(result.get("ingested", [])) == 0

    finally:
        try:
            await provider.delete_repository(REPO_INVENTORY_EMPTY)
        except SCMException:
            pass


# ==================== Test: Empty Repo with incremental_sync ====================


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_incremental_sync_empty_repo(provider):
    """Test incremental_sync on an empty repository returns ingested=0.

    Verifies:
    - Repository is created successfully
    - incremental_sync runs without errors
    - No documents are ingested (ingested count = 0)
    """
    owner = provider.owner

    try:
        # Create empty repository
        repo_result = await provider.create_repository(
            name=REPO_SYNC_EMPTY,
            description="Empty repository for incremental_sync test",
            private=False,
        )
        assert repo_result["name"] == REPO_SYNC_EMPTY

        # Run incremental_sync on empty repo
        result = await scm_app.incremental_sync(
            scm=SCM.GITEA,
            repo_name=REPO_SYNC_EMPTY,
            owner=owner,
        )

        # Verify no errors and nothing ingested
        assert "error" not in result
        assert len(result.get("ingested", [])) == 0

    finally:
        try:
            await provider.delete_repository(REPO_SYNC_EMPTY)
        except SCMException:
            pass


# ==================== Test: Repo with File and load_inventory ====================


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_load_inventory_with_file(provider):
    """Test load_inventory on a repository with a text file returns ingested=1.

    Verifies:
    - Repository is created successfully
    - File is added to repository
    - load_inventory runs without errors
    - One document is ingested (ingested count = 1)

    Note: File must have an allowed extension (md, pdf, doc, docx).
    Using test.md since .txt is not in the default allowed extensions.
    """
    owner = provider.owner

    try:
        # Create repository
        repo_result = await provider.create_repository(
            name=REPO_INVENTORY_FILE,
            description="Repository with file for load_inventory test",
            private=False,
        )
        assert repo_result["name"] == REPO_INVENTORY_FILE

        # Add a text file (using .md extension since .txt is not allowed by default)
        file_content = "this is a test file"
        await provider.create_file(
            repo=REPO_INVENTORY_FILE,
            file_path="test.md",
            content=file_content,
            message="Add test file",
            branch="main",
        )

        # Run load_inventory
        result = await scm_app.load_inventory(
            scm=SCM.GITEA,
            repo_name=REPO_INVENTORY_FILE,
            owner=owner,
        )

        # Verify no errors and one document ingested
        assert "errors" not in result or len(result.get("errors", [])) == 0
        assert len(result.get("ingested", [])) == 1

    finally:
        try:
            await provider.delete_repository(REPO_INVENTORY_FILE)
        except SCMException:
            pass


# ==================== Test: Repo with Issue and incremental_sync ====================


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_incremental_sync_with_issue(provider):
    """Test incremental_sync on a repository with an issue returns ingested=1.

    Verifies:
    - Repository is created successfully
    - Issue is created in repository
    - incremental_sync runs without errors
    - One document is ingested (the issue, ingested count = 1)
    """
    owner = provider.owner

    try:
        # Create repository
        repo_result = await provider.create_repository(
            name=REPO_SYNC_ISSUE,
            description="Repository with issue for incremental_sync test",
            private=False,
        )
        assert repo_result["name"] == REPO_SYNC_ISSUE

        # Create an issue
        issue_result = await provider.create_issue(
            repo=REPO_SYNC_ISSUE,
            title="test issue",
            body="This is a test issue for incremental sync testing.",
        )
        assert issue_result["title"] == "test issue"

        # Run incremental_sync
        result = await scm_app.incremental_sync(
            scm=SCM.GITEA,
            repo_name=REPO_SYNC_ISSUE,
            owner=owner,
        )

        # Verify no errors and one document ingested
        assert "error" not in result
        assert len(result.get("ingested", [])) == 1

    finally:
        try:
            await provider.delete_repository(REPO_SYNC_ISSUE)
        except SCMException:
            pass
