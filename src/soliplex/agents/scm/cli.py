import asyncio
import logging

import typer

from . import app as app
from .lib.config import SCM
from .lib.config import settings

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


@cli.command("load-inventory")
def load_inventory(scm: SCM, repo_name: str, owner: str = None, resume_batch: int | None = None):
    print(asyncio.run(app.load_inventory(scm, repo_name, owner, resume_batch=resume_batch)))


if __name__ == "__main__":
    cli()
