import datetime
import hashlib
import logging

from soliplex.agents.common.config import detect_mime_type
from soliplex.agents.scm.base import BaseSCMProvider

from .. import local_state
from .. import local_store
from ..common import processors
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


def clean_meta(meta: dict):
    meta = meta.copy()
    for k, v in list(meta.items()):
        if v is None:
            del meta[k]
        elif isinstance(v, datetime.datetime):
            meta[k] = v.isoformat()
    return meta


# Keys that are recorded separately (mime_type, hash, source) and should not
# be duplicated into the sidecar's ``metadata`` block.
_STRIP_KEYS = ("path", "sha256", "size", "source", "batch_id", "source_uri", "content-type")


def _doc_meta(row: dict, extra_metadata: dict[str, str] | None) -> dict:
    """Build the sidecar metadata for an inventory row."""
    meta = dict(row.get("metadata") or {})
    for k in _STRIP_KEYS:
        meta.pop(k, None)
    meta = clean_meta(meta)
    if extra_metadata:
        meta.update(extra_metadata)
    return meta


def _resolve_mime(row: dict) -> str:
    """Resolve the MIME type for a row from its metadata or its URI."""
    ct = row.get("content-type") or (row.get("metadata") or {}).get("content-type")
    if ct:
        return ct
    return detect_mime_type(row.get("uri") or row.get("path"))


async def load_inventory(
    scm: str,
    repo_name: str,
    owner: str = None,
    content_filter: ContentFilter = ContentFilter.ALL,
    extra_metadata: dict[str, str] | None = None,
    source: str | None = None,
    delete_stale: bool = False,
):
    """Fetch a repository's full inventory and write changed documents locally.

    Files (and/or issues) are written under the configured ``download_dir``;
    local state tracks content hashes so unchanged documents are skipped on
    subsequent runs. When ``delete_stale`` is set, documents no longer present
    in the source are removed from disk.
    """
    data = await get_data(scm, repo_name, owner, content_filter=content_filter)

    source = source or f"{scm.value}:{owner}:{repo_name}:{content_filter.value}"

    to_process = local_state.compute_to_process(data, source)
    ingested = []
    errors = []
    ret = {"inventory": data, "to_process": to_process, "ingested": ingested, "errors": errors}
    logger.info(f"found {len(to_process)} to process")

    for row in to_process:
        uri = row["uri"]
        try:
            mime_type = _resolve_mime(row)
            meta = _doc_meta(row, extra_metadata)
            doc_bytes = row["file_bytes"]
            logger.info(f"writing {uri}")
            target = local_store.write_document(source, uri, doc_bytes, mime_type, meta)
            processors.run_processors(target, mime_type)
            local_state.upsert_file(source, uri, row.get("sha256"), size=len(doc_bytes), mime_type=mime_type)
            ingested.append(uri)
        except Exception as e:
            logger.exception("Failed to write %s", uri)
            errors.append({"uri": uri, "error": str(e)})

    delete_stale_result = None
    if delete_stale and len(errors) == 0:
        delete_stale_result = local_state.prune_documents(source, {r["uri"] for r in data})
    ret["delete_stale_result"] = delete_stale_result
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


async def list_all_uris(
    scm: str,
    repo_name: str,
    owner: str = None,
    branch: str = "main",
    content_filter: ContentFilter = ContentFilter.ALL,
) -> list[dict[str, str]]:
    """Return all URI/sha256 pairs for a repo without downloading content.

    Used by the manifest runner when delete_stale is enabled with
    incremental SCM components to get the full URI set.
    """
    impl = get_scm(scm)
    items: list[dict[str, str]] = []

    if content_filter in (ContentFilter.ALL, ContentFilter.FILES):
        allowed_extensions = settings.extensions
        files = await impl.list_repo_files(
            repo_name,
            owner,
            allowed_extensions=allowed_extensions,
            branch=branch,
        )
        for f in files:
            if f["name"].split(".")[-1] in allowed_extensions:
                items.append({"uri": f["uri"], "sha256": f.get("sha256", "")})

    if content_filter in (ContentFilter.ALL, ContentFilter.ISSUES):
        issues = await impl.list_issues(
            repo=repo_name,
            owner=owner,
            add_comments=False,
        )
        for issue in issues:
            items.append(
                {
                    "uri": f"/{owner}/{repo_name}/issues/{issue['number']}",
                    "sha256": "",
                }
            )

    return items


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
    content_filter: ContentFilter = ContentFilter.ALL,
    extra_metadata: dict[str, str] | None = None,
    source: str | None = None,
    delete_stale: bool = False,
):
    """
    Perform incremental sync based on commit history.

    Only fetches and writes files that changed since the last sync. Falls
    back to a full sync if no local sync state exists. Sync state (commit
    sha, branch, timestamp) is tracked locally.

    Args:
        scm: SCM type (gitea/github)
        repo_name: Repository name
        owner: Repository owner
        branch: Branch to sync
        content_filter: Whether to sync files, issues, or both
        extra_metadata: Extra metadata attached to every document
        source: Optional source name override (used by manifests)
        delete_stale: Remove documents not in full inventory (default: False)

    Returns:
        Sync result dict with statistics
    """
    impl = get_scm(scm)
    source = source or f"{scm.value}:{owner}:{repo_name}:{content_filter.value}"

    logger.info(f"Starting incremental sync for {source}")

    # Get last sync state (local)
    sync_state = local_state.get_sync_meta(source)
    last_commit_sha = sync_state.get("last_commit_sha")

    if not last_commit_sha:
        logger.info("No previous sync state found, performing full sync")
        inventory_res = await load_inventory(
            scm,
            repo_name,
            owner,
            content_filter=content_filter,
            extra_metadata=extra_metadata,
            source=source,
            delete_stale=delete_stale,
        )

        latest_commit_sha = None
        for i in inventory_res["inventory"]:
            meta = i.get("metadata")
            if meta and meta.get("last_commit_sha"):
                latest_commit_sha = meta["last_commit_sha"]
                break

        local_state.set_sync_meta(
            source,
            latest_commit_sha,
            branch=branch,
            last_sync_date=datetime.datetime.now(datetime.UTC),
            metadata={},
        )

        return inventory_res

    issues = []
    if content_filter in (ContentFilter.ALL, ContentFilter.ISSUES):
        issues = await get_issues(scm, repo_name, owner, since=sync_state.get("last_sync_date"))
        logger.info(f"found {len(issues)} issues to ingest")

        # Reconcile full issue inventory to remove deleted/closed issues
        # Only safe when source contains only issues (not mixed with files)
        if content_filter == ContentFilter.ISSUES:
            all_issues = await get_issues(scm, repo_name, owner)
            logger.info(f"Reconciling issue inventory ({len(all_issues)} total issues)")
            local_state.prune_documents(source, {i["uri"] for i in all_issues})
    # Fetch commits since last sync
    logger.info(f"Last sync was at commit {last_commit_sha}")

    new_commits = []
    changed_files = set()
    removed_files = set()
    file_data = []
    fetch_errors = False

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

        # Delete removed files locally
        for removed_path in removed_files:
            logger.info(f"Deleting removed file: {removed_path}")
            local_store.delete_document(source, removed_path)
            local_state.delete_file(source, removed_path)

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
                fetch_errors = True
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

    # Write changed files and issues locally
    errors = []
    ingested = []

    file_data.extend(issues)

    for file in file_data:
        uri = file["uri"]
        try:
            mime_type = _resolve_mime(file)
            meta = _doc_meta(file, extra_metadata)
            doc_bytes = file["file_bytes"]
            target = local_store.write_document(source, uri, doc_bytes, mime_type, meta)
            processors.run_processors(target, mime_type)
            local_state.upsert_file(source, uri, file.get("sha256"), size=len(doc_bytes), mime_type=mime_type)
            ingested.append(uri)
            logger.info(f"wrote {uri}")
        except Exception as e:
            logger.exception(f"Failed to write {file.get('uri', 'unknown')}")
            errors.append({"uri": uri, "error": str(e)})

    # Update sync state with latest commit only if no fetch/ingest errors
    latest_commit_sha = last_commit_sha
    has_sync_errors = fetch_errors or len(errors) > 0
    if new_commits and not has_sync_errors:
        latest_commit_sha = new_commits[0]["sha"]
    elif has_sync_errors:
        logger.warning(
            "Not advancing sync state past %s due to errors (fetch_errors=%s, ingest_errors=%d)",
            last_commit_sha,
            fetch_errors,
            len(errors),
        )
    local_state.set_sync_meta(
        source,
        latest_commit_sha,
        branch=branch,
        last_sync_date=datetime.datetime.now(datetime.UTC),
        metadata={
            "commits_processed": len(new_commits),
            "files_changed": len(changed_files),
            "files_removed": len(removed_files),
            "files_ingested": len(ingested),
        },
    )
    logger.info(f"Incremental sync complete. Updated sync state to {latest_commit_sha}")

    # Delete stale documents using full URI listing
    delete_stale_result = None
    if delete_stale and len(errors) == 0:
        all_uris = await list_all_uris(
            scm,
            repo_name,
            owner=owner,
            branch=branch,
            content_filter=content_filter,
        )
        delete_stale_result = local_state.prune_documents(source, {u["uri"] for u in all_uris})

    return {
        "status": "synced",
        "commits_processed": len(new_commits),
        "files_changed": len(changed_files),
        "files_removed": len(removed_files),
        "ingested": ingested,
        "errors": errors,
        "new_commit_sha": latest_commit_sha,
        "delete_stale_result": delete_stale_result,
    }
