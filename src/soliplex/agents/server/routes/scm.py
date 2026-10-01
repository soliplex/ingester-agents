"""SCM (Source Control Management) agent API routes."""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query

from soliplex.agents.config import SCM
from soliplex.agents.config import settings
from soliplex.agents.scm import app as scm_app
from soliplex.agents.server.auth import get_current_user

logger = logging.getLogger(__name__)

scm_router = APIRouter(
    prefix="/api/v1/scm",
    tags=["scm"],
    dependencies=[Depends(get_current_user)],
)


@scm_router.get("/{scm}/issues")
async def list_issues(
    scm: SCM,
    repo_name: str = Query(..., description="Repository name"),
    owner: str = Query(..., description="Repository owner"),
):
    """
    List issues from a GitHub or Gitea repository.

    Returns all issues with their titles, bodies, and comments.
    """
    try:
        provider = scm_app.get_scm(scm)
        issues = await provider.list_issues(repo_name, owner, add_comments=True)

        return {
            "status": "ok",
            "scm": scm.value,
            "repo": repo_name,
            "owner": owner,
            "issue_count": len(issues),
            "issues": issues,
        }
    except Exception as e:
        logger.exception("Error listing issues for %s/%s", owner, repo_name)
        raise HTTPException(status_code=500, detail=str(e)) from e


@scm_router.get("/{scm}/repo")
async def get_repo(
    scm: SCM,
    repo_name: str = Query(..., description="Repository name"),
    owner: str = Query(..., description="Repository owner"),
):
    """
    List files in a GitHub or Gitea repository.

    Returns file metadata filtered by allowed extensions.
    """
    try:
        provider = scm_app.get_scm(scm)
        files = await provider.list_repo_files(repo_name, owner, settings.extensions)

        # Return file metadata without the full file bytes
        file_list = [
            {
                "name": f.get("name"),
                "uri": f.get("uri"),
                "sha256": f.get("sha256"),
                "content_type": f.get("content-type"),
                "last_updated": f.get("last_updated"),
            }
            for f in files
        ]

        return {
            "status": "ok",
            "scm": scm.value,
            "repo": repo_name,
            "owner": owner,
            "file_count": len(file_list),
            "files": file_list,
        }
    except Exception as e:
        logger.exception("Error listing repo files for %s/%s", owner, repo_name)
        raise HTTPException(status_code=500, detail=str(e)) from e
