"""SCM (Source Control Management) agent API routes."""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import Query

from soliplex.agents.config import SCM
from soliplex.agents.config import ContentFilter
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


@scm_router.post("/run-inventory")
async def run_inventory(
    scm: SCM = Form(..., description="SCM provider (github/gitea)"),
    repo_name: str = Form(..., description="Repository name"),
    owner: str = Form(..., description="Repository owner"),
    start_workflows: bool = Form(True, description="Start workflows after ingestion"),
    workflow_definition_id: str | None = Form(None, description="Workflow definition ID"),
    param_set_id: str | None = Form(None, description="Parameter set ID"),
    priority: int = Form(0, description="Workflow priority"),
    content_filter: ContentFilter = Form(ContentFilter.ALL, description="Content filter: all, files, issues"),
):
    """
    Run ingestion from a SCM repository.

    Ingests files, issues, or both from the repository based on content_filter.
    """
    result = await scm_app.load_inventory(
        scm,
        repo_name,
        owner,
        start_workflows=start_workflows,
        workflow_definition_id=workflow_definition_id,
        param_set_id=param_set_id,
        priority=priority,
        content_filter=content_filter,
    )

    return {
        "status": "ok",
        "scm": scm.value,
        "repo": repo_name,
        "owner": owner,
        "inventory_count": len(result.get("inventory", [])),
        "to_process_count": len(result.get("to_process", [])),
        "ingested_count": len(result.get("ingested", [])),
        "error_count": len(result.get("errors", [])),
        "errors": result.get("errors", []),
        "workflow_result": result.get("workflow_result"),
    }


@scm_router.post("/incremental-sync")
async def run_incremental_sync(
    scm: SCM = Form(..., description="SCM provider (github/gitea)"),
    repo_name: str = Form(..., description="Repository name"),
    owner: str = Form(..., description="Repository owner"),
    branch: str = Form("main", description="Branch to sync"),
    start_workflows: bool = Form(True, description="Start workflows after ingestion"),
    workflow_definition_id: str | None = Form(None, description="Workflow definition ID"),
    param_set_id: str | None = Form(None, description="Parameter set ID"),
    priority: int = Form(0, description="Workflow priority"),
    content_filter: ContentFilter = Form(ContentFilter.ALL, description="Content filter: all, files, issues"),
):
    """
    Run incremental sync from a SCM repository.

    Only fetches and processes content that changed since last sync.
    Falls back to full sync if no sync state exists.
    """
    result = await scm_app.incremental_sync(
        scm,
        repo_name,
        owner,
        branch=branch,
        start_workflows=start_workflows,
        workflow_definition_id=workflow_definition_id,
        param_set_id=param_set_id,
        priority=priority,
        content_filter=content_filter,
    )

    if "error" in result:
        return {
            "status": "error",
            "error": result["error"],
        }

    return {
        "status": result.get("status", "ok"),
        "scm": scm.value,
        "repo": repo_name,
        "owner": owner,
        "branch": branch,
        "commits_processed": result.get("commits_processed", 0),
        "files_changed": result.get("files_changed", 0),
        "files_removed": result.get("files_removed", 0),
        "ingested_count": len(result.get("ingested", [])),
        "ingested": result.get("ingested", []),
        "error_count": len(result.get("errors", [])),
        "errors": result.get("errors", []),
        "workflow_result": result.get("workflow_result"),
        "new_commit_sha": result.get("new_commit_sha"),
    }
