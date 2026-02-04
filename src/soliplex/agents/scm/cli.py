import asyncio
import json
import logging
from typing import Annotated

import typer

from ..config import SCM
from ..config import settings
from . import app as app

logger = logging.getLogger(__name__)


def init():
    logging.basicConfig(level=settings.log_level)


cli = typer.Typer(no_args_is_help=True)


@cli.command("list-issues")
def ingest_issues(scm: SCM, repo_name: str, owner: str = None):
    issues = asyncio.run(app.get_scm(scm).list_issues(repo_name, owner, add_comments=True))
    for issue in issues:
        print(issue["title"])
        print(issue["body"])


@cli.command("get-repo")
def get_repo(scm: SCM, repo_name: str, owner: str = None):
    print(asyncio.run(app.get_scm(scm).list_repo_files(repo_name, owner, settings.extensions)))


@cli.command("run-inventory")
def run_inventory(
    scm: Annotated[SCM, typer.Argument(help="scm provider")],
    repo_name: Annotated[str, typer.Argument(help="repo name")],
    owner: Annotated[str, typer.Argument(help="repo owner")],
    start_workflows: Annotated[bool, typer.Option(help="start workflows")] = False,
    workflow_definition_id: Annotated[str, typer.Option(help="workflow definition id")] = None,
    param_set_id: Annotated[str, typer.Option(help="param set id")] = None,
    priority: Annotated[int, typer.Option(help="workflow priority")] = 0,
    do_json: Annotated[bool, typer.Option(help="output json")] = False,
):
    if start_workflows:
        if workflow_definition_id is None:
            raise Exception("workflow_definition_id is required when start_workflows is true")  # noqa: TRY002
        if param_set_id is None:
            raise Exception("param_set_id is required when start_workflows is true")  # noqa: TRY002
    res = asyncio.run(
        app.load_inventory(
            scm,
            repo_name,
            owner,
            start_workflows=start_workflows,
            workflow_definition_id=workflow_definition_id,
            param_set_id=param_set_id,
            priority=priority,
        )
    )
    if do_json:
        print(json.dumps(res, indent=2))
    else:
        if "errors" in res and len(res["errors"]) > 0:
            print(f"found {len(res['errors'])} errors:")
            for err in res["errors"]:
                print(err)
        else:
            print("no errors found")
            print(f"found {len(res['inventory'])} files")
            print(f"found {len(res['to_process'])} to process")
            print(f"{len(res['ingested'])} ingested")
            if start_workflows:
                print("workflow result")
                print(json.dumps(res["workflow_result"], indent=2))


@cli.command("run-incremental")
def run_incremental(
    scm: Annotated[SCM, typer.Argument(help="scm provider")],
    repo_name: Annotated[str, typer.Argument(help="repo name")],
    owner: Annotated[str, typer.Argument(help="repo owner")],
    branch: Annotated[str, typer.Option(help="branch name")] = "main",
    start_workflows: Annotated[bool, typer.Option(help="start workflows")] = False,
    workflow_definition_id: Annotated[str, typer.Option(help="workflow definition id")] = None,
    param_set_id: Annotated[str, typer.Option(help="param set id")] = None,
    priority: Annotated[int, typer.Option(help="workflow priority")] = 0,
    do_json: Annotated[bool, typer.Option(help="output json")] = False,
):
    """
    Run incremental sync based on commit history.

    Only processes files that changed since last sync.
    Falls back to full sync if no sync state exists.

    Example:
        si-agent scm run-incremental gitea myrepo admin
    """
    init()
    res = asyncio.run(
        app.incremental_sync(
            scm,
            repo_name,
            owner,
            branch=branch,
            priority=priority,
            start_workflows=start_workflows,
            workflow_definition_id=workflow_definition_id,
            param_set_id=param_set_id,
        )
    )

    if do_json:
        print(json.dumps(res, indent=2))
    else:
        print(f"Status: {res.get('status', 'unknown')}")
        print(f"Commits processed: {res.get('commits_processed', 0)}")
        print(f"Files changed: {res.get('files_changed', 0)}")
        print(f"Files removed: {res.get('files_removed', 0)}")
        print(f"Files ingested: {len(res.get('ingested', []))}")

        if res.get("errors"):
            print(f"\nErrors: {len(res['errors'])}")
            for err in res["errors"]:
                print(f"  - {err.get('uri', 'unknown')}: {err.get('error', 'unknown error')}")

        if res.get("new_commit_sha"):
            print(f"\nSync state updated to: {res['new_commit_sha']}")

        if res.get("workflow_result"):
            print("\nWorkflow result:")
            print(json.dumps(res["workflow_result"], indent=2))


@cli.command("reset-sync")
def reset_sync(
    scm: Annotated[SCM, typer.Argument(help="scm provider")],
    repo_name: Annotated[str, typer.Argument(help="repo name")],
    owner: Annotated[str, typer.Argument(help="repo owner")],
):
    """
    Reset sync state for a repository.

    Next sync will be a full scan.

    Example:
        si-agent scm reset-sync gitea myrepo admin
    """
    init()
    from ... import client

    source = f"{scm.value}:{owner}:{repo_name}"

    res = asyncio.run(client.reset_sync_state(source))

    if "error" in res:
        print(f"Error: {res['error']}")
    else:
        print(res.get("message", f"Sync state reset for {source}"))


@cli.command("get-sync-state")
def get_sync_state(
    scm: Annotated[SCM, typer.Argument(help="scm provider")],
    repo_name: Annotated[str, typer.Argument(help="repo name")],
    owner: Annotated[str, typer.Argument(help="repo owner")],
):
    """
    Get current sync state for a repository.

    Example:
        si-agent scm get-sync-state gitea myrepo admin
    """
    init()
    from ... import client

    source = f"{scm.value}:{owner}:{repo_name}"

    res = asyncio.run(client.get_sync_state(source))

    if "error" in res:
        print(f"Error: {res['error']}")
    else:
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    cli()
