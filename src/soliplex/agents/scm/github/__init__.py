"""GitHub SCM provider implementation."""

import logging
from typing import Any

import aiohttp

from soliplex.agents.config import settings
from soliplex.agents.scm import GitHubAPIError
from soliplex.agents.scm import SCMException
from soliplex.agents.scm.base import BaseSCMProvider

logger = logging.getLogger(__name__)


class GitHubProvider(BaseSCMProvider):
    """GitHub implementation of SCM provider."""

    def get_base_url(self) -> str:
        """Get the base API URL for GitHub."""
        return settings.scm_base_url or "https://api.github.com"

    def get_last_updated(self, rec: dict[str, Any]) -> str | None:
        """
        Extract last updated timestamp from GitHub file record.

        GitHub API doesn't provide last updated timestamp in the contents API,
        so this returns None.
        """
        return None

    async def validate_response(self, response: aiohttp.ClientResponse, resp: dict | list) -> None:
        """
        Validate GitHub API response.

        Args:
            response: HTTP response
            resp: Parsed JSON response

        Raises:
            SCMException: If response indicates an error
        """
        if response.status != 200:
            if isinstance(resp, dict) and "message" in resp:
                raise SCMException(str(resp["message"]))
            logger.error("GitHub API error: status %s", response.status)
            raise GitHubAPIError

        if isinstance(resp, dict) and "errors" in resp:
            raise SCMException(str(resp))

    async def is_empty_branch(self, session: aiohttp.ClientSession, owner: str, repo: str, branch: str, resp: Any) -> bool:
        """
        Name a missing branch behind a contents 404; never list one as empty.

        A GitHub branch always has a commit, so an existing branch with no
        files lists as a 200 with ``[]`` and never reaches here. A 404 is an
        error either way (an empty repository included); this only makes the
        missing-branch case say so.

        Raises:
            SCMException: If *branch* does not exist
        """
        if not await self._branch_exists(session, owner, repo, branch):
            raise SCMException(f"branch '{branch}' not found in {owner}/{repo}")
        return False

    def _commits_url(self, owner: str, repo: str, branch: str, limit: int) -> str:
        """GitHub pages commits with ``per_page`` (it ignores ``limit``, and defaults to 30)."""
        return self.build_url(f"/repos/{owner}/{repo}/commits?sha={branch}&per_page={limit}")

    def _issue_is_gone(self, response: aiohttp.ClientResponse) -> bool:
        """
        404 or 410 (a deleted issue), or a redirect: an issue transferred to
        another repository answers with a 301 to its new home, which is no
        longer this repository's issue.
        """
        return response.status in (404, 410) or bool(response.history)

    async def get_file_content(
        self, rec: dict[str, Any], session: aiohttp.ClientSession, owner: str, repo: str
    ) -> dict[str, Any]:
        """
        Get file content, fetching blob if content is empty.

        GitHub sometimes returns empty content for large files,
        requiring a separate blob API call.

        Args:
            rec: File record
            session: HTTP session
            owner: Repository owner
            repo: Repository name

        Returns:
            Updated file record with content
        """
        if "content" not in rec or rec["content"] is None or len(rec["content"]) == 0:
            rec["content"] = await self.get_blob(repo, owner, rec, session)
        return rec

    async def get_blob(self, repo: str, owner: str, rec: dict[str, Any], session: aiohttp.ClientSession) -> bytes:
        """
        Fetch blob content from GitHub API.

        Args:
            repo: Repository name
            owner: Repository owner
            rec: File record with 'sha' field
            session: HTTP session

        Returns:
            Blob content as bytes
        """
        sha = rec["sha"]
        url = self.build_url(f"/repos/{owner}/{repo}/git/blobs/{sha}")
        logger.debug(f"Fetching blob from {url}")

        response = await self._request_with_retry(session, url)
        async with response:
            response.raise_for_status()
            return await response.read()
