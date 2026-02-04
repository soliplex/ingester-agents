"""
Script to set up a Gitea test repository with issues and files.

This script:
1. Creates a new repository in Gitea
2. Adds issues from issues.csv
3. Uploads all files from tests/test_fs to the repository

Usage:
    python setup_test_repo.py [--repo-name NAME] [--delete-existing]

Environment variables (or .env file):
    GITEA_URL: Gitea API URL (e.g., http://localhost:3000/api/v1)
    GITEA_TOKEN: API token for authentication
    GITEA_OWNER: Repository owner (default: admin)
"""

import asyncio
import csv
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

# Add parent directories to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from soliplex.agents.scm import SCMException
from soliplex.agents.scm.gitea import GiteaProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = typer.Typer(help="Set up a Gitea test repository with issues and files")


def get_script_dir() -> Path:
    """Get the directory containing this script."""
    return Path(__file__).parent


def get_test_fs_dir() -> Path:
    """Get the test_fs directory."""
    return Path(__file__).parent.parent / "test_fs"


def load_issues_from_csv(csv_path: Path, start_index: int | None = None, end_index: int | None = None) -> list[dict]:
    """
    Load issues from a CSV file.

    Args:
        csv_path: Path to the issues.csv file
        start_index: Index of the first issue to create
        end_index: Index of the last issue to create

    Returns:
        List of issue dictionaries with 'title' and 'body' keys
    """
    issues = []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        items = list(reader)
        if start_index is not None and end_index is not None:
            items = items[start_index:end_index]
        elif end_index is not None:
            items = items[:end_index]
        elif start_index is not None:
            items = items[start_index:]
        for i, row in enumerate(items):
            issues.append(
                {
                    "title": row.get("issue_title", f"Issue {i + 1}"),
                    "body": row.get("issue_body_md", ""),
                }
            )
    return issues


def collect_files(directory: Path) -> list[tuple[str, bytes]]:
    """
    Recursively collect all files from a directory.

    Args:
        directory: Directory to scan

    Returns:
        List of tuples (relative_path, file_bytes)
    """
    files = []
    for file_path in directory.rglob("*"):
        if file_path.is_file():
            relative_path = file_path.relative_to(directory)
            # Convert Windows paths to forward slashes for Git
            relative_path_str = str(relative_path).replace("\\", "/")
            try:
                file_bytes = file_path.read_bytes()
                files.append((relative_path_str, file_bytes))
                logger.debug(f"Collected file: {relative_path_str} ({len(file_bytes)} bytes)")
            except Exception as e:
                logger.warning(f"Failed to read file {file_path}: {e}")
    return files


async def setup_repository(
    repo_name: str,
    delete_existing: bool = False,
    start_index: int | None = None,
    end_index: int | None = None,
) -> None:
    """
    Set up a Gitea repository with issues and files.

    Args:
        repo_name: Name of the repository to create
        delete_existing: If True, delete existing repo before creating
        start_index: Index of the first issue to create
        end_index: Index of the last issue to create
    """
    provider = GiteaProvider()
    script_dir = get_script_dir()
    test_fs_dir = get_test_fs_dir()

    logger.info(f"Setting up repository: {repo_name}")
    logger.info(f"Script directory: {script_dir}")
    logger.info(f"Test files directory: {test_fs_dir}")

    # Delete existing repository if requested
    if delete_existing:
        try:
            await provider.delete_repository(repo_name)
            logger.info(f"Deleted existing repository: {repo_name}")
        except SCMException as e:
            if "not found" in str(e).lower():
                logger.info(f"Repository {repo_name} does not exist, proceeding with creation")
            else:
                raise

    # Create repository
    try:
        result = await provider.create_repository(
            name=repo_name,
            description="Test repository created by setup_test_repo.py",
            private=False,
        )
        logger.info(f"Created repository: {result.get('full_name', repo_name)}")
    except SCMException as e:
        if "already exists" in str(e).lower():
            logger.warning(f"Repository {repo_name} already exists. Use --delete-existing to recreate.")
            if not delete_existing:
                return
        else:
            raise

    # Add files from test_fs
    if test_fs_dir.exists():
        files = collect_files(test_fs_dir)
        logger.info(f"Found {len(files)} files to upload")

        for file_path, file_bytes in files:
            try:
                await provider.create_file(
                    repo=repo_name,
                    file_path=file_path,
                    content=file_bytes,
                    message=f"Add {file_path}",
                )
                logger.info(f"Uploaded file: {file_path}")
            except SCMException as e:
                logger.warning(f"Failed to upload {file_path}: {e}")
    else:
        logger.warning(f"Test files directory not found: {test_fs_dir}")

    # Add issues from CSV
    issues_csv = script_dir / "issues.csv"
    if issues_csv.exists():
        issues = load_issues_from_csv(issues_csv, start_index, end_index)
        logger.info(f"Loaded {len(issues)} issues from CSV")

        for i, issue in enumerate(issues, 1):
            try:
                await provider.create_issue(
                    repo=repo_name,
                    title=issue["title"],
                    body=issue["body"],
                )
                logger.info(f"Created issue {i}/{len(issues)}: {issue['title'][:50]}...")
            except SCMException as e:
                logger.warning(f"Failed to create issue '{issue['title'][:30]}...': {e}")
    else:
        logger.warning(f"Issues CSV not found: {issues_csv}")

    logger.info(f"Repository setup complete: {repo_name}")


async def delete_repository_async(repo_name: str) -> None:
    """Delete a repository."""
    provider = GiteaProvider()
    try:
        await provider.delete_repository(repo_name)
        logger.info(f"Deleted repository: {repo_name}")
    except SCMException as e:
        if "not found" in str(e).lower():
            logger.warning(f"Repository {repo_name} does not exist")
        else:
            raise


@app.command()
def setup(
    repo_name: Annotated[str, typer.Option("--repo-name", "-r", help="Name of the repository to create")] = "test-repo",
    max_issues: Annotated[
        int, typer.Option("--max-issues", "-n", help="Maximum number of issues to create (0 for all)")
    ] = 10,
    delete_existing: Annotated[
        bool, typer.Option("--delete-existing", "-d", help="Delete existing repository before creating")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose logging")] = False,
) -> None:
    """Set up a Gitea test repository with issues and files."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    end_index = max_issues if max_issues > 0 else None

    try:
        asyncio.run(
            setup_repository(
                repo_name=repo_name,
                delete_existing=delete_existing,
                end_index=end_index,
            )
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        raise typer.Exit(1) from None
    except Exception:
        logger.exception("Error setting up repository")
        raise typer.Exit(1) from None


@app.command()
def delete(
    repo_name: Annotated[str, typer.Option("--repo-name", "-r", help="Name of the repository to delete")] = "test-repo",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose logging")] = False,
) -> None:
    """Delete a Gitea test repository."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        asyncio.run(delete_repository_async(repo_name))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        raise typer.Exit(1) from None
    except Exception:
        logger.exception("Error deleting repository")
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
