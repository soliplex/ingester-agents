"""Tests for soliplex.agents.scm.git_cli module."""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from soliplex.agents.scm import AuthenticationConfigError
from soliplex.agents.scm.git_cli import GitCleanError
from soliplex.agents.scm.git_cli import GitCliDecorator
from soliplex.agents.scm.git_cli import GitCliError
from soliplex.agents.scm.git_cli import GitCliWrapper
from soliplex.agents.scm.git_cli import GitCloneError
from soliplex.agents.scm.git_cli import InputSanitizationError
from soliplex.agents.scm.git_cli import mask_credentials
from soliplex.agents.scm.git_cli import sanitize_input

# ====================
# Lazy import tests (for __init__.py coverage)
# ====================


class TestLazyImports:
    """Tests for lazy imports from soliplex.agents.scm module."""

    def test_lazy_import_git_cli_decorator(self):
        """Test lazy import of GitCliDecorator from scm package."""
        from soliplex.agents.scm import GitCliDecorator

        assert GitCliDecorator is not None

    def test_lazy_import_git_cli_wrapper(self):
        """Test lazy import of GitCliWrapper from scm package."""
        from soliplex.agents.scm import GitCliWrapper

        assert GitCliWrapper is not None

    def test_lazy_import_sanitize_input(self):
        """Test lazy import of sanitize_input from scm package."""
        from soliplex.agents.scm import sanitize_input

        assert sanitize_input("test", "field") == "test"

    def test_lazy_import_mask_credentials(self):
        """Test lazy import of mask_credentials from scm package."""
        from soliplex.agents.scm import mask_credentials

        assert "***" in mask_credentials("https://token@github.com")

    def test_lazy_import_error_classes(self):
        """Test lazy import of error classes from scm package."""
        from soliplex.agents.scm import GitCleanError
        from soliplex.agents.scm import GitCliError
        from soliplex.agents.scm import GitCloneError
        from soliplex.agents.scm import GitPullError
        from soliplex.agents.scm import InputSanitizationError

        assert issubclass(GitCliError, Exception)
        assert issubclass(GitCloneError, GitCliError)
        assert issubclass(GitPullError, GitCliError)
        assert issubclass(GitCleanError, GitCliError)
        assert issubclass(InputSanitizationError, GitCliError)

    def test_lazy_import_unknown_attribute_raises(self):
        """Test that accessing unknown attribute raises AttributeError."""
        import soliplex.agents.scm as scm_module

        with pytest.raises(AttributeError, match="has no attribute 'NonExistentClass'"):
            _ = scm_module.NonExistentClass


# ====================
# sanitize_input tests
# ====================


class TestSanitizeInput:
    """Tests for sanitize_input function."""

    def test_valid_alphanumeric(self):
        """Test valid alphanumeric input."""
        assert sanitize_input("myrepo123", "repo") == "myrepo123"

    def test_valid_with_dashes(self):
        """Test valid input with dashes."""
        assert sanitize_input("my-repo-name", "repo") == "my-repo-name"

    def test_valid_with_underscores(self):
        """Test valid input with underscores."""
        assert sanitize_input("my_repo_name", "repo") == "my_repo_name"

    def test_valid_with_dots(self):
        """Test valid input with dots."""
        assert sanitize_input("repo.name", "repo") == "repo.name"

    def test_valid_with_slashes(self):
        """Test valid input with forward slashes."""
        assert sanitize_input("path/to/repo", "path") == "path/to/repo"

    def test_valid_mixed(self):
        """Test valid input with mixed characters."""
        assert sanitize_input("my-repo_v1.0/subdir", "path") == "my-repo_v1.0/subdir"

    def test_rejects_empty(self):
        """Test that empty input raises error."""
        with pytest.raises(InputSanitizationError, match="cannot be empty"):
            sanitize_input("", "repo")

    def test_rejects_semicolon(self):
        """Test that semicolon is rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input("repo;rm -rf /", "repo")

    def test_rejects_pipe(self):
        """Test that pipe is rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input("repo|cat /etc/passwd", "repo")

    def test_rejects_ampersand(self):
        """Test that ampersand is rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input("repo&&whoami", "repo")

    def test_rejects_dollar(self):
        """Test that dollar sign is rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input("repo$USER", "repo")

    def test_rejects_backtick(self):
        """Test that backtick is rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input("repo`id`", "repo")

    def test_rejects_parentheses(self):
        """Test that parentheses are rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input("repo()", "repo")

    def test_rejects_brackets(self):
        """Test that brackets are rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input("repo[]", "repo")

    def test_rejects_angle_brackets(self):
        """Test that angle brackets are rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input("repo<>", "repo")

    def test_rejects_single_quotes(self):
        """Test that single quotes are rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input("repo'test'", "repo")

    def test_rejects_double_quotes(self):
        """Test that double quotes are rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input('repo"test"', "repo")

    def test_rejects_backslash(self):
        """Test that backslash is rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input("repo\\test", "repo")

    def test_rejects_newlines(self):
        """Test that newlines are rejected."""
        with pytest.raises(InputSanitizationError, match="contains newlines"):
            sanitize_input("repo\nmalicious", "repo")

    def test_rejects_carriage_return(self):
        """Test that carriage returns are rejected."""
        with pytest.raises(InputSanitizationError, match="contains newlines"):
            sanitize_input("repo\rmalicious", "repo")

    def test_rejects_null_bytes(self):
        """Test that null bytes are rejected."""
        with pytest.raises(InputSanitizationError, match="contains null bytes"):
            sanitize_input("repo\x00malicious", "repo")

    def test_rejects_path_traversal(self):
        """Test that path traversal sequences are rejected."""
        with pytest.raises(InputSanitizationError, match="path traversal"):
            sanitize_input("../../../etc/passwd", "path")

    def test_rejects_path_traversal_middle(self):
        """Test that path traversal in middle is rejected."""
        with pytest.raises(InputSanitizationError, match="path traversal"):
            sanitize_input("repo/../../../etc/passwd", "path")

    def test_rejects_spaces(self):
        """Test that spaces are rejected."""
        with pytest.raises(InputSanitizationError, match="invalid characters"):
            sanitize_input("repo name", "repo")


# ====================
# mask_credentials tests
# ====================


class TestMaskCredentials:
    """Tests for mask_credentials function."""

    def test_mask_token_auth(self):
        """Test masking token in URL."""
        url = "https://ghp_token123@github.com/owner/repo.git"
        assert mask_credentials(url) == "https://***@github.com/owner/repo.git"

    def test_mask_basic_auth(self):
        """Test masking user:pass in URL."""
        url = "https://user:password@gitea.example.com/owner/repo.git"
        assert mask_credentials(url) == "https://***@gitea.example.com/owner/repo.git"

    def test_no_credentials(self):
        """Test URL without credentials unchanged."""
        url = "https://github.com/owner/repo.git"
        assert mask_credentials(url) == "https://github.com/owner/repo.git"

    def test_http_protocol(self):
        """Test HTTP URL masking."""
        url = "http://token@localhost/repo.git"
        assert mask_credentials(url) == "http://***@localhost/repo.git"


# ====================
# GitCliWrapper tests
# ====================


class TestGitCliWrapper:
    """Tests for GitCliWrapper class."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for tests."""
        temp = tempfile.mkdtemp()
        yield Path(temp)
        shutil.rmtree(temp, ignore_errors=True)

    @pytest.fixture
    def wrapper(self, temp_dir):
        """Create a GitCliWrapper instance with temp directory."""
        return GitCliWrapper(base_dir=temp_dir, timeout=10)

    def test_init_with_defaults(self):
        """Test initialization with default values."""
        with patch("soliplex.agents.scm.git_cli.settings") as mock_settings:
            mock_settings.scm_git_cli_timeout = 300
            wrapper = GitCliWrapper()
            assert wrapper.timeout == 300
            assert "soliplex-git-repos" in str(wrapper.base_dir)

    def test_init_with_custom_values(self, temp_dir):
        """Test initialization with custom values."""
        wrapper = GitCliWrapper(base_dir=temp_dir, timeout=60)
        assert wrapper.base_dir == temp_dir
        assert wrapper.timeout == 60

    def test_get_repo_dir(self, wrapper):
        """Test get_repo_dir returns correct path."""
        repo_dir = wrapper.get_repo_dir("myowner", "myrepo")
        assert repo_dir == wrapper.base_dir / "myowner" / "myrepo"

    def test_get_repo_dir_sanitizes_input(self, wrapper):
        """Test that get_repo_dir sanitizes input."""
        with pytest.raises(InputSanitizationError):
            wrapper.get_repo_dir("owner;rm -rf /", "repo")

    # build_clone_url tests

    def test_build_clone_url_with_token_github(self, wrapper):
        """Test building clone URL with token for GitHub."""
        url = wrapper.build_clone_url(
            "https://api.github.com",
            "owner",
            "repo",
            token="ghp_token123",
        )
        assert url == "https://ghp_token123@github.com/owner/repo.git"

    def test_build_clone_url_with_token_gitea(self, wrapper):
        """Test building clone URL with token for Gitea."""
        url = wrapper.build_clone_url(
            "https://gitea.example.com/api/v1",
            "owner",
            "repo",
            token="token123",
        )
        assert url == "https://token123@gitea.example.com/owner/repo.git"

    def test_build_clone_url_with_basic_auth(self, wrapper):
        """Test building clone URL with basic auth."""
        url = wrapper.build_clone_url(
            "https://gitea.example.com/api/v1",
            "owner",
            "repo",
            username="user",
            password="pass",
        )
        assert url == "https://user:pass@gitea.example.com/owner/repo.git"

    def test_build_clone_url_with_special_chars_in_password(self, wrapper):
        """Test building clone URL with special characters in password."""
        url = wrapper.build_clone_url(
            "https://gitea.example.com",
            "owner",
            "repo",
            username="user",
            password="p@ss:w/rd",
        )
        # Special chars should be URL-encoded
        assert "p%40ss%3Aw%2Frd" in url

    def test_build_clone_url_no_auth_raises(self, wrapper):
        """Test that building clone URL without auth raises error."""
        with pytest.raises(AuthenticationConfigError):
            wrapper.build_clone_url("https://github.com", "owner", "repo")

    def test_build_clone_url_strips_trailing_slash(self, wrapper):
        """Test that trailing slashes are stripped."""
        url = wrapper.build_clone_url(
            "https://github.com/",
            "owner",
            "repo",
            token="token",
        )
        assert "//" not in url.replace("https://", "")

    def test_build_clone_url_no_protocol_split(self, wrapper):
        """Test URL building when no protocol present (edge case)."""
        url = wrapper.build_clone_url(
            "github.com",
            "owner",
            "repo",
            token="token",
        )
        assert url == "github.com/owner/repo.git"

    def test_build_clone_url_basic_auth_no_protocol_split(self, wrapper):
        """Test basic auth URL building when no protocol present."""
        url = wrapper.build_clone_url(
            "github.com",
            "owner",
            "repo",
            username="user",
            password="pass",
        )
        assert url == "github.com/owner/repo.git"

    # _run_command tests

    @pytest.mark.asyncio
    async def test_run_command_success(self, wrapper):
        """Test successful command execution."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"output", b"")
            mock_exec.return_value = mock_proc

            returncode, stdout, stderr = await wrapper._run_command(["git", "--version"])

            assert returncode == 0
            assert stdout == "output"
            assert stderr == ""

    @pytest.mark.asyncio
    async def test_run_command_with_cwd(self, wrapper, temp_dir):
        """Test command execution with working directory."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"", b"")
            mock_exec.return_value = mock_proc

            await wrapper._run_command(["git", "status"], cwd=temp_dir)

            # Verify cwd was passed
            call_kwargs = mock_exec.call_args.kwargs
            assert call_kwargs["cwd"] == temp_dir

    @pytest.mark.asyncio
    async def test_run_command_with_env(self, wrapper):
        """Test command execution with custom environment."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"", b"")
            mock_exec.return_value = mock_proc

            await wrapper._run_command(["git", "status"], env={"GIT_DIR": "/custom"})

            # Verify env includes custom var
            call_kwargs = mock_exec.call_args.kwargs
            assert "GIT_DIR" in call_kwargs["env"]

    @pytest.mark.asyncio
    async def test_run_command_timeout(self, wrapper):
        """Test command timeout handling."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.side_effect = TimeoutError()
            mock_proc.kill = MagicMock()
            mock_proc.wait = AsyncMock()
            mock_exec.return_value = mock_proc

            with pytest.raises(GitCliError, match="timed out"):
                await wrapper._run_command(["git", "clone", "url"])

            mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_command_decodes_utf8(self, wrapper):
        """Test command output is decoded as UTF-8."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = ("unicode: \u00e9".encode(), b"")
            mock_exec.return_value = mock_proc

            _, stdout, _ = await wrapper._run_command(["echo", "test"])

            assert "unicode: \u00e9" in stdout

    # clone tests

    @pytest.mark.asyncio
    async def test_clone_success(self, wrapper, temp_dir):
        """Test successful clone operation."""
        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            repo_dir = await wrapper.clone(
                "https://github.com",
                "owner",
                "repo",
                token="token",
                branch="main",
            )

            assert repo_dir == temp_dir / "owner" / "repo"
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "git" in cmd
            assert "clone" in cmd

    @pytest.mark.asyncio
    async def test_clone_creates_parent_directory(self, wrapper, temp_dir):
        """Test that clone creates parent directory."""
        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            await wrapper.clone(
                "https://github.com",
                "newowner",
                "newrepo",
                token="token",
            )

            assert (temp_dir / "newowner").exists()

    @pytest.mark.asyncio
    async def test_clone_removes_existing_directory(self, wrapper, temp_dir):
        """Test that clone removes existing directory."""
        # Create existing directory
        existing = temp_dir / "owner" / "repo"
        existing.mkdir(parents=True)
        (existing / "old_file.txt").write_text("old content")

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            await wrapper.clone(
                "https://github.com",
                "owner",
                "repo",
                token="token",
            )

            # Old file should be gone (directory was removed and recreated by git)
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_clone_failure_raises_error(self, wrapper):
        """Test that clone failure raises GitCloneError."""
        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (128, "", "fatal: repository not found")

            with pytest.raises(GitCloneError, match="git clone failed"):
                await wrapper.clone(
                    "https://github.com",
                    "owner",
                    "repo",
                    token="token",
                )

    @pytest.mark.asyncio
    async def test_clone_with_custom_branch(self, wrapper):
        """Test clone with custom branch."""
        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            await wrapper.clone(
                "https://github.com",
                "owner",
                "repo",
                token="token",
                branch="develop",
            )

            cmd = mock_run.call_args[0][0]
            assert "--branch" in cmd
            assert "develop" in cmd

    # pull tests

    @pytest.mark.asyncio
    async def test_pull_success(self, wrapper, temp_dir):
        """Test successful pull operation."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "Already up to date.", "")

            result = await wrapper.pull(repo_dir)

            assert result is True
            cmd = mock_run.call_args[0][0]
            assert "pull" in cmd
            assert "--ff-only" in cmd

    @pytest.mark.asyncio
    async def test_pull_failure_returns_false(self, wrapper, temp_dir):
        """Test that pull failure returns False."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (1, "", "merge conflict")

            result = await wrapper.pull(repo_dir)

            assert result is False

    @pytest.mark.asyncio
    async def test_pull_nonexistent_repo_returns_false(self, wrapper, temp_dir):
        """Test that pull on nonexistent repo returns False."""
        repo_dir = temp_dir / "nonexistent"

        result = await wrapper.pull(repo_dir)

        assert result is False

    # clean tests

    @pytest.mark.asyncio
    async def test_clean_success(self, wrapper, temp_dir):
        """Test successful clean operation."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            await wrapper.clean(repo_dir)

            cmd = mock_run.call_args[0][0]
            assert "clean" in cmd
            assert "-fd" in cmd

    @pytest.mark.asyncio
    async def test_clean_nonexistent_repo_noop(self, wrapper, temp_dir):
        """Test that clean on nonexistent repo is a no-op."""
        repo_dir = temp_dir / "nonexistent"

        # Should not raise
        await wrapper.clean(repo_dir)

    @pytest.mark.asyncio
    async def test_clean_failure_raises_error(self, wrapper, temp_dir):
        """Test that clean failure raises GitCleanError."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (1, "", "permission denied")

            with pytest.raises(GitCleanError, match="git clean failed"):
                await wrapper.clean(repo_dir)

    # ensure_repo tests

    @pytest.mark.asyncio
    async def test_ensure_repo_clones_when_missing(self, wrapper, temp_dir):
        """Test that ensure_repo clones when repo doesn't exist."""
        with patch.object(wrapper, "clone") as mock_clone:
            with patch.object(wrapper, "clean") as mock_clean:
                mock_clone.return_value = temp_dir / "owner" / "repo"

                await wrapper.ensure_repo(
                    "https://github.com",
                    "owner",
                    "repo",
                    token="token",
                )

                mock_clone.assert_called_once()
                mock_clean.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_repo_pulls_when_exists(self, wrapper, temp_dir):
        """Test that ensure_repo pulls when repo exists."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        with patch.object(wrapper, "pull") as mock_pull:
            with patch.object(wrapper, "clean") as mock_clean:
                mock_pull.return_value = True

                result = await wrapper.ensure_repo(
                    "https://github.com",
                    "owner",
                    "repo",
                    token="token",
                )

                mock_pull.assert_called_once()
                mock_clean.assert_called_once()
                assert result == repo_dir

    @pytest.mark.asyncio
    async def test_ensure_repo_reclones_on_pull_failure(self, wrapper, temp_dir):
        """Test that ensure_repo re-clones when pull fails."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        with patch.object(wrapper, "pull") as mock_pull:
            with patch.object(wrapper, "clone") as mock_clone:
                with patch.object(wrapper, "clean"):
                    mock_pull.return_value = False
                    mock_clone.return_value = repo_dir

                    await wrapper.ensure_repo(
                        "https://github.com",
                        "owner",
                        "repo",
                        token="token",
                    )

                    mock_pull.assert_called_once()
                    mock_clone.assert_called_once()

    # delete_repo tests

    @pytest.mark.asyncio
    async def test_delete_repo_removes_directory(self, wrapper, temp_dir):
        """Test that delete_repo removes the directory."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "file.txt").write_text("content")

        await wrapper.delete_repo("owner", "repo")

        assert not repo_dir.exists()

    @pytest.mark.asyncio
    async def test_delete_repo_nonexistent_noop(self, wrapper, temp_dir):
        """Test that delete_repo on nonexistent repo is a no-op."""
        # Should not raise
        await wrapper.delete_repo("nonexistent", "repo")

    # get_commits_since tests

    @pytest.mark.asyncio
    async def test_get_commits_since_no_marker(self, wrapper, temp_dir):
        """Test getting commits without a marker SHA."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "abc123|First commit\ndef456|Second commit\n", "")

            commits = await wrapper.get_commits_since(repo_dir)

            assert len(commits) == 2
            assert commits[0]["sha"] == "abc123"
            assert commits[0]["message"] == "First commit"

    @pytest.mark.asyncio
    async def test_get_commits_since_with_marker(self, wrapper, temp_dir):
        """Test getting commits with a marker SHA."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "abc123|New commit\n", "")

            await wrapper.get_commits_since(repo_dir, since_sha="def456")

            cmd = mock_run.call_args[0][0]
            assert "def456..HEAD" in cmd

    @pytest.mark.asyncio
    async def test_get_commits_since_failure_returns_empty(self, wrapper, temp_dir):
        """Test that git log failure returns empty list."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (1, "", "error")

            commits = await wrapper.get_commits_since(repo_dir)

            assert commits == []

    @pytest.mark.asyncio
    async def test_get_commits_since_empty_output(self, wrapper, temp_dir):
        """Test handling of empty git log output."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            commits = await wrapper.get_commits_since(repo_dir)

            assert commits == []

    # get_commit_files tests

    @pytest.mark.asyncio
    async def test_get_commit_files_added(self, wrapper, temp_dir):
        """Test getting files from a commit with added files."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "A\tnew_file.txt\n", "")

            result = await wrapper.get_commit_files(repo_dir, "abc123")

            assert result["sha"] == "abc123"
            assert len(result["files"]) == 1
            assert result["files"][0]["filename"] == "new_file.txt"
            assert result["files"][0]["status"] == "added"

    @pytest.mark.asyncio
    async def test_get_commit_files_modified(self, wrapper, temp_dir):
        """Test getting files from a commit with modified files."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "M\tchanged_file.txt\n", "")

            result = await wrapper.get_commit_files(repo_dir, "abc123")

            assert result["files"][0]["status"] == "modified"

    @pytest.mark.asyncio
    async def test_get_commit_files_deleted(self, wrapper, temp_dir):
        """Test getting files from a commit with deleted files."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (0, "D\tdeleted_file.txt\n", "")

            result = await wrapper.get_commit_files(repo_dir, "abc123")

            assert result["files"][0]["status"] == "removed"

    @pytest.mark.asyncio
    async def test_get_commit_files_failure_returns_empty(self, wrapper, temp_dir):
        """Test that git show failure returns empty files list."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            mock_run.return_value = (1, "", "error")

            result = await wrapper.get_commit_files(repo_dir, "abc123")

            assert result["files"] == []

    @pytest.mark.asyncio
    async def test_get_commit_files_with_empty_lines(self, wrapper, temp_dir):
        """Test get_commit_files skips lines without tabs."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)

        with patch.object(wrapper, "_run_command") as mock_run:
            # Output with empty lines and lines without tabs (e.g., commit message remnants)
            mock_run.return_value = (0, "\nsome header line\nA\tfile.txt\n\n", "")

            result = await wrapper.get_commit_files(repo_dir, "abc123")

            # Should only include the line with tab
            assert len(result["files"]) == 1
            assert result["files"][0]["filename"] == "file.txt"


# ====================
# GitCliDecorator tests
# ====================


class TestGitCliDecorator:
    """Tests for GitCliDecorator class."""

    @pytest.fixture
    def mock_inner_provider(self):
        """Create a mock inner provider."""
        provider = MagicMock()
        provider.owner = "default_owner"
        provider.get_base_url.return_value = "https://api.github.com"
        provider.get_default_owner.return_value = "default_owner"
        provider.get_auth_token.return_value = "token"
        provider.get_auth_headers.return_value = {"Authorization": "token xxx"}
        provider.get_session.return_value = MagicMock()
        provider.get_last_updated.return_value = "2024-01-01"
        provider.list_issues = AsyncMock(return_value=[])
        provider.list_repo_comments = AsyncMock(return_value=[])
        provider.list_issue_comments = AsyncMock(return_value=[])
        provider.create_repository = AsyncMock(return_value={})
        provider.delete_repository = AsyncMock(return_value=True)
        provider.create_issue = AsyncMock(return_value={})
        provider.create_file = AsyncMock(return_value={})
        provider.validate_response = AsyncMock()
        provider.get_data_from_url = AsyncMock(return_value={})
        return provider

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for tests."""
        temp = tempfile.mkdtemp()
        yield Path(temp)
        shutil.rmtree(temp, ignore_errors=True)

    @pytest.fixture
    def decorator(self, mock_inner_provider, temp_dir):
        """Create a GitCliDecorator instance."""
        with patch("soliplex.agents.scm.git_cli.settings") as mock_settings:
            mock_settings.scm_git_repo_base_dir = str(temp_dir)
            mock_settings.scm_git_cli_timeout = 10
            mock_settings.scm_auth_token = SecretStr("test_token")
            mock_settings.scm_auth_username = None
            mock_settings.scm_auth_password = None
            mock_settings.extensions = ["md", "txt"]
            return GitCliDecorator(mock_inner_provider)

    # Delegation tests

    def test_delegates_get_default_owner(self, decorator, mock_inner_provider):
        """Test that get_default_owner is delegated."""
        result = decorator.get_default_owner()
        assert result == "default_owner"
        mock_inner_provider.get_default_owner.assert_called_once()

    def test_delegates_get_base_url(self, decorator, mock_inner_provider):
        """Test that get_base_url is delegated."""
        result = decorator.get_base_url()
        assert result == "https://api.github.com"
        mock_inner_provider.get_base_url.assert_called_once()

    def test_delegates_get_auth_token(self, decorator, mock_inner_provider):
        """Test that get_auth_token is delegated."""
        result = decorator.get_auth_token()
        assert result == "token"

    def test_delegates_get_auth_headers(self, decorator, mock_inner_provider):
        """Test that get_auth_headers is delegated."""
        result = decorator.get_auth_headers()
        assert "Authorization" in result

    def test_delegates_get_session(self, decorator, mock_inner_provider):
        """Test that get_session is delegated."""
        decorator.get_session()
        mock_inner_provider.get_session.assert_called_once()

    def test_delegates_get_last_updated(self, decorator, mock_inner_provider):
        """Test that get_last_updated is delegated."""
        result = decorator.get_last_updated({"name": "test"})
        assert result == "2024-01-01"

    @pytest.mark.asyncio
    async def test_delegates_list_issues(self, decorator, mock_inner_provider):
        """Test that list_issues is delegated to inner provider."""
        await decorator.list_issues("repo", "owner", True)
        mock_inner_provider.list_issues.assert_called_once()

    @pytest.mark.asyncio
    async def test_delegates_list_repo_comments(self, decorator, mock_inner_provider):
        """Test that list_repo_comments is delegated."""
        await decorator.list_repo_comments("owner", "repo")
        mock_inner_provider.list_repo_comments.assert_called_once()

    @pytest.mark.asyncio
    async def test_delegates_list_issue_comments(self, decorator, mock_inner_provider):
        """Test that list_issue_comments is delegated."""
        await decorator.list_issue_comments("owner", "repo", 1)
        mock_inner_provider.list_issue_comments.assert_called_once()

    @pytest.mark.asyncio
    async def test_delegates_create_repository(self, decorator, mock_inner_provider):
        """Test that create_repository is delegated."""
        await decorator.create_repository("repo", "desc", True, "org")
        mock_inner_provider.create_repository.assert_called_once()

    @pytest.mark.asyncio
    async def test_delegates_delete_repository(self, decorator, mock_inner_provider):
        """Test that delete_repository is delegated."""
        await decorator.delete_repository("repo", "owner")
        mock_inner_provider.delete_repository.assert_called_once()

    @pytest.mark.asyncio
    async def test_delegates_create_issue(self, decorator, mock_inner_provider):
        """Test that create_issue is delegated."""
        await decorator.create_issue("repo", "title", "body", "owner")
        mock_inner_provider.create_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_delegates_create_file(self, decorator, mock_inner_provider):
        """Test that create_file is delegated."""
        await decorator.create_file("repo", "path", "content", "msg", "main", "owner")
        mock_inner_provider.create_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_delegates_validate_response(self, decorator, mock_inner_provider):
        """Test that validate_response is delegated."""
        await decorator.validate_response(MagicMock(), {})
        mock_inner_provider.validate_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_delegates_get_data_from_url(self, decorator, mock_inner_provider):
        """Test that get_data_from_url is delegated."""
        await decorator.get_data_from_url("http://example.com")
        mock_inner_provider.get_data_from_url.assert_called_once()

    # URL conversion tests

    def test_get_git_base_url_github(self, decorator, mock_inner_provider):
        """Test converting GitHub API URL to git URL."""
        mock_inner_provider.get_base_url.return_value = "https://api.github.com"
        result = decorator._get_git_base_url()
        assert result == "https://github.com"

    def test_get_git_base_url_gitea(self, decorator, mock_inner_provider):
        """Test converting Gitea API URL to git URL."""
        mock_inner_provider.get_base_url.return_value = "https://gitea.example.com/api/v1"
        result = decorator._get_git_base_url()
        assert result == "https://gitea.example.com"

    def test_get_git_base_url_custom(self, decorator, mock_inner_provider):
        """Test custom URL without api/v1."""
        mock_inner_provider.get_base_url.return_value = "https://custom.git.com/"
        result = decorator._get_git_base_url()
        assert result == "https://custom.git.com"

    # Credential tests

    def test_get_credentials_with_token(self, decorator):
        """Test getting credentials when token is set."""
        with patch("soliplex.agents.scm.git_cli.settings") as mock_settings:
            mock_settings.scm_auth_token = SecretStr("mytoken")
            mock_settings.scm_auth_username = None
            mock_settings.scm_auth_password = None

            token, username, password = decorator._get_credentials()

            assert token == "mytoken"
            assert username is None
            assert password is None

    def test_get_credentials_with_basic_auth(self, decorator):
        """Test getting credentials when basic auth is set."""
        with patch("soliplex.agents.scm.git_cli.settings") as mock_settings:
            mock_settings.scm_auth_token = None
            mock_settings.scm_auth_username = "user"
            mock_settings.scm_auth_password = SecretStr("pass")

            token, username, password = decorator._get_credentials()

            assert token is None
            assert username == "user"
            assert password == "pass"

    @pytest.mark.asyncio
    async def test_ensure_repo_cloned(self, decorator, temp_dir):
        """Test _ensure_repo_cloned calls git wrapper correctly."""
        with patch("soliplex.agents.scm.git_cli.settings") as mock_settings:
            mock_settings.scm_auth_token = SecretStr("test_token")
            mock_settings.scm_auth_username = None
            mock_settings.scm_auth_password = None

            with patch.object(decorator._git, "ensure_repo") as mock_ensure:
                mock_ensure.return_value = temp_dir / "owner" / "repo"

                result = await decorator._ensure_repo_cloned("repo", "owner", "main")

                mock_ensure.assert_called_once()
                assert result == temp_dir / "owner" / "repo"

    # File reading tests

    @pytest.mark.asyncio
    async def test_read_local_file_success(self, decorator, temp_dir):
        """Test reading a local file."""
        # Create a test file
        test_file = temp_dir / "test.md"
        test_file.write_text("# Test Content")

        result = await decorator._read_local_file(temp_dir, "test.md")

        assert result["name"] == "test.md"
        assert result["file_bytes"] == b"# Test Content"
        assert result["sha256"] is not None

    @pytest.mark.asyncio
    async def test_read_local_file_not_found(self, decorator, temp_dir):
        """Test reading a nonexistent file raises error."""
        from soliplex.agents.scm import SCMException

        with pytest.raises(SCMException, match="File not found"):
            await decorator._read_local_file(temp_dir, "nonexistent.md")

    # Override tests

    @pytest.mark.asyncio
    async def test_list_repo_files_uses_local_clone(self, decorator, temp_dir):
        """Test that list_repo_files uses local clone."""
        # Create test repo structure
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()
        (repo_dir / "doc.md").write_text("# Doc")
        (repo_dir / "readme.txt").write_text("Readme")
        (repo_dir / "script.py").write_text("print('hi')")  # Should be excluded

        with patch.object(decorator, "_ensure_repo_cloned") as mock_ensure:
            mock_ensure.return_value = repo_dir

            with patch("soliplex.agents.scm.git_cli.settings") as mock_settings:
                mock_settings.extensions = ["md", "txt"]

                files = await decorator.list_repo_files("repo", "owner")

            assert len(files) == 2
            names = [f["name"] for f in files]
            assert "doc.md" in names
            assert "readme.txt" in names
            assert "script.py" not in names

    @pytest.mark.asyncio
    async def test_list_repo_files_excludes_git_directory(self, decorator, temp_dir):
        """Test that .git directory is excluded even when files match extension."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        git_dir = repo_dir / ".git"
        git_dir.mkdir()
        # Create a .md file inside .git that matches the extension filter
        (git_dir / "hooks").mkdir()
        (git_dir / "hooks" / "readme.md").write_text("# Git hooks readme")
        (repo_dir / "doc.md").write_text("# Doc")

        with patch.object(decorator, "_ensure_repo_cloned") as mock_ensure:
            mock_ensure.return_value = repo_dir

            with patch("soliplex.agents.scm.git_cli.settings") as mock_settings:
                mock_settings.extensions = ["md"]

                files = await decorator.list_repo_files("repo", "owner")

            # Only doc.md should be included, not .git/hooks/readme.md
            assert len(files) == 1
            assert files[0]["name"] == "doc.md"

    @pytest.mark.asyncio
    async def test_list_repo_files_uses_default_owner(self, decorator, temp_dir):
        """Test that list_repo_files uses default owner when not provided."""
        repo_dir = temp_dir / "default_owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()
        (repo_dir / "doc.md").write_text("# Doc")

        with patch.object(decorator, "_ensure_repo_cloned") as mock_ensure:
            mock_ensure.return_value = repo_dir

            with patch("soliplex.agents.scm.git_cli.settings") as mock_settings:
                mock_settings.extensions = ["md"]

                await decorator.list_repo_files("repo")

            mock_ensure.assert_called_with("repo", "default_owner", "main")

    @pytest.mark.asyncio
    async def test_get_single_file(self, decorator, temp_dir):
        """Test getting a single file from local clone."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "doc.md").write_text("# Single Doc")

        with patch.object(decorator, "_ensure_repo_cloned") as mock_ensure:
            mock_ensure.return_value = repo_dir

            result = await decorator.get_single_file("repo", "owner", "doc.md")

            assert result["name"] == "doc.md"
            assert result["file_bytes"] == b"# Single Doc"

    @pytest.mark.asyncio
    async def test_list_commits_since(self, decorator, temp_dir):
        """Test listing commits since a SHA."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        with patch.object(decorator, "_ensure_repo_cloned") as mock_ensure:
            mock_ensure.return_value = repo_dir

            with patch.object(decorator._git, "get_commits_since") as mock_commits:
                mock_commits.return_value = [{"sha": "abc", "message": "test"}]

                commits = await decorator.list_commits_since("repo", "owner", "def456")

                assert len(commits) == 1

    @pytest.mark.asyncio
    async def test_get_commit_details(self, decorator, temp_dir):
        """Test getting commit details."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        # Set up the git wrapper's base_dir
        decorator._git.base_dir = temp_dir

        with patch.object(decorator._git, "get_commit_files") as mock_files:
            mock_files.return_value = {"sha": "abc", "files": []}

            result = await decorator.get_commit_details("repo", "owner", "abc123")

            assert result["sha"] == "abc"

    @pytest.mark.asyncio
    async def test_get_commit_details_clones_if_needed(self, decorator, temp_dir):
        """Test that get_commit_details clones repo if not present."""
        decorator._git.base_dir = temp_dir

        with patch.object(decorator, "_ensure_repo_cloned") as mock_ensure:
            mock_ensure.return_value = temp_dir / "owner" / "repo"

            with patch.object(decorator._git, "get_commit_files") as mock_files:
                mock_files.return_value = {"sha": "abc", "files": []}

                await decorator.get_commit_details("repo", "owner", "abc123")

                mock_ensure.assert_called_once()

    @pytest.mark.asyncio
    async def test_iter_repo_files(self, decorator, temp_dir):
        """Test iterating through repo files."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()
        (repo_dir / "doc1.md").write_text("# Doc 1")
        (repo_dir / "doc2.md").write_text("# Doc 2")

        with patch.object(decorator, "_ensure_repo_cloned") as mock_ensure:
            mock_ensure.return_value = repo_dir

            files = []
            async for f in decorator.iter_repo_files("repo", "owner"):
                files.append(f)

            assert len(files) == 2

    @pytest.mark.asyncio
    async def test_iter_repo_files_skips_directories(self, decorator, temp_dir):
        """Test that iter_repo_files skips directories."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()
        (repo_dir / "subdir").mkdir()
        (repo_dir / "doc.md").write_text("# Doc")

        with patch.object(decorator, "_ensure_repo_cloned") as mock_ensure:
            mock_ensure.return_value = repo_dir

            files = []
            async for f in decorator.iter_repo_files("repo", "owner"):
                files.append(f)

            # Only the file, not the directory
            assert len(files) == 1
            assert files[0]["name"] == "doc.md"

    @pytest.mark.asyncio
    async def test_list_repo_files_handles_read_errors(self, decorator, temp_dir):
        """Test that list_repo_files handles file read errors gracefully."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()
        (repo_dir / "doc.md").write_text("# Doc")

        with patch.object(decorator, "_ensure_repo_cloned") as mock_ensure:
            mock_ensure.return_value = repo_dir

            with patch.object(decorator, "_read_local_file") as mock_read:
                mock_read.side_effect = Exception("Read error")

                with patch("soliplex.agents.scm.git_cli.settings") as mock_settings:
                    mock_settings.extensions = ["md"]

                    files = await decorator.list_repo_files("repo", "owner")

                    # Should return empty list due to error
                    assert files == []

    @pytest.mark.asyncio
    async def test_iter_repo_files_handles_read_errors(self, decorator, temp_dir):
        """Test that iter_repo_files handles file read errors gracefully."""
        repo_dir = temp_dir / "owner" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()
        (repo_dir / "doc.md").write_text("# Doc")

        with patch.object(decorator, "_ensure_repo_cloned") as mock_ensure:
            mock_ensure.return_value = repo_dir

            with patch.object(decorator, "_read_local_file") as mock_read:
                mock_read.side_effect = Exception("Read error")

                files = []
                async for f in decorator.iter_repo_files("repo", "owner"):
                    files.append(f)

                # Should yield nothing due to error
                assert files == []


# ====================
# Integration tests
# ====================


class TestGetScmWithDecorator:
    """Tests for get_scm function with decorator."""

    def test_get_scm_applies_decorator_when_enabled(self):
        """Test that get_scm applies decorator when git CLI is enabled."""
        with patch("soliplex.agents.scm.app.settings") as mock_settings:
            mock_settings.scm_use_git_cli = True
            mock_settings.scm_git_repo_base_dir = None
            mock_settings.scm_git_cli_timeout = 300
            mock_settings.scm_auth_token = SecretStr("token")
            mock_settings.scm_auth_username = None
            mock_settings.scm_auth_password = None
            mock_settings.scm_owner = "owner"

            from soliplex.agents.config import SCM
            from soliplex.agents.scm.app import get_scm

            provider = get_scm(SCM.GITHUB)

            assert isinstance(provider, GitCliDecorator)

    def test_get_scm_no_decorator_when_disabled(self):
        """Test that get_scm doesn't apply decorator when git CLI is disabled."""
        with patch("soliplex.agents.scm.app.settings") as mock_settings:
            mock_settings.scm_use_git_cli = False
            mock_settings.scm_owner = "owner"
            mock_settings.scm_auth_token = SecretStr("token")

            from soliplex.agents.config import SCM
            from soliplex.agents.scm.app import get_scm
            from soliplex.agents.scm.github import GitHubProvider

            provider = get_scm(SCM.GITHUB)

            assert isinstance(provider, GitHubProvider)
            assert not isinstance(provider, GitCliDecorator)
