#


class SCMException(Exception):
    def __init__(self, msg: str) -> None:
        super().__init__(msg)


class APIFetchError(SCMException):
    def __init__(self) -> None:
        super().__init__("Failed to fetch from API")


class AuthenticationConfigError(SCMException):
    def __init__(self) -> None:
        super().__init__(
            "No valid authentication configured. "
            "Provide either scm_auth_token or both scm_auth_username and scm_auth_password."
        )


class GitHubAPIError(SCMException):
    def __init__(self) -> None:
        super().__init__("GitHub API error")


class RateLimitError(SCMException):
    """Raised when SCM API rate limit is exceeded."""

    def __init__(self, retry_after: int = 60) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded. Retry after {retry_after} seconds.")


class UnexpectedResponseError(Exception):
    def __init__(self) -> None:
        super().__init__("Unexpected response status")


class SCMListingError(SCMException):
    """A full listing named files it could not read, so it is incomplete.

    Raised where an incomplete listing would be reconciled against (and so
    delete the documents it left out) rather than reported row by row.
    """


class CursorNotFound(SCMException):
    """The incremental sync cursor's commit is not in the branch's history.

    The branch was force-pushed, the cursor is older than the history that
    was searched, or the local checkout no longer holds it. Either way the
    commits since the cursor are unknown, so the caller must run a full sync
    rather than read "no commits" as "up to date".
    """

    def __init__(self, sha: str) -> None:
        self.sha = sha
        super().__init__(f"sync cursor {sha} not found in the branch history")


# Git CLI classes - imported lazily to avoid circular imports
def __getattr__(name: str):
    """Lazy import for git_cli module to avoid circular imports."""
    git_cli_exports = {
        "GitCliDecorator",
        "GitCliError",
        "GitCloneError",
        "GitPullError",
        "GitCleanError",
        "GitLogError",
        "GitShowError",
        "InputSanitizationError",
        "GitCliWrapper",
        "sanitize_input",
        "mask_credentials",
    }
    if name in git_cli_exports:
        from soliplex.agents.scm import git_cli

        return getattr(git_cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
