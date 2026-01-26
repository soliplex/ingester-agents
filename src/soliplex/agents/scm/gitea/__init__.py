"""Gitea SCM provider implementation."""

import logging
from typing import Any

from soliplex.agents.scm.base import BaseSCMProvider

logger = logging.getLogger(__name__)


class GiteaProvider(BaseSCMProvider):
    """Gitea implementation of SCM provider."""

    def get_last_updated(self, rec: dict[str, Any]) -> str | None:
        """Extract last updated timestamp from Gitea file record."""
        return rec.get("last_committer_date")
