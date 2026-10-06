import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated

import typer

from .. import local_state
from ..config import SCM
from ..config import ContentFilter
from ..config import settings
from . import app as app

logger = logging.getLogger(__name__)


def parse_repo(repo: str) -> tuple[str, str]:
    """
    Parse owner/repo notation into (owner, repo_name).

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        Tuple of (owner, repo_name)

    Raises:
        typer.BadParameter: If repo is not in "owner/repo" format
    """
    if "/" not in repo:
        raise typer.BadParameter(f"Repository must be in 'owner/repo' format, got '{repo}'")
    owner, _, repo_name = repo.partition("/")
    if not owner or not repo_name:
        raise typer.BadParameter(f"Repository must be in 'owner/repo' format, got '{repo}'")
    return owner, repo_name


cli = typer.Typer(no_args_is_help=True)


@cli.command("list-issues")
def ingest_issues(
    scm: Annotated[SCM, typer.Argument(help="scm provider")],
    repo: Annotated[str, typer.Argument(help="repository in owner/repo format")],
):
    """
    List issues from a repository.

    Example:
        si-agent scm list-issues gitea admin/myrepo
        si-agent scm list-issues github myorg/myrepo
    """
    owner, repo_name = parse_repo(repo)
    issues = asyncio.run(app.get_scm(scm).list_issues(repo_name, owner, add_comments=True))
    for issue in issues:
        print(issue["title"])
        print(issue["body"])


@cli.command("get-repo")
def get_repo(
    scm: Annotated[SCM, typer.Argument(help="scm provider")],
    repo: Annotated[str, typer.Argument(help="repository in owner/repo format")],
):
    """
    Get repository files.

    Example:
        si-agent scm get-repo gitea admin/myrepo
        si-agent scm get-repo github myorg/myrepo
    """
    owner, repo_name = parse_repo(repo)
    print(asyncio.run(app.get_scm(scm).list_repo_files(repo_name, owner, settings.extensions)))


@cli.command("reset-sync")
def reset_sync(
    scm: Annotated[SCM, typer.Argument(help="scm provider")],
    repo: Annotated[str, typer.Argument(help="repository in owner/repo format")],
    content_filter: Annotated[ContentFilter, typer.Option(help="filter content: all, files, issues")] = ContentFilter.ALL,
):
    """
    Reset sync state for a repository.

    Next sync will be a full scan.

    Example:
        si-agent scm reset-sync gitea admin/myrepo
        si-agent scm reset-sync github myorg/myrepo
    """
    owner, repo_name = parse_repo(repo)
    source = f"{scm.value}:{owner}:{repo_name}:{content_filter.value}"

    if local_state.reset_state(source):
        print(f"Sync state reset for {source}")
    else:
        print(f"No sync state found for {source}")


@cli.command("reset-clone")
def reset_clone(
    repo: Annotated[str, typer.Argument(help="repository in owner/repo format")],
    branch: Annotated[str, typer.Option(help="branch whose checkout to delete")] = "main",
    source: Annotated[str | None, typer.Option(help="source (the manifest's `source`) whose sync cursor to clear")] = None,
):
    """
    Delete a git CLI checkout so the next run clones it afresh.

    The recovery for a checkout that can no longer be updated in place (a
    force-pushed branch, a corrupted directory): a failed pull is reported as
    a component error and the checkout is left alone for inspection. With
    --source, that source's sync cursor is cleared as well, so its next run
    is a full sync.

    Example:
        si-agent scm reset-clone admin/myrepo --branch main --source my-repo
    """
    from .git_cli import GitCliWrapper

    owner, repo_name = parse_repo(repo)
    git = GitCliWrapper(base_dir=Path(settings.scm_git_repo_base_dir) if settings.scm_git_repo_base_dir else None)
    repo_dir = git.get_repo_dir(owner, repo_name, branch)
    if asyncio.run(git.delete_repo(owner, repo_name, branch)):
        print(f"Deleted checkout {repo_dir}")
    else:
        print(f"No checkout at {repo_dir}")

    if source is None:
        return
    if not local_state.get_state_path(source).exists():
        print(f"No sync state found for {source}")
        return
    local_state.clear_sync_cursor(source)
    print(f"Sync cursor cleared for {source}; its next run is a full sync")


@cli.command("get-sync-state")
def get_sync_state(
    scm: Annotated[SCM, typer.Argument(help="scm provider")],
    repo: Annotated[str, typer.Argument(help="repository in owner/repo format")],
    content_filter: Annotated[ContentFilter, typer.Option(help="filter content: all, files, issues")] = ContentFilter.ALL,
):
    """
    Get current sync state for a repository.

    Example:
        si-agent scm get-sync-state gitea admin/myrepo
        si-agent scm get-sync-state github myorg/myrepo
    """
    owner, repo_name = parse_repo(repo)
    source = f"{scm.value}:{owner}:{repo_name}:{content_filter.value}"

    res = local_state.get_sync_meta(source)
    print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    cli()
