"""Gitea SCM provider implementation."""

import logging
from typing import Any

import aiohttp

from soliplex.agents.scm import SCMException
from soliplex.agents.scm.base import BaseSCMProvider

logger = logging.getLogger(__name__)


class GiteaProvider(BaseSCMProvider):
    """Gitea implementation of SCM provider."""

    def get_last_updated(self, rec: dict[str, Any]) -> str | None:
        """Extract last updated timestamp from Gitea file record."""
        return rec.get("last_committer_date")

    async def is_empty_branch(self, session: aiohttp.ClientSession, owner: str, repo: str, branch: str, resp: Any) -> bool:
        """
        Tell an empty repository from a missing branch behind a contents 404.

        Gitea answers ``contents?ref=<branch>`` with 404 "object does not
        exist" when there is no tree to list. That is an empty listing only
        when the branch exists, or when the repository has no commits at all
        (Gitea's ``empty`` flag; such a repository has no branches either).
        A branch missing from a repository that has commits raises, so a typo
        in ``branch`` can never list as a repository with no files.

        Raises:
            SCMException: If *branch* does not exist in a non-empty repository
        """
        if await self._branch_exists(session, owner, repo, branch):
            errors = resp.get("errors", []) if isinstance(resp, dict) else []
            return any("object does not exist" in str(e) for e in errors)
        rec = await self.get_repo(repo, owner)
        if isinstance(rec, dict) and rec.get("empty"):
            return True
        raise SCMException(f"branch '{branch}' not found in {owner}/{repo}")
