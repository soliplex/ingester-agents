import datetime
import hashlib
import logging

from soliplex.agents.common.config import detect_mime_type
from soliplex.agents.scm.base import BaseSCMProvider

from .. import client
from ..config import SCM
from ..config import ContentFilter
from ..config import settings
from . import gitea
from . import github
from .lib import templates

logger = logging.getLogger(__name__)


def get_scm(scm) -> BaseSCMProvider:
    if scm == SCM.GITEA:
        provider = gitea.GiteaProvider()
    elif scm == SCM.GITHUB:
        provider = github.GitHubProvider()
    else:
        raise ValueError(scm)

    # Apply git CLI decorator if enabled
    if settings.scm_use_git_cli:
        from .git_cli import GitCliDecorator

        provider = GitCliDecorator(provider)

    return provider


async def load_inventory(
    scm: str,
    repo_name: str,
    owner: str = None,
    resume_batch: int | None = None,
    priority: int = 0,
    start_workflows: bool = False,
    workflow_definition_id: str | None = None,
    param_set_id: str | None = None,
    content_filter: ContentFilter = ContentFilter.ALL,
):
    client.validate_parameters(start_workflows, workflow_definition_id, param_set_id)
    data = await get_data(scm, repo_name, owner, content_filter=content_filter)

    source = f"{scm.value}:{owner}:{repo_name}:{content_filter.value}"

    to_process = await client.check_status(data, source)
    ingested = []
    ret = {"inventory": data, "to_process": to_process, "ingested": ingested}
    logger.info(f"found {len(to_process)} to process")
    if len(to_process) == 0:
        logger.info("nothing to process. exiting")
        return ret
    found_batch_id = await client.find_batch_for_source(source)
    if found_batch_id:
        logger.info(f"found batch {found_batch_id} for {source}")
        batch_id = found_batch_id
    else:
        logger.info(f"no batch found for {source}. creating")
        batch_id = await client.create_batch(
            source,
            source,
        )
    logger.info(f"batch_id={batch_id}")

    errors = []

    for row in to_process:
        meta = row["metadata"].copy()
        for k in [
            "path",
            "sha256",
            "size",
            "source",
            "batch_id",
            "source_uri",
        ]:
            if k in meta:
                del meta[k]
        logger.info(f"starting ingest for {row['uri']}")
        mime_type = detect_mime_type(row["uri"])
        if "metadata" in row and "content-type" in row["metadata"] and row["metadata"]["content-type"]:
            mime_type = row["metadata"]["content-type"]

        res = await client.do_ingest(
            row["file_bytes"],
            row["uri"],
            meta,
            source,
            batch_id,
            mime_type,
        )
        if "error" in res:
            logger.error(f"Error ingesting {row['uri']}: {res['error']}")
            res["uri"] = row["uri"]
            res["source"] = source
            res["resumed_batch"] = resume_batch
            res["batch_id"] = batch_id
            errors.append(res)
        else:
            ingested.append(row["uri"])
    wf_res = None
    if len(errors) == 0 and start_workflows:
        wf_res = await client.start_workflows_for_batch(
            batch_id,
            workflow_definition_id,
            param_set_id,
            priority,
        )

    ret["ingested"] = ingested
    ret["errors"] = errors
    ret["workflow_result"] = wf_res
    return ret


async def get_issues(scm: str, repo_name: str, owner: str = None, since: datetime.datetime | None = None):
    """
    Get all issues for a repository formatted for ingestion

    """
    impl = get_scm(scm)
    issues = await impl.list_issues(repo=repo_name, owner=owner, add_comments=True, since=since)
    formatted = []
    for issue in issues:
        txt = await templates.render_issue(issue, owner, repo_name)
        row = {
            "file_bytes": txt.encode("utf-8"),
            "uri": f"/{owner}/{repo_name}/issues/{issue['number']}",
            "title": issue["title"],
            "metadata": {
                "date": issue["created_at"],
                "assignee": str(issue["assignee"]),
                "state": issue["state"],
                "comments": issue["comment_count"],
                "title": issue["title"],
                "content-type": "text/markdown",
            },
        }
        row["sha256"] = hashlib.sha256(row["file_bytes"], usedforsecurity=False).hexdigest()
        formatted.append(row)
    return formatted


async def get_data(scm: str, repo_name: str, owner: str = None, content_filter: ContentFilter = ContentFilter.ALL):
    doc_data = []

    if content_filter in (ContentFilter.ALL, ContentFilter.FILES):
        impl = get_scm(scm)
        allowed_extensions = settings.extensions
        files = await impl.list_repo_files(repo_name, owner, allowed_extensions=allowed_extensions)
        try:
            # Sort files by last updated
            for f in files:
                if f["last_updated"] is None:
                    f["last_updated"] = datetime.datetime.now(datetime.UTC)
                elif isinstance(f["last_updated"], str):
                    # Handle ISO 8601 format (with Z or +00:00 timezone)
                    date_str = f["last_updated"]
                    if date_str.endswith("Z"):
                        date_str = date_str[:-1] + "+00:00"
                    f["last_updated"] = datetime.datetime.fromisoformat(date_str)
            files = sorted(files, key=lambda x: x.get("last_updated"), reverse=True)
        except Exception as e:
            logger.exception("Error sorting files", exc_info=e)
        filtered_files = [x for x in files if x["name"].split(".")[-1] in allowed_extensions]
        for f in filtered_files:
            row = {
                "file_bytes": f["file_bytes"],
                "uri": f["uri"],
                "sha256": f["sha256"],
                "metadata": {
                    "last_modified_date": f["last_updated"],
                    "content-type": f["content-type"],
                    "last_commit_sha": f["last_commit_sha"],
                },
            }
            doc_data.append(row)

    if content_filter in (ContentFilter.ALL, ContentFilter.ISSUES):
        doc_data.extend(await get_issues(scm, repo_name, owner))

    return doc_data


async def incremental_sync(
    scm: str,
    repo_name: str,
    owner: str = None,
    branch: str = "main",
    priority: int = 0,
    start_workflows: bool = False,
    workflow_definition_id: str | None = None,
    param_set_id: str | None = None,
    content_filter: ContentFilter = ContentFilter.ALL,
):
    """
    Perform incremental sync based on commit history.

    Only fetches and processes files that changed since last sync.
    Falls back to full sync if no sync state exists.

    Args:
        scm: SCM type (gitea/github)
        repo_name: Repository name
        owner: Repository owner
        branch: Branch to sync
        priority: Workflow priority
        start_workflows: Whether to start workflows after ingestion
        workflow_definition_id: Optional workflow ID
        param_set_id: Optional parameter set ID

    Returns:
        Sync result dict with statistics
    """
    impl = get_scm(scm)
    source = f"{scm.value}:{owner}:{repo_name}:{content_filter.value}"

    logger.info(f"Starting incremental sync for {source}")

    # Get last sync state
    sync_state = await client.get_sync_state(source)

    if "error" in sync_state:
        logger.error(f"Error getting sync state: {sync_state['error']}")
        return {"error": sync_state["error"]}

    last_commit_sha = sync_state.get("last_commit_sha")

    if not last_commit_sha:
        logger.info("No previous sync state found, performing full sync")
        inventory_res = await load_inventory(
            scm,
            repo_name,
            owner,
            priority=priority,
            start_workflows=start_workflows,
            workflow_definition_id=workflow_definition_id,
            param_set_id=param_set_id,
            content_filter=content_filter,
        )

        latest_commit_sha = None

        if len(inventory_res["inventory"]) > 0:
            for i in inventory_res["inventory"]:
                if "metadata" in i:
                    meta = i.get("metadata")
                    if meta and "last_commit_sha" in meta:
                        latest_commit_sha = meta["last_commit_sha"]
                        break

        update_result = await client.update_sync_state(source, latest_commit_sha, branch=branch, metadata={})

        return inventory_res

    issues = []
    if content_filter in (ContentFilter.ALL, ContentFilter.ISSUES):
        issues = await get_issues(scm, repo_name, owner, since=sync_state.get("last_sync_date"))
        logger.info(f"found {len(issues)} issues to ingest")
    # Fetch commits since last sync
    logger.info(f"Last sync was at commit {last_commit_sha}")

    new_commits = []
    changed_files = set()
    removed_files = set()
    file_data = []

    if content_filter in (ContentFilter.ALL, ContentFilter.FILES):
        new_commits = await impl.list_commits_since(repo_name, owner, since_commit_sha=last_commit_sha, branch=branch)

        if not new_commits and not issues:
            logger.info("No new commits since last sync, repository is up to date")
            return {
                "status": "up-to-date",
                "commits_processed": 0,
                "files_changed": 0,
                "ingested": [],
                "errors": [],
            }

        logger.info(f"Found {len(new_commits)} new commits to process")

        # Extract changed file paths from commits
        for commit in new_commits:
            # Get detailed commit info with file list
            try:
                commit_detail = await impl.get_commit_details(repo_name, owner, commit["sha"])

                # Extract file changes (format varies by SCM, handle both)
                files_list = commit_detail.get("files", [])

                for file in files_list:
                    file_path = file.get("filename") or file.get("path") or file.get("name")
                    status = file.get("status", "")

                    if status in ("removed", "deleted"):
                        removed_files.add(file_path)
                        changed_files.discard(file_path)  # Don't fetch if removed
                    else:
                        if file_path:
                            changed_files.add(file_path)

            except Exception as e:
                logger.exception(f"Error processing commit {commit.get('sha')}", exc_info=e)
                continue

        logger.info(f"Files changed: {len(changed_files)}, removed: {len(removed_files)}")

        # Fetch only changed files
        allowed_extensions = settings.extensions

        for file_path in changed_files:
            # Check if extension is allowed
            ext = file_path.split(".")[-1] if "." in file_path else ""
            if ext not in allowed_extensions:
                logger.debug(f"Skipping {file_path} - extension '{ext}' not in allowed list")
                continue

            try:
                file = await impl.get_single_file(repo_name, owner, file_path, branch)
                file_data.append(file)
            except Exception as e:
                logger.exception(f"Failed to fetch {file_path}", exc_info=e)

        logger.info(f"Fetched {len(file_data)} changed files with allowed extensions")
    elif not issues:
        logger.info("No new issues since last sync, repository is up to date")
        return {
            "status": "up-to-date",
            "commits_processed": 0,
            "files_changed": 0,
            "ingested": [],
            "errors": [],
        }

    # Get or create batch
    found_batch_id = await client.find_batch_for_source(source)
    if found_batch_id:
        logger.info(f"Using existing batch {found_batch_id}")
        batch_id = found_batch_id
    else:
        logger.info("Creating new batch")
        batch_id = await client.create_batch(source, source)

    # Ingest changed files
    errors = []
    ingested = []

    file_data.extend(issues)

    for file in file_data:
        try:
            meta = file.get("metadata", {}).copy()
            # Clean metadata
            for k in ["path", "sha256", "size", "source", "batch_id", "source_uri"]:
                meta.pop(k, None)

            mime_type = file.get("content-type") or meta.get("content-type", "application/octet-stream")

            res = await client.do_ingest(file["file_bytes"], file["uri"], meta, source, batch_id, mime_type)

            if "error" in res:
                logger.error(f"Error ingesting {file['uri']}: {res['error']}")
                errors.append({"uri": file["uri"], "error": res["error"]})
            else:
                ingested.append(file["uri"])
                logger.info(f"Ingested {file['uri']}")

        except Exception as e:
            logger.exception(f"Failed to ingest {file.get('uri', 'unknown')}")
            errors.append({"uri": file.get("uri", "unknown"), "error": str(e)})

    # Start workflows if requested and no errors
    wf_res = None
    if len(errors) == 0 and start_workflows and len(ingested) > 0:
        logger.info("Starting workflows")
        wf_res = await client.do_start_workflows(batch_id, workflow_definition_id, param_set_id, priority)

    # Update sync state with latest commit
    latest_commit_sha = last_commit_sha
    if new_commits:
        latest_commit_sha = new_commits[0]["sha"]
    update_result = await client.update_sync_state(
        source,
        latest_commit_sha,
        branch=branch,
        metadata={
            "commits_processed": len(new_commits),
            "files_changed": len(changed_files),
            "files_removed": len(removed_files),
            "files_ingested": len(ingested),
        },
    )

    if "error" in update_result:
        logger.error(f"Error updating sync state: {update_result['error']}")
    else:
        logger.info(f"Incremental sync complete. Updated sync state to {latest_commit_sha}")

    return {
        "status": "synced",
        "commits_processed": len(new_commits),
        "files_changed": len(changed_files),
        "files_removed": len(removed_files),
        "ingested": ingested,
        "errors": errors,
        "workflow_result": wf_res,
        "new_commit_sha": latest_commit_sha,
    }
