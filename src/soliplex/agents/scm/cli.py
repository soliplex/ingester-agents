import asyncio
import json
import logging
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
