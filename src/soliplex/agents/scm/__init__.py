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
