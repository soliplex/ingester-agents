"""Git CLI wrapper and decorator for SCM providers."""

import asyncio
import logging
import mimetypes
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import aiofiles

from soliplex.agents.config import settings
from soliplex.agents.scm import AuthenticationConfigError
from soliplex.agents.scm import SCMException
from soliplex.agents.scm.base import BaseSCMProvider
from soliplex.agents.scm.lib.utils import compute_file_hash

logger = logging.getLogger(__name__)


# Exceptions
class GitCliError(SCMException):
    """Base exception for git CLI operations."""

    pass


class GitCloneError(GitCliError):
    """Raised when git clone fails."""

    pass


class GitPullError(GitCliError):
    """Raised when git pull fails."""

    pass


class GitCleanError(GitCliError):
    """Raised when git clean fails."""

    pass


class InputSanitizationError(GitCliError):
    """Raised when input contains invalid characters."""

    pass


# Security: Allowlist pattern for git-safe characters
SAFE_INPUT_PATTERN = re.compile(r"^[a-zA-Z0-9._/-]+$")


def sanitize_input(value: str, field_name: str = "input") -> str:
    """
    Sanitize input to prevent command injection.

    Args:
        value: The input string to sanitize
        field_name: Name of the field (for error messages)

    Returns:
        The validated input string

    Raises:
        InputSanitizationError: If input contains invalid characters
    """
    if not value:
        raise InputSanitizationError(f"{field_name} cannot be empty")

    if "\x00" in value:
        raise InputSanitizationError(f"{field_name} contains null bytes")

    if "\n" in value or "\r" in value:
        raise InputSanitizationError(f"{field_name} contains newlines")

    if ".." in value:
        raise InputSanitizationError(f"{field_name} contains path traversal sequence")

    if not SAFE_INPUT_PATTERN.match(value):
        raise InputSanitizationError(
            f"{field_name} contains invalid characters. Allowed: alphanumeric, dash, underscore, dot, forward slash"
        )

    return value


def mask_credentials(url: str) -> str:
    """Mask credentials in URL for logging."""
    # Match patterns like https://token@host or https://user:pass@host
    return re.sub(r"(https?://)([^@]+)@", r"\1***@", url)


class GitCliWrapper:
    """Wrapper for git CLI operations with security sanitization."""

    def __init__(self, base_dir: Path | None = None, timeout: int | None = None):
        """
        Initialize git CLI wrapper.

        Args:
            base_dir: Base directory for cloned repositories (default: tempdir)
            timeout: Timeout for git operations in seconds (default: from settings)
        """
        self.base_dir = base_dir or Path(tempfile.gettempdir()) / "soliplex-git-repos"
        self.timeout = timeout or settings.scm_git_cli_timeout
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_repo_dir(self, owner: str, repo: str) -> Path:
        """Get path to local repository directory."""
        owner = sanitize_input(owner, "owner")
        repo = sanitize_input(repo, "repo")
        return self.base_dir / owner / repo

    def build_clone_url(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> str:
        """
        Build authenticated git clone URL.

        Args:
            base_url: Git server base URL (e.g., https://github.com)
            owner: Repository owner
            repo: Repository name
            token: Authentication token (priority 1)
            username: Username for basic auth (priority 2)
            password: Password for basic auth

        Returns:
            Authenticated clone URL

        Raises:
            AuthenticationConfigError: If no valid auth provided
        """
        owner = sanitize_input(owner, "owner")
        repo = sanitize_input(repo, "repo")

        # Remove trailing slashes and /api/v1 suffix
        base_url = base_url.rstrip("/")
        if base_url.endswith("/api/v1"):
            base_url = base_url[:-7]

        # GitHub API URL conversion
        if "api.github.com" in base_url:
            base_url = "https://github.com"

        # Build URL with authentication
        if token:
            # Token auth: https://token@github.com/owner/repo.git
            url_parts = base_url.split("://", 1)
            if len(url_parts) == 2:
                return f"{url_parts[0]}://{token}@{url_parts[1]}/{owner}/{repo}.git"
            return f"{base_url}/{owner}/{repo}.git"

        if username and password:
            # Basic auth: https://user:pass@host/owner/repo.git
            from urllib.parse import quote

            encoded_user = quote(username, safe="")
            encoded_pass = quote(password, safe="")
            url_parts = base_url.split("://", 1)
            if len(url_parts) == 2:
                return f"{url_parts[0]}://{encoded_user}:{encoded_pass}@{url_parts[1]}/{owner}/{repo}.git"
            return f"{base_url}/{owner}/{repo}.git"

        raise AuthenticationConfigError()

    async def _run_command(
        self,
        cmd: list[str],
        cwd: Path | None = None,
        env: dict | None = None,
    ) -> tuple[int, str, str]:
        """
        Run git command asynchronously with timeout.

        Args:
            cmd: Command and arguments as list
            cwd: Working directory
            env: Environment variables

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        import os

        # Merge environment
        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        logger.debug(f"Running: {' '.join(cmd[:2])}...")  # Log only command, not args with creds

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout,
            )
            return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
        except TimeoutError as err:
            raise GitCliError(f"Git command timed out after {self.timeout}s") from err
        finally:
            proc.kill()
            await proc.wait()

    async def clone(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        branch: str = "main",
    ) -> Path:
        """
        Clone repository to local directory.

        Args:
            base_url: Git server base URL
            owner: Repository owner
            repo: Repository name
            token: Authentication token
            username: Username for basic auth
            password: Password for basic auth
            branch: Branch to clone

        Returns:
            Path to cloned repository

        Raises:
            GitCloneError: If clone fails
        """
        branch = sanitize_input(branch, "branch")
        clone_url = self.build_clone_url(base_url, owner, repo, token, username, password)
        repo_dir = self.get_repo_dir(owner, repo)

        # Ensure parent directory exists
        repo_dir.parent.mkdir(parents=True, exist_ok=True)

        # Remove existing directory if present
        if repo_dir.exists():
            shutil.rmtree(repo_dir)

        cmd = ["git", "clone", "--branch", branch, "--single-branch", "--depth", "1", clone_url, str(repo_dir)]

        logger.info(f"Cloning {mask_credentials(clone_url)} to {repo_dir}")

        returncode, stdout, stderr = await self._run_command(cmd)

        if returncode != 0:
            logger.error(f"Clone failed: {stderr}")
            raise GitCloneError(f"git clone failed with exit code {returncode}: {stderr}")

        logger.info(f"Successfully cloned {owner}/{repo}")
        return repo_dir

    async def pull(self, repo_dir: Path) -> bool:
        """
        Pull latest changes in repository.

        Args:
            repo_dir: Path to repository

        Returns:
            True if pull succeeded, False otherwise
        """
        if not repo_dir.exists():
            logger.warning(f"Repository directory does not exist: {repo_dir}")
            return False

        cmd = ["git", "pull", "--ff-only"]

        logger.info(f"Pulling updates in {repo_dir}")

        returncode, stdout, stderr = await self._run_command(cmd, cwd=repo_dir)

        if returncode != 0:
            logger.warning(f"Pull failed: {stderr}")
            return False

        logger.info("Successfully pulled updates")
        return True

    async def clean(self, repo_dir: Path) -> None:
        """
        Run git clean to remove untracked files.

        Args:
            repo_dir: Path to repository

        Raises:
            GitCleanError: If clean fails
        """
        if not repo_dir.exists():
            return

        cmd = ["git", "clean", "-fd"]

        logger.debug(f"Cleaning untracked files in {repo_dir}")

        returncode, stdout, stderr = await self._run_command(cmd, cwd=repo_dir)

        if returncode != 0:
            raise GitCleanError(f"git clean failed: {stderr}")

    async def ensure_repo(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        branch: str = "main",
    ) -> Path:
        """
        Ensure repository is cloned and up to date.

        If repo doesn't exist, clone it.
        If repo exists, pull updates. If pull fails, delete and re-clone.
        After success, run git clean.

        Args:
            base_url: Git server base URL
            owner: Repository owner
            repo: Repository name
            token: Authentication token
            username: Username for basic auth
            password: Password for basic auth
            branch: Branch name

        Returns:
            Path to repository directory
        """
        repo_dir = self.get_repo_dir(owner, repo)

        if repo_dir.exists() and (repo_dir / ".git").exists():
            # Repository exists, try to pull
            if await self.pull(repo_dir):
                await self.clean(repo_dir)
                return repo_dir
            else:
                # Pull failed, delete and re-clone
                logger.info(f"Pull failed, re-cloning {owner}/{repo}")
                shutil.rmtree(repo_dir)

        # Clone repository
        await self.clone(base_url, owner, repo, token, username, password, branch)
        await self.clean(repo_dir)
        return repo_dir

    async def delete_repo(self, owner: str, repo: str) -> None:
        """Delete local repository clone."""
        repo_dir = self.get_repo_dir(owner, repo)
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
            logger.info(f"Deleted local clone: {repo_dir}")

    async def get_commits_since(
        self,
        repo_dir: Path,
        since_sha: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Get commits using git log.

        Args:
            repo_dir: Path to repository
            since_sha: Get commits after this SHA (exclusive)
            limit: Maximum number of commits

        Returns:
            List of commit dictionaries with 'sha' and 'message'
        """
        cmd = ["git", "log", f"--max-count={limit}", "--format=%H|%s"]

        if since_sha:
            since_sha = sanitize_input(since_sha, "since_sha")
            cmd.append(f"{since_sha}..HEAD")

        returncode, stdout, stderr = await self._run_command(cmd, cwd=repo_dir)

        if returncode != 0:
            logger.error(f"git log failed: {stderr}")
            return []

        commits = []
        for line in stdout.strip().split("\n"):
            if "|" in line:
                sha, message = line.split("|", 1)
                commits.append({"sha": sha.strip(), "message": message.strip()})

        return commits

    async def get_commit_files(self, repo_dir: Path, commit_sha: str) -> dict[str, Any]:
        """
        Get files changed in a commit using git show.

        Args:
            repo_dir: Path to repository
            commit_sha: Commit SHA

        Returns:
            Dict with 'sha' and 'files' list
        """
        commit_sha = sanitize_input(commit_sha, "commit_sha")
        cmd = ["git", "show", "--name-status", "--format=", commit_sha]

        returncode, stdout, stderr = await self._run_command(cmd, cwd=repo_dir)

        if returncode != 0:
            logger.error(f"git show failed: {stderr}")
            return {"sha": commit_sha, "files": []}

        files = []
        for line in stdout.strip().split("\n"):
            if "\t" in line:
                status, filepath = line.split("\t", 1)
                file_status = "removed" if status == "D" else "modified" if status == "M" else "added"
                files.append({"filename": filepath, "status": file_status})

        return {"sha": commit_sha, "files": files}

    async def get_file_last_commit(
        self,
        repo_dir: Path,
        file_path: str,
    ) -> dict[str, Any]:
        """
        Get the last commit info for a file.

        Args:
            repo_dir: Path to repository
            file_path: Relative path to file within repo

        Returns:
            Dict with 'sha' and 'date' (ISO format) or None values if not in git
        """
        # git log -1 --format=%H|%aI -- <file>
        cmd = ["git", "log", "-1", "--format=%H|%aI", "--", file_path]

        returncode, stdout, stderr = await self._run_command(cmd, cwd=repo_dir)

        if returncode != 0 or not stdout.strip():
            return {"sha": None, "date": None}

        parts = stdout.strip().split("|")
        if len(parts) >= 2:
            return {"sha": parts[0], "date": parts[1]}

        return {"sha": None, "date": None}


class GitCliDecorator(BaseSCMProvider):
    """
    Decorator that wraps an SCM provider to use git CLI for file operations.

    File-related methods are overridden to use local git clone.
    API-only methods (issues, repo management) are delegated to wrapped provider.
    """

    def __init__(self, inner: BaseSCMProvider, owner: str | None = None):
        """
        Initialize decorator.

        Args:
            inner: The wrapped SCM provider
            owner: Override owner (uses inner's owner if not provided)
        """
        self._inner = inner
        self._owner = owner or inner.owner
        self._git = GitCliWrapper(
            base_dir=Path(settings.scm_git_repo_base_dir) if settings.scm_git_repo_base_dir else None,
            timeout=settings.scm_git_cli_timeout,
        )
        super().__init__(owner=self._owner)

    # === Delegated methods (API required) ===

    def get_base_url(self) -> str:
        return self._inner.get_base_url()

    def get_auth_token(self) -> str:
        return self._inner.get_auth_token()

    def get_auth_headers(self) -> dict[str, str]:
        return self._inner.get_auth_headers()

    def get_session(self):
        return self._inner.get_session()

    async def list_issues(
        self,
        repo: str,
        owner: str | None = None,
        add_comments: bool = False,
        since=None,
    ):
        return await self._inner.list_issues(repo, owner, add_comments, since)

    async def list_repo_comments(self, owner: str | None, repo: str):
        return await self._inner.list_repo_comments(owner, repo)

    async def list_issue_comments(self, owner: str | None, repo: str, issue_number: int):
        return await self._inner.list_issue_comments(owner, repo, issue_number)

    async def create_repository(
        self,
        name: str,
        description: str = "",
        private: bool = False,
        organization: str | None = None,
    ):
        return await self._inner.create_repository(name, description, private, organization)

    async def delete_repository(self, repo: str, owner: str | None = None):
        return await self._inner.delete_repository(repo, owner)

    async def create_issue(self, repo: str, title: str, body: str = "", owner: str | None = None):
        return await self._inner.create_issue(repo, title, body, owner)

    async def create_file(
        self,
        repo: str,
        file_path: str,
        content,
        message: str = "Add file",
        branch: str = "main",
        owner: str | None = None,
    ):
        return await self._inner.create_file(repo, file_path, content, message, branch, owner)

    async def validate_response(self, response, resp):
        return await self._inner.validate_response(response, resp)

    def get_last_updated(self, rec: dict[str, Any]) -> str | None:
        return self._inner.get_last_updated(rec)

    # === Overridden methods (use local git clone) ===

    def _get_git_base_url(self) -> str:
        """Convert API URL to git URL."""
        api_url = self._inner.get_base_url()

        # GitHub: https://api.github.com -> https://github.com
        if "api.github.com" in api_url:
            return "https://github.com"

        # Gitea/others: https://host/api/v1 -> https://host
        if api_url.endswith("/api/v1"):
            return api_url[:-7]

        return api_url.rstrip("/")

    def _get_credentials(self) -> tuple[str | None, str | None, str | None]:
        """Get authentication credentials."""
        token = settings.scm_auth_token.get_secret_value() if settings.scm_auth_token else None
        username = settings.scm_auth_username
        password = settings.scm_auth_password.get_secret_value() if settings.scm_auth_password else None
        return token, username, password

    async def _ensure_repo_cloned(self, repo: str, owner: str, branch: str = "main") -> Path:
        """Ensure repository is cloned and up to date."""
        base_url = self._get_git_base_url()
        token, username, password = self._get_credentials()
        return await self._git.ensure_repo(base_url, owner, repo, token, username, password, branch)

    async def _read_local_file(self, repo_dir: Path, file_path: str) -> dict[str, Any]:
        """Read a file from local repository."""
        full_path = repo_dir / file_path

        if not full_path.exists():
            raise SCMException(f"File not found: {file_path}")

        async with aiofiles.open(full_path, "rb") as f:
            content = await f.read()

        # Get last commit info for this file
        commit_info = await self._git.get_file_last_commit(repo_dir, file_path)

        return {
            "name": full_path.name,
            "path": file_path,
            "uri": "/" + file_path.replace("\\", "/"),
            "url": "",  # No API URL for local files
            "file_bytes": content,
            "sha256": compute_file_hash(content),
            "content-type": mimetypes.guess_type(full_path.name)[0],
            "last_updated": commit_info["date"],
            "last_commit_sha": commit_info["sha"],
        }

    async def list_repo_files(
        self,
        repo: str,
        owner: str | None = None,
        allowed_extensions: list[str] | None = None,
        branch: str = "main",
    ) -> list[dict[str, Any]]:
        """List all files in repository from local clone."""

        owner = owner or self.owner
        allowed_extensions = allowed_extensions or settings.extensions

        repo_dir = await self._ensure_repo_cloned(repo, owner, branch)

        files = []
        for ext in allowed_extensions:
            for file_path in repo_dir.rglob(f"*.{ext}"):
                # Skip .git directory
                if ".git" in file_path.parts:
                    continue

                rel_path = str(file_path.relative_to(repo_dir)).replace("\\", "/")
                try:
                    file_data = await self._read_local_file(repo_dir, rel_path)
                    files.append(file_data)
                except Exception as e:
                    logger.warning(f"Failed to read {rel_path}: {e}")

        logger.info(f"Found {len(files)} files in local clone of {owner}/{repo}")
        return files

    async def iter_repo_files(
        self,
        repo: str,
        owner: str | None = None,
        branch: str = "main",
    ):
        """Iterate through repository files from local clone."""
        owner = owner or self.owner
        repo_dir = await self._ensure_repo_cloned(repo, owner, branch)

        for file_path in repo_dir.rglob("*"):
            if file_path.is_file() and ".git" not in file_path.parts:
                rel_path = str(file_path.relative_to(repo_dir)).replace("\\", "/")
                try:
                    yield await self._read_local_file(repo_dir, rel_path)
                except Exception as e:
                    logger.warning(f"Failed to read {rel_path}: {e}")

    async def get_single_file(
        self,
        repo: str,
        owner: str | None = None,
        file_path: str = "",
        branch: str = "main",
    ) -> dict[str, Any]:
        """Get a single file from local clone."""
        owner = owner or self.owner
        repo_dir = await self._ensure_repo_cloned(repo, owner, branch)
        return await self._read_local_file(repo_dir, file_path)

    async def get_data_from_url(
        self,
        url: str,
        session=None,
        owner: str | None = None,
        repo: str | None = None,
        allowed_extensions=None,
        semaphore=None,
    ):
        """Not used in CLI mode - delegate to inner for compatibility."""
        return await self._inner.get_data_from_url(url, session, owner, repo, allowed_extensions, semaphore)

    async def list_commits_since(
        self,
        repo: str,
        owner: str | None = None,
        since_commit_sha: str | None = None,
        branch: str = "main",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List commits since a SHA using git log."""
        owner = owner or self.owner
        repo_dir = await self._ensure_repo_cloned(repo, owner, branch)
        return await self._git.get_commits_since(repo_dir, since_commit_sha, limit)

    async def get_commit_details(
        self,
        repo: str,
        owner: str | None = None,
        commit_sha: str = None,
    ) -> dict[str, Any]:
        """Get commit details using git show."""
        owner = owner or self.owner
        repo_dir = self._git.get_repo_dir(owner, repo)

        if not repo_dir.exists():
            # Need to clone first
            repo_dir = await self._ensure_repo_cloned(repo, owner)

        return await self._git.get_commit_files(repo_dir, commit_sha)
