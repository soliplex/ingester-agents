import datetime
import hashlib
import logging

from soliplex.agents.common import mime
from soliplex.agents.scm import CursorNotFound
from soliplex.agents.scm import SCMListingError
from soliplex.agents.scm.base import BaseSCMProvider
from soliplex.agents.scm.base import passes_extension_prefilter

from .. import local_state
from .. import local_store
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


# Keys that are recorded separately (mime_type, hash, source, source_url) and
# should not be duplicated into the sidecar's ``metadata`` block.
_STRIP_KEYS = ("path", "sha256", "size", "source", "batch_id", "source_uri", "content-type", "html_url")


def _doc_meta(row: dict, extra_metadata: dict[str, str] | None) -> dict:
    """Build the sidecar metadata for an inventory row."""
    meta = dict(row.get("metadata") or {})
    for k in _STRIP_KEYS:
        meta.pop(k, None)
    meta = clean_meta(meta)
    if extra_metadata:
        meta.update(extra_metadata)
    return meta


def _source_url(row: dict) -> str | None:
    """The browsable URL for an inventory row, if the provider gave one.

    Reads both row shapes: ``get_data`` nests provider fields under
    ``metadata``, while ``incremental_sync`` passes ``parse_file_rec`` output
    through flat. Keeping that in one place is deliberate -- the two shapes
    diverging is how the URL came to be dropped on both paths.

    Prefers ``html_url`` (what a person can open) over ``url`` (the contents
    API endpoint), and returns None when neither is present.
    """
    meta = row.get("metadata") or {}
    return row.get("html_url") or meta.get("html_url") or row.get("url") or meta.get("url")


def _resolve_mime(row: dict) -> str:
    """Resolve the MIME type for a row from its metadata, content, or URI."""
    ct = row.get("content-type") or (row.get("metadata") or {}).get("content-type")
    if ct:
        return ct
    return mime.detect_mime_type(
        row.get("uri") or row.get("path"),
        data=row.get("file_bytes"),
        text_fallback=True,
    )


async def resolve_branch(impl: BaseSCMProvider, repo_name: str, owner: str | None, branch: str | None) -> str:
    """*branch*, or the repository's default branch when it is ``None``."""
    if branch:
        return branch
    branch = await impl.get_default_branch(repo_name, owner)
    logger.info(f"Using default branch '{branch}' of {owner}/{repo_name}")
    return branch


def issue_uri(owner: str | None, repo_name: str, number: int | str) -> str:
    """The document URI of an issue."""
    return f"/{owner}/{repo_name}/issues/{number}"


async def confirm_issue_removals(
    impl: BaseSCMProvider,
    source: str,
    owner: str | None,
    repo_name: str,
    listed_uris: set[str],
) -> list[dict]:
    """Check with the API that every issue about to drop out of *source* is gone.

    Zero issues can be a true answer, so an empty issue list cannot be refused
    by size. Instead, each issue document of this repository that is stored
    for *source* but missing from *listed_uris* is looked up on its own: a 404
    (deleted) or a closed issue confirms the removal; an issue that is still
    open means the list was wrong. The repository is checked first, because a
    provider answers 404 for a private repository it will not show, which
    would otherwise "confirm" every removal.

    Costs one request per removed issue (plus one for the repository), and
    nothing when no issue dropped out.

    Returns:
        Error rows (``stage: "issues"``); empty when every removal is
        confirmed. Any row means the issue list is not to be reconciled
        against, so callers record them where they block the clean-up.
    """
    prefix = issue_uri(owner, repo_name, "")
    dropped = sorted(
        uri
        for uri in local_state.load_file_state(source)
        if uri.startswith(prefix) and uri[len(prefix) :].isdigit() and uri not in listed_uris
    )
    if not dropped:
        return []

    logger.info(f"Confirming removal of {len(dropped)} issues missing from the {owner}/{repo_name} issue list")
    try:
        await impl.get_repo(repo_name, owner)
    except Exception as e:
        logger.exception("Cannot check %s/%s before confirming issue removals", owner, repo_name)
        return [
            {
                "uri": f"/{owner}/{repo_name}",
                "error": f"repository check failed, so {len(dropped)} issue removals are unconfirmed: {e}",
                "stage": "issues",
            }
        ]

    errors = []
    for uri in dropped:
        number = int(uri[len(prefix) :])
        try:
            state = await impl.get_issue_state(repo_name, owner, number)
        except Exception as e:
            logger.exception("Cannot confirm removal of %s", uri)
            errors.append({"uri": uri, "error": f"removal unconfirmed: {e}", "stage": "issues"})
            continue
        if state == "open":
            logger.error("Issue %s is open but missing from the issue list; not removing anything", uri)
            errors.append({"uri": uri, "error": "issue is open but missing from the issue list", "stage": "issues"})
        else:
            logger.info(f"Issue {uri} is {state}; removal confirmed")
    return errors


async def load_inventory(
    scm: str,
    repo_name: str,
    owner: str = None,
    content_filter: ContentFilter = ContentFilter.ALL,
    extra_metadata: dict[str, str] | None = None,
    source: str | None = None,
    delete_stale: bool = False,
    branch: str | None = "main",
    check_issue_removals: bool = True,
):
    """Fetch a repository's full inventory and write changed documents locally.

    Files (and/or issues) are written under the configured ``download_dir``;
    local state tracks content hashes so unchanged documents are skipped on
    subsequent runs. When ``delete_stale`` is set, documents no longer present
    in the source are removed from disk.

    Files come from *branch* (``None``: the repository's default branch).
    A file the provider listed but could not read lands in ``errors``
    (``stage: "fetch"``), as does an issue removal the API would not confirm
    (``stage: "issues"``); either holds back every clean-up, while every
    readable document is still written. ``check_issue_removals=False`` skips
    that confirmation, for a caller that will never remove anything (the
    manifest runner without ``delete_stale``): it costs a request per stored
    issue missing from the list, on every run, for as long as it stays stored.
    """
    source = source or f"{scm.value}:{owner}:{repo_name}:{content_filter.value}"
    data, fetch_errors = await get_data(scm, repo_name, owner, content_filter=content_filter, branch=branch)

    to_process = local_state.compute_to_process(data, source)
    ingested = []
    errors = [{"uri": e["uri"], "error": e["error"], "stage": "fetch"} for e in fetch_errors]
    if (check_issue_removals or delete_stale) and content_filter in (ContentFilter.ALL, ContentFilter.ISSUES):
        errors.extend(await confirm_issue_removals(get_scm(scm), source, owner, repo_name, {r["uri"] for r in data}))
    ret = {
        "inventory": data,
        "to_process": to_process,
        "ingested": ingested,
        "errors": errors,
    }
    logger.info(f"found {len(to_process)} to process")

    for row in to_process:
        uri = row["uri"]
        try:
            mime_type = _resolve_mime(row)
            meta = _doc_meta(row, extra_metadata)
            doc_bytes = row["file_bytes"]
            logger.info(f"writing {uri}")
            await local_store.write_document(
                source, uri, doc_bytes, mime_type, meta, ingestion_type="scm", source_url=_source_url(row)
            )
            local_state.upsert_file(source, uri, row.get("sha256"), size=len(doc_bytes), mime_type=mime_type)
            ingested.append(uri)
        except Exception as e:
            logger.exception("Failed to write %s", uri)
            errors.append({"uri": uri, "error": str(e)})

    delete_stale_result = None
    if delete_stale and len(errors) == 0:
        delete_stale_result = await local_state.prune_documents(source, {r["uri"] for r in data})
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
            "uri": issue_uri(owner, repo_name, issue["number"]),
            "title": issue["title"],
            "html_url": issue.get("html_url"),
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
    branch: str | None = "main",
    content_filter: ContentFilter = ContentFilter.ALL,
    source: str | None = None,
) -> list[dict[str, str]]:
    """Return all URI/sha256 pairs for a repo without downloading content.

    Used by the manifest runner when delete_stale is enabled with
    incremental SCM components to get the full URI set.

    Raises:
        SCMListingError: If any listed file could not be read, or an issue
            removal could not be confirmed: the listing is then incomplete,
            and reconciling against it would delete what it left out.
    """
    impl = get_scm(scm)
    source = source or f"{scm.value}:{owner}:{repo_name}:{content_filter.value}"
    items: list[dict[str, str]] = []

    if content_filter in (ContentFilter.ALL, ContentFilter.FILES):
        allowed_extensions = settings.extensions
        files = await impl.list_repo_files(
            repo_name,
            owner,
            allowed_extensions=allowed_extensions,
            branch=await resolve_branch(impl, repo_name, owner, branch),
        )
        files, unreadable = _split_error_rows(files)
        if unreadable:
            raise SCMListingError(
                f"{len(unreadable)} files in {owner}/{repo_name} could not be read, e.g. "
                f"{unreadable[0]['uri']}: {unreadable[0]['error']}"
            )
        for f in files:
            if mime.extension_allowed(f.get("content-type"), allowed_extensions):
                items.append({"uri": f["uri"], "sha256": f.get("sha256", "")})

    if content_filter in (ContentFilter.ALL, ContentFilter.ISSUES):
        issues = await impl.list_issues(
            repo=repo_name,
            owner=owner,
            add_comments=False,
        )
        issue_items = [{"uri": issue_uri(owner, repo_name, issue["number"]), "sha256": ""} for issue in issues]
        unconfirmed = await confirm_issue_removals(impl, source, owner, repo_name, {i["uri"] for i in issue_items})
        if unconfirmed:
            raise SCMListingError(
                f"{len(unconfirmed)} issue removals in {owner}/{repo_name} are unconfirmed, e.g. "
                f"{unconfirmed[0]['uri']}: {unconfirmed[0]['error']}"
            )
        items.extend(issue_items)

    return items


def _split_error_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split a provider listing into readable files and error rows."""
    files = [r for r in rows if "error" not in r]
    errors = [r for r in rows if "error" in r]
    return files, errors


async def get_data(
    scm: str,
    repo_name: str,
    owner: str = None,
    content_filter: ContentFilter = ContentFilter.ALL,
    branch: str | None = "main",
) -> tuple[list[dict], list[dict]]:
    """List a repository's documents, files from *branch*.

    Returns:
        ``(rows, errors)``: the documents, and an error row (``uri``,
        ``error``) for each file the provider listed but could not read.
    """
    doc_data = []
    errors = []

    if content_filter in (ContentFilter.ALL, ContentFilter.FILES):
        impl = get_scm(scm)
        allowed_extensions = settings.extensions
        branch = await resolve_branch(impl, repo_name, owner, branch)
        files = await impl.list_repo_files(repo_name, owner, allowed_extensions=allowed_extensions, branch=branch)
        files, errors = _split_error_rows(files)
        for e in errors:
            logger.error("Could not read %s: %s", e["uri"], e["error"])
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
        filtered_files = [x for x in files if mime.extension_allowed(x.get("content-type"), allowed_extensions)]
        for f in filtered_files:
            row = {
                "file_bytes": f["file_bytes"],
                "uri": f["uri"],
                "sha256": f["sha256"],
                # Top level, not in metadata: it becomes the sidecar's own
                # source_url rather than operator-supplied metadata.
                "html_url": f.get("html_url"),
                "metadata": {
                    "last_modified_date": f["last_updated"],
                    "content-type": f["content-type"],
                    "last_commit_sha": f["last_commit_sha"],
                },
            }
            doc_data.append(row)

    if content_filter in (ContentFilter.ALL, ContentFilter.ISSUES):
        doc_data.extend(await get_issues(scm, repo_name, owner))

    return doc_data, errors


def _uri_hashes(rows: list[dict]) -> list[dict[str, str]]:
    """``{"uri", "sha256"}`` pairs for a full listing, without the content."""
    return [{"uri": r["uri"], "sha256": r.get("sha256") or ""} for r in rows]


async def incremental_sync(
    scm: str,
    repo_name: str,
    owner: str = None,
    branch: str | None = "main",
    content_filter: ContentFilter = ContentFilter.ALL,
    extra_metadata: dict[str, str] | None = None,
    source: str | None = None,
    delete_stale: bool = False,
    check_issue_removals: bool = True,
):
    """
    Perform incremental sync based on commit history.

    Only fetches and writes files that changed since the last sync. Falls
    back to a full sync when there is no usable cursor: none stored, one
    stored for another branch, or one the branch's history no longer holds
    (a force-push, a fresh clone). Sync state (commit sha, branch,
    timestamp) is tracked locally.

    Args:
        scm: SCM type (gitea/github)
        repo_name: Repository name
        owner: Repository owner
        branch: Branch to sync (``None``: the repository's default branch)
        content_filter: Whether to sync files, issues, or both
        extra_metadata: Extra metadata attached to every document
        source: Optional source name override (used by manifests)
        delete_stale: Remove documents not in full inventory (default: False)
        check_issue_removals: Confirm, issue by issue, that every stored issue
            missing from the issue list is gone (see :func:`load_inventory`)

    Returns:
        Sync result dict with statistics
    """
    impl = get_scm(scm)
    source = source or f"{scm.value}:{owner}:{repo_name}:{content_filter.value}"
    if content_filter in (ContentFilter.ALL, ContentFilter.FILES):
        branch = await resolve_branch(impl, repo_name, owner, branch)

    logger.info(f"Starting incremental sync for {source}")

    # Get last sync state (local)
    sync_state = local_state.get_sync_meta(source)
    last_commit_sha = sync_state.get("last_commit_sha")

    # A cursor recorded on another branch says nothing about this one.
    if last_commit_sha and sync_state.get("branch") != branch:
        logger.warning(
            "Sync cursor %s was recorded on branch '%s', not '%s'; running a full sync",
            last_commit_sha,
            sync_state.get("branch"),
            branch,
        )
        local_state.clear_sync_cursor(source)
        last_commit_sha = None

    # Commits are listed before anything is written, so a cursor the branch's
    # history no longer holds turns into a full sync rather than "no commits".
    new_commits = []
    if last_commit_sha and content_filter in (ContentFilter.ALL, ContentFilter.FILES):
        try:
            new_commits = await impl.list_commits_since(repo_name, owner, since_commit_sha=last_commit_sha, branch=branch)
        except CursorNotFound:
            logger.warning("Sync cursor %s not in the history of '%s'; running a full sync", last_commit_sha, branch)
            local_state.clear_sync_cursor(source)
            last_commit_sha = None

    if not last_commit_sha:
        logger.info("No usable sync cursor, performing full sync")
        inventory_res = await load_inventory(
            scm,
            repo_name,
            owner,
            content_filter=content_filter,
            extra_metadata=extra_metadata,
            source=source,
            delete_stale=delete_stale,
            branch=branch,
            check_issue_removals=check_issue_removals,
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

        # A full listing: the runner reconciles against it directly.
        inventory_res["full_inventory"] = _uri_hashes(inventory_res["inventory"])
        return inventory_res

    # Every failure lands here -- fetching a commit's file list, fetching a
    # file, writing one, or an issue removal the API would not confirm -- so
    # a single check both holds the sync cursor and tells the manifest runner
    # the component was not clean.
    errors = []

    issues = []
    full_inventory = None
    if content_filter in (ContentFilter.ALL, ContentFilter.ISSUES):
        issues = await get_issues(scm, repo_name, owner, since=sync_state.get("last_sync_date"))
        logger.info(f"found {len(issues)} issues to ingest")

        # An issues-only source has no commits to diff, so it lists every
        # issue and hands the list to the runner as its full inventory: the
        # runner's gated clean-up removes deleted/closed issues, like every
        # other component's stale documents, and only once each removal is
        # confirmed. (With files mixed in, the runner's full listing does it.)
        if content_filter == ContentFilter.ISSUES:
            all_issues = await impl.list_issues(repo=repo_name, owner=owner, add_comments=False)
            full_inventory = [{"uri": issue_uri(owner, repo_name, i["number"]), "sha256": ""} for i in all_issues]
            logger.info(f"Listed {len(full_inventory)} issues for the issue inventory")
            if check_issue_removals or delete_stale:
                errors.extend(
                    await confirm_issue_removals(impl, source, owner, repo_name, {i["uri"] for i in full_inventory})
                )
    # Fetch commits since last sync
    logger.info(f"Last sync was at commit {last_commit_sha}")

    changed_files = set()
    removed_files = set()
    file_data = []

    if content_filter in (ContentFilter.ALL, ContentFilter.FILES):
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
                commit_detail = await impl.get_commit_details(repo_name, owner, commit["sha"], branch=branch)

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
                # Its changed files are unknown, so the cursor must not move
                # past it: record the failure rather than skipping the commit.
                logger.exception("Error processing commit %s", commit.get("sha"))
                errors.append({"uri": f"commit:{commit.get('sha')}", "error": str(e), "stage": "commit"})
                continue

        logger.info(f"Files changed: {len(changed_files)}, removed: {len(removed_files)}")

        # Delete removed files locally. Pass the stored mime_type so the
        # synthesized-extension path round-trips (delete_document recomputes
        # the relpath from the URI + mime_type).
        removed_state = local_state.load_file_state(source)
        for removed_path in removed_files:
            logger.info(f"Deleting removed file: {removed_path}")
            removed_mime = removed_state.get(removed_path, {}).get("mime_type")
            await local_store.delete_document(source, removed_path, mime_type=removed_mime)
            local_state.delete_file(source, removed_path)

        # Fetch changed files. Use a coarse extension pre-filter (allowed
        # extension or none); the authoritative filter runs after fetch
        # against the content-detected MIME type.
        allowed_extensions = settings.extensions

        for file_path in changed_files:
            if not passes_extension_prefilter(file_path, allowed_extensions):
                logger.debug(f"Skipping {file_path} - extension not in allowed list")
                continue

            try:
                file = await impl.get_single_file(repo_name, owner, file_path, branch)
                file_data.append(file)
            except Exception as e:
                logger.exception("Failed to fetch %s", file_path)
                errors.append({"uri": file_path, "error": str(e), "stage": "fetch"})

        # Drop fetched files whose detected content type isn't allowed.
        file_data = [f for f in file_data if mime.extension_allowed(_resolve_mime(f), allowed_extensions)]

        logger.info(f"Fetched {len(file_data)} changed files with allowed extensions")
    elif not issues:
        logger.info("No new issues since last sync, repository is up to date")
        return {
            "status": "up-to-date",
            "commits_processed": 0,
            "files_changed": 0,
            "ingested": [],
            "errors": errors,
            "full_inventory": full_inventory,
        }

    # Write changed files and issues locally
    ingested = []

    file_data.extend(issues)

    for file in file_data:
        uri = file["uri"]
        try:
            mime_type = _resolve_mime(file)
            meta = _doc_meta(file, extra_metadata)
            doc_bytes = file["file_bytes"]
            await local_store.write_document(
                source, uri, doc_bytes, mime_type, meta, ingestion_type="scm", source_url=_source_url(file)
            )
            local_state.upsert_file(source, uri, file.get("sha256"), size=len(doc_bytes), mime_type=mime_type)
            ingested.append(uri)
            logger.info(f"wrote {uri}")
        except Exception as e:
            logger.exception("Failed to write %s", uri)
            errors.append({"uri": uri, "error": str(e)})

    # Update sync state with latest commit only if nothing failed
    latest_commit_sha = last_commit_sha
    if new_commits and not errors:
        latest_commit_sha = new_commits[0]["sha"]
    elif errors:
        logger.warning(
            "Not advancing sync state past %s due to %d errors",
            last_commit_sha,
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
        all_uris = full_inventory
        if all_uris is None:
            all_uris = await list_all_uris(
                scm,
                repo_name,
                owner=owner,
                branch=branch,
                content_filter=content_filter,
                source=source,
            )
        delete_stale_result = await local_state.prune_documents(source, {u["uri"] for u in all_uris})

    ret = {
        "status": "synced",
        "commits_processed": len(new_commits),
        "files_changed": len(changed_files),
        "files_removed": len(removed_files),
        "ingested": ingested,
        "errors": errors,
        "new_commit_sha": latest_commit_sha,
        "delete_stale_result": delete_stale_result,
    }
    if full_inventory is not None:
        ret["full_inventory"] = full_inventory
    return ret
